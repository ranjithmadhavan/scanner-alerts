"""Zerodha Kite client supporting two auth modes.

* ``connect``  — official Kite Connect API (api_key + daily access_token via login redirect).
* ``enctoken`` — the Kite web session token (pasted daily), same as algo-nisha.

Only what the scanner needs today: profile check and historical candles. Order
placement can be added here later without touching the scanner.
"""

import csv
import hashlib
import io
import json
import logging
import os
import struct
import tempfile
import threading
import time
from urllib.parse import quote as quote_url, unquote
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from websockets.exceptions import InvalidStatus, WebSocketException
from websockets.sync.client import connect as ws_connect

CONNECT_BASE = "https://api.kite.trade"
WEB_BASE = "https://kite.zerodha.com/oms"
WS_WEB = "wss://ws.zerodha.com/"
INSTRUMENTS_URL = "https://api.kite.trade/instruments"

log = logging.getLogger("kite")

# UI value -> (Kite interval name, minutes; None for daily)
TIMEFRAMES: dict[str, tuple[str, int | None]] = {
    "1m": ("minute", 1),
    "3m": ("3minute", 3),
    "5m": ("5minute", 5),
    "10m": ("10minute", 10),
    "15m": ("15minute", 15),
    "30m": ("30minute", 30),
    "1h": ("60minute", 60),
    "1d": ("day", None),
}


class KiteError(Exception):
    pass


class KiteAuthError(KiteError):
    """Token missing, expired or rejected — the user has to log in again."""


@dataclass
class Candle:
    start: datetime
    open: float
    high: float
    low: float
    close: float


class RateLimit:
    """Calls spaced at least `gap` seconds apart, across every thread (scans, scanners, backtests, pages).
    Kite answers bursts with HTTP 429 "Too many requests", so every call goes through one of these first.
    pause() holds the next calls back for a while, after Kite has said no anyway."""

    def __init__(self, gap: float):
        self.gap = gap
        self._lock = threading.Lock()
        self._next = 0.0  # monotonic time the next call may go

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if self._next > now:
                time.sleep(self._next - now)
            self._next = max(now, self._next) + self.gap

    def pause(self, seconds: float) -> None:
        with self._lock:
            self._next = max(self._next, time.monotonic() + seconds)


# Kite's published limits, per second: historical candles 3, quotes 1, everything else 10. Each is kept
# comfortably under, and shared by the whole app (one server, one IP, possibly one Kite app for everyone).
HISTORICAL = RateLimit(0.4)  # 2.5 a second
QUOTES = RateLimit(1.1)  # REST quotes and the live-feed connections web sessions use instead
OTHER = RateLimit(0.2)  # profile and the like
DUMPS = RateLimit(3.0)  # the daily instrument list downloads (api.kite.trade/instruments)
# Backtests take their candles through this as well, so they never use more than 2 of the 2.5 a second and
# live scans and alerts always have room.
BACKTESTS = RateLimit(0.5)
TOO_MANY_PAUSE = 3.0  # seconds every call of a kind waits after Kite says "Too many requests"
DUMPS_PAUSE = 60.0  # ...and before the next instrument list download, after a 429 on one
TOO_MANY_RETRIES = 2
LONG_WAIT = 30  # a Retry-After longer than this (seconds) is a block: stop sending until it ends, don't retry


class KiteBlocked(KiteError):
    """Kite (its Cloudflare front) has told this server to wait; nothing is sent until then."""


# When Kite says "wait until", across restarts: {what: epoch seconds}. `what` is "instruments" for the list
# downloads, or the host REST calls go to. Kept on disk next to the instrument lists, because a server that
# restarts (a deploy, or --reload while developing) and asks again at once only makes the block longer.
_waits: dict[str, float] = {}
_waits_loaded = False
_waits_lock = threading.Lock()


def _waits_file() -> Path:
    return INSTRUMENTS_DIR / "kite-wait.json"


def blocked_until(what: str) -> float:
    global _waits, _waits_loaded
    with _waits_lock:
        if not _waits_loaded:
            try:
                _waits = {k: float(v) for k, v in json.loads(_waits_file().read_text()).items()}
            except (OSError, ValueError, AttributeError):
                _waits = {}
            _waits_loaded = True
        return _waits.get(what, 0.0)


def _retry_after(r: httpx.Response) -> float:
    """Seconds Kite asked us to wait: its Retry-After header, kept between 1 minute and 6 hours (10 minutes if absent)."""
    try:
        return min(max(float(r.headers.get("retry-after", "")), 60.0), 6 * 3600.0)
    except ValueError:
        return 600.0


def _block(what: str, seconds: float) -> float:
    until = max(blocked_until(what), time.time() + seconds)
    with _waits_lock:
        _waits[what] = until
        try:
            INSTRUMENTS_DIR.mkdir(parents=True, exist_ok=True)
            _waits_file().write_text(json.dumps(_waits))
        except OSError as e:
            log.warning("couldn't save Kite's wait: %s", e)
    log.warning("Kite asked us to wait %ds before more %s requests (until %s)", seconds, what, _clock(until))
    return until


def _clock(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, _IST).strftime("%-I:%M %p")


def check_blocked(what: str) -> None:
    """Raise KiteBlocked, without sending anything, while Kite's wait for `what` lasts."""
    until = blocked_until(what)
    if time.time() < until:
        raise KiteBlocked(f"Kite asked this server to wait until {_clock(until)} before more requests. It will carry on then.")


def parse_ticks(msg: bytes) -> dict[int, dict]:
    """Kite's binary tick frame: a packet count, then length-prefixed packets of big-endian int32s.
    Every packet starts token, last price (paise); quote and full packets have the day's OHLC at byte 28, full ones for F&O OI at byte 48.
    A 1-byte frame is a heartbeat."""
    out: dict[int, dict] = {}
    if len(msg) < 2:
        return out
    (count,), at = struct.unpack_from(">H", msg, 0), 2
    for _ in range(count):
        if at + 2 > len(msg):
            break
        (size,) = struct.unpack_from(">H", msg, at)
        packet = msg[at + 2:at + 2 + size]
        at += 2 + size
        if len(packet) < 8:
            continue
        token, ltp = struct.unpack_from(">ii", packet, 0)
        tick = {"last_price": ltp / 100}
        if len(packet) >= 44:  # quote and full packets carry the day's OHLC, as REST quotes do
            o, h, lo, c = struct.unpack_from(">iiii", packet, 28)
            tick["ohlc"] = {"open": o / 100, "high": h / 100, "low": lo / 100, "close": c / 100}
        if len(packet) >= 52:  # index packets are 28/32 bytes and carry no OI
            tick["oi"] = struct.unpack_from(">i", packet, 48)[0]
        out[token] = tick
    return out


class KiteClient:
    def __init__(self, mode: str, *, api_key: str = "", access_token: str = "", enctoken: str = "", user_id: str = ""):
        if mode == "connect":
            if not (api_key and access_token):
                raise KiteAuthError("Kite Connect is not logged in")
            self.base = CONNECT_BASE
            self.headers = {"X-Kite-Version": "3", "Authorization": f"token {api_key}:{access_token}"}
        elif mode == "enctoken":
            if not enctoken:
                raise KiteAuthError("Enctoken is missing")
            self.base = WEB_BASE
            self.headers = {"Authorization": f"enctoken {enctoken}"}
            self.enctoken = enctoken
        else:
            raise KiteError(f"Unknown Kite mode {mode!r}")
        self.mode = mode
        self.user_id = user_id

    def _get(self, path: str, params: dict | None = None, limit: RateLimit | None = None) -> dict:
        limit = limit or OTHER
        host = self.base.split("/")[2]
        for attempt in range(TOO_MANY_RETRIES + 1):
            check_blocked(host)
            limit.wait()
            try:
                r = httpx.get(self.base + path, headers=self.headers, params=params, timeout=15)
            except httpx.HTTPError as e:
                raise KiteError(f"Network error talking to Kite: {e}") from e
            if r.status_code != 429:
                break
            if _retry_after(r) > LONG_WAIT and r.headers.get("retry-after"):
                # Blocked for a while: asking again before then only makes it longer.
                _block(host, _retry_after(r))
                check_blocked(host)
            # Too many requests anyway (another app on the account, or Kite being strict): hold every call of
            # this kind back for a moment, then try again.
            limit.pause(TOO_MANY_PAUSE * (attempt + 1))
            log.warning("Kite said too many requests for %s; pausing %ss", path.split("/")[1], TOO_MANY_PAUSE * (attempt + 1))
        else:
            raise KiteError("Kite is turning away requests (too many requests). Try again in a minute.")
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code in (401, 403) or body.get("error_type") in ("TokenException", "PermissionException"):
            raise KiteAuthError(body.get("message") or f"Kite rejected the session (HTTP {r.status_code})")
        if r.status_code != 200 or body.get("status") != "success":
            raise KiteError(body.get("message") or f"Kite returned HTTP {r.status_code}")
        return body["data"]

    def profile(self) -> dict:
        return self._get("/user/profile")

    def quote(self, keys: list[str]) -> dict:
        """Last price and open interest for up to 500 'EXCHANGE:SYMBOL' keys: {key: {"last_price", "oi"}}.

        Kite Connect answers this over REST. A web session gets 400 from /oms/quote, so it reads
        one round of ticks off the websocket the Kite web option chain uses instead."""
        if self.mode == "enctoken":
            return self._quote_ticks(keys)
        return self._get("/quote", {"i": keys}, QUOTES)

    def _quote_ticks(self, keys: list[str], wait: float = 10.0) -> dict:
        known = instruments()
        tokens: dict[int, str] = {}
        for k in keys:
            inst = known.get(k.removeprefix("NSE:"))
            if inst:
                tokens[inst.token] = k
        if not tokens:
            return {}
        user_id = self.user_id or self.profile().get("user_id", "")
        url = (f"{WS_WEB}?api_key=kitefront&user_id={quote_url(user_id, safe="")}&enctoken={quote_url(self.enctoken, safe="")}"
               f"&uid={int(time.time() * 1000)}&user-agent=kite3-web&version=3.0.0")
        out: dict[str, dict] = {}
        deadline = time.monotonic() + wait
        QUOTES.wait()
        try:
            with ws_connect(url, origin="https://kite.zerodha.com", open_timeout=wait) as ws:
                ws.send(json.dumps({"a": "subscribe", "v": list(tokens)}))
                ws.send(json.dumps({"a": "mode", "v": ["full", list(tokens)]}))
                while len(out) < len(tokens) and (left := deadline - time.monotonic()) > 0:
                    try:
                        msg = ws.recv(timeout=left)
                    except TimeoutError:
                        break
                    if isinstance(msg, bytes):
                        for token, tick in parse_ticks(msg).items():
                            if token in tokens:
                                out[tokens[token]] = tick
        except InvalidStatus as e:
            if e.response.status_code in (401, 403):
                raise KiteAuthError("Kite rejected the session; paste a fresh enctoken") from e
            raise KiteError(f"Kite's live feed refused the connection (HTTP {e.response.status_code})") from e
        except (OSError, WebSocketException) as e:
            raise KiteError(f"Network error on Kite's live feed: {e}") from e
        if not out:
            raise KiteError("Kite's live feed sent no prices")
        return out

    def candles(self, instrument_token: int, timeframe: str, day: date) -> list[Candle]:
        return self.candles_range(instrument_token, timeframe, day, day)

    def candles_range(self, instrument_token: int, timeframe: str, start: date, end: date) -> list[Candle]:
        """Candles from `start` to `end` inclusive. Kite allows up to 60 days of minute data per call."""
        interval = TIMEFRAMES[timeframe][0]
        params = {"from": f"{start} 00:00:00", "to": f"{end} 23:59:59"}
        if self.mode == "enctoken" and self.user_id:
            params["user_id"] = self.user_id
        data = self._get(f"/instruments/historical/{instrument_token}/{interval}", params, HISTORICAL)
        return [
            Candle(datetime.fromisoformat(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]))
            for c in data.get("candles", [])
        ]


def clean_enctoken(raw: str) -> str:
    """Accept the enctoken however it was copied: bare, 'enctoken=…', 'enctoken …', quoted,
    wrapped across lines, or URL-encoded (DevTools shows %2B etc. unless 'Show URL-decoded' is ticked)."""
    token = "".join(raw.split()).strip("'\";")
    for prefix in ("enctoken=", "enctoken:", "enctoken"):
        if token.lower().startswith(prefix):
            token = token[len(prefix):]
            break
    if "%" in token:
        token = unquote(token)
    return token


# ---- Kite Connect login -----------------------------------------------------

def connect_login_url(api_key: str) -> str:
    return f"https://kite.zerodha.com/connect/login?v=3&api_key={api_key}"


def connect_exchange_token(api_key: str, api_secret: str, request_token: str) -> dict:
    """Swap the request_token from the login redirect for an access_token."""
    checksum = hashlib.sha256(f"{api_key}{request_token}{api_secret}".encode()).hexdigest()
    r = httpx.post(
        f"{CONNECT_BASE}/session/token",
        headers={"X-Kite-Version": "3"},
        data={"api_key": api_key, "request_token": request_token, "checksum": checksum},
        timeout=15,
    )
    body = r.json() if r.content else {}
    if r.status_code != 200 or body.get("status") != "success":
        raise KiteAuthError(body.get("message") or f"Kite login failed (HTTP {r.status_code})")
    return body["data"]


# ---- Instruments (Kite's public dumps, once a day) ----------------------------
# Kite publishes each exchange's list once a day, early in the morning, and rate-limits the downloads hard
# (HTTP 429). So each list is downloaded once a day, kept on disk so a restart reads it back instead of
# downloading again, and a failed download is retried after a few minutes, not on every request. A list
# that can't be downloaded is served from the last copy on disk.

EXCHANGES = ("NSE", "BSE", "NFO", "BFO")  # also the order results are ranked in
DERIVATIVES = ("NFO", "BFO")
LISTS_PUBLISHED = dtime(8, 30)  # IST; a list fetched after the latest of these is the day's
INSTRUMENTS_DIR = Path(os.environ.get("INSTRUMENTS_CACHE_DIR") or Path(tempfile.gettempdir()) / "stock-scanner-instruments")
_RETRY_AFTER = 300  # seconds before trying Kite again after a failed download
_IST = ZoneInfo("Asia/Kolkata")
_KIND_RANK = {"EQ": 0, "FUT": 1, "CE": 2, "PE": 2}


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    name: str
    token: int
    is_index: bool
    exchange: str = "NSE"
    kind: str = "EQ"  # EQ (stocks and indices), FUT, CE, PE
    expiry: str = ""  # ISO date, derivatives only
    underlying: str = ""  # derivatives only, e.g. NIFTY
    strike: float = 0.0  # options only

    @property
    def key(self) -> str:
        """What the UI passes around. NSE stays a bare symbol so older alerts and links still work."""
        return self.symbol if self.exchange == "NSE" else f"{self.exchange}:{self.symbol}"


_by_exchange: dict[str, dict[str, Instrument]] = {}
_instruments: dict[str, Instrument] = {}
_loaded_at = 0.0  # wall-clock time of the last load (from Kite or from disk)
_stale = False  # some list is an old copy or missing: try Kite again after _RETRY_AFTER
_tried_at = -_RETRY_AFTER  # monotonic time of the last load attempt
_failed_at = -_RETRY_AFTER  # ...and of the last one that got no list at all
_instruments_lock = threading.Lock()
_refreshing = threading.Lock()


def parse_instruments(exchange: str, text: str) -> dict[str, Instrument]:
    """Rows we can alert on from one Kite dump: stocks and indices, or futures and options."""
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        kind, symbol = row["instrument_type"], row["tradingsymbol"]
        if exchange in DERIVATIVES:
            if kind not in ("FUT", "CE", "PE"):
                continue
            # Contract symbols are cryptic (NIFTY26O0624500CE), so the name spells them out.
            expiry = date.fromisoformat(row["expiry"])
            parts = [(row["name"] or symbol).upper(), f"{expiry:%d %b %y}".upper()]
            if kind != "FUT":
                parts.append(f"{float(row['strike']):g}")
            inst = Instrument(symbol, " ".join(parts + [kind]), int(row["instrument_token"]), False,
                              exchange, kind, row["expiry"], (row["name"] or "").upper(),
                              float(row["strike"] or 0) if kind != "FUT" else 0.0)
        elif kind == "EQ" and row["segment"] in (exchange, "INDICES"):
            inst = Instrument(symbol, (row["name"] or symbol).upper(), int(row["instrument_token"]),
                              row["segment"] == "INDICES", exchange)
        else:
            continue
        out[inst.key] = inst
    return out


def published_since() -> float:
    """The latest time Kite published its lists (8:30 AM IST today, or yesterday before then), as epoch seconds."""
    now = datetime.now(_IST)
    mark = datetime.combine(now.date(), LISTS_PUBLISHED, _IST)
    return (mark if now >= mark else mark - timedelta(days=1)).timestamp()


def _download(exchange: str, force: bool = False) -> tuple[dict[str, Instrument], bool]:
    """One exchange's list and whether it is the day's: from disk if today's copy is there (unless `force`),
    else from Kite, saved to disk. If Kite fails, the last copy on disk (not the day's) if there is one."""
    path = INSTRUMENTS_DIR / f"{exchange}.csv"
    if not force and path.exists() and path.stat().st_mtime >= published_since():
        return parse_instruments(exchange, path.read_text()), True
    try:
        check_blocked("instruments")
        DUMPS.wait()
        r = httpx.get(f"{INSTRUMENTS_URL}/{exchange}", timeout=30)
        if r.status_code == 429:
            DUMPS.pause(DUMPS_PAUSE)
            _block("instruments", _retry_after(r))  # Kite's own Retry-After, which survives restarts
        r.raise_for_status()
    except (httpx.HTTPError, KiteBlocked) as e:
        if not path.exists():
            raise
        log.warning("couldn't download %s instruments (%s); using the copy from %s", exchange, e,
                    datetime.fromtimestamp(path.stat().st_mtime, _IST).strftime("%d %b %H:%M"))
        return parse_instruments(exchange, path.read_text()), False
    parsed = parse_instruments(exchange, r.text)
    INSTRUMENTS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(r.text)
    tmp.replace(path)
    return parsed, True


def _reload(force: bool = False) -> None:
    global _instruments, _loaded_at, _stale, _tried_at, _failed_at
    _tried_at = time.monotonic()
    stale = False
    for exchange in EXCHANGES:
        try:
            _by_exchange[exchange], fresh = _download(exchange, force)
            stale = stale or not fresh
        except Exception as e:  # keep the previous list for this exchange if we have one
            stale = True
            log.warning("couldn't load %s instruments: %s", exchange, e)
    if not _by_exchange:
        _failed_at = time.monotonic()
        check_blocked("instruments")  # say until when, if Kite has said
        raise KiteError("Couldn't load the instrument lists from Kite. Trying again in a few minutes.")
    _instruments = {k: i for exchange in EXCHANGES for k, i in _by_exchange.get(exchange, {}).items()}
    _loaded_at, _stale = time.time(), stale


def _reload_in_background() -> None:
    try:
        _reload()
    except Exception as e:
        log.warning("instrument refresh failed: %s", e)
    finally:
        _refreshing.release()


def instruments() -> dict[str, Instrument]:
    """Everything that can be alerted on, keyed by Instrument.key."""
    if _instruments:
        # Once Kite has published the day's lists (or a failed download is due a retry), refresh in the background;
        # the lists we have are served meanwhile, so a search never waits on a download.
        due = _loaded_at < published_since() or (_stale and time.monotonic() - _tried_at >= _RETRY_AFTER)
        if due and time.time() >= blocked_until("instruments") and _refreshing.acquire(blocking=False):
            threading.Thread(target=_reload_in_background, name="instruments", daemon=True).start()
        return _instruments
    with _instruments_lock:
        if not _instruments:
            check_blocked("instruments")
            if time.monotonic() - _failed_at < _RETRY_AFTER:  # failed a moment ago: don't hit Kite on every request
                raise KiteError("Kite's instrument lists couldn't be loaded. Trying again in a few minutes.")
            _reload()
        return _instruments


def refresh_instruments() -> dict[str, int]:
    """Download every list from Kite now (the super admin's Refresh button). Returns instruments per exchange."""
    with _instruments_lock:
        _reload(force=True)
    return {e: len(_by_exchange.get(e, {})) for e in EXCHANGES}


def find_instrument(symbol: str) -> Instrument | None:
    """Look up `SYMBOL` or `EXCHANGE:SYMBOL`. A bare symbol means NSE first, then the other exchanges."""
    symbol = symbol.strip().upper()
    if not symbol:
        return None
    items = instruments()
    if symbol in items:
        return items[symbol]
    if symbol.startswith("NSE:"):
        return items.get(symbol[4:])
    if ":" not in symbol:
        for exchange in EXCHANGES[1:]:
            if inst := items.get(f"{exchange}:{symbol}"):
                return inst
    return None


def search_instruments(query: str, limit: int = 10) -> list[Instrument]:
    """Every word must appear in the symbol or name, so 'nifty 24500 ce' and 'sensex oct fut' work."""
    query = query.strip().upper()
    exchange, _, rest = query.partition(":")
    if rest and exchange in EXCHANGES:  # a picked value such as BSE:SENSEX
        query = rest
    else:
        exchange = ""
    words = query.split()
    if not words:
        return []
    q = "".join(words)
    found = [i for i in instruments().values()
             if all(w in i.symbol or w in i.name for w in words) and exchange in ("", i.exchange)]
    # Exact symbol, then symbols starting with the query; indices, then stocks, futures and options;
    # nearest expiry first.
    found.sort(key=lambda i: (
        i.symbol.replace(" ", "") != q, not i.symbol.startswith(words[0]), not i.is_index,
        _KIND_RANK[i.kind], EXCHANGES.index(i.exchange), i.expiry, len(i.symbol), i.symbol))
    return found[:limit]
