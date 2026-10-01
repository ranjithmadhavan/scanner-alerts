"""Zerodha Kite client supporting two auth modes.

* ``connect``  — official Kite Connect API (api_key + daily access_token via login redirect).
* ``enctoken`` — the Kite web session token (pasted daily), same as algo-nisha.

Only what the scanner needs today: profile check and historical candles. Order
placement can be added here later without touching the scanner.
"""

import csv
import hashlib
import io
import logging
import threading
import time
from urllib.parse import unquote
from dataclasses import dataclass
from datetime import date, datetime

import httpx

CONNECT_BASE = "https://api.kite.trade"
WEB_BASE = "https://kite.zerodha.com/oms"
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


# Kite allows ~3 historical requests/second per account; one shared gap is simplest.
_throttle_lock = threading.Lock()
_last_call = 0.0
_MIN_GAP = 0.35


def _throttle() -> None:
    global _last_call
    with _throttle_lock:
        wait = _last_call + _MIN_GAP - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


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
        else:
            raise KiteError(f"Unknown Kite mode {mode!r}")
        self.mode = mode
        self.user_id = user_id

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            r = httpx.get(self.base + path, headers=self.headers, params=params, timeout=15)
        except httpx.HTTPError as e:
            raise KiteError(f"Network error talking to Kite: {e}") from e
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

    def candles(self, instrument_token: int, timeframe: str, day: date) -> list[Candle]:
        return self.candles_range(instrument_token, timeframe, day, day)

    def candles_range(self, instrument_token: int, timeframe: str, start: date, end: date) -> list[Candle]:
        """Candles from `start` to `end` inclusive. Kite allows up to 60 days of minute data per call."""
        interval = TIMEFRAMES[timeframe][0]
        params = {"from": f"{start} 00:00:00", "to": f"{end} 23:59:59"}
        if self.mode == "enctoken" and self.user_id:
            params["user_id"] = self.user_id
        _throttle()
        data = self._get(f"/instruments/historical/{instrument_token}/{interval}", params)
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


# ---- Instruments (public dumps, refreshed through the day) -------------------
# F&O contracts are listed and expire all the time, so the lists are re-downloaded
# every few hours rather than kept for the life of the process.

EXCHANGES = ("NSE", "BSE", "NFO", "BFO")  # also the order results are ranked in
DERIVATIVES = ("NFO", "BFO")
INSTRUMENTS_REFRESH = 4 * 3600
_RETRY_AFTER = 300  # an exchange failed to download: try again soon instead of in 4 hours
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

    @property
    def key(self) -> str:
        """What the UI passes around. NSE stays a bare symbol so older alerts and links still work."""
        return self.symbol if self.exchange == "NSE" else f"{self.exchange}:{self.symbol}"


_by_exchange: dict[str, dict[str, Instrument]] = {}
_instruments: dict[str, Instrument] = {}
_instruments_at = 0.0
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
                              exchange, kind, row["expiry"])
        elif kind == "EQ" and row["segment"] in (exchange, "INDICES"):
            inst = Instrument(symbol, (row["name"] or symbol).upper(), int(row["instrument_token"]),
                              row["segment"] == "INDICES", exchange)
        else:
            continue
        out[inst.key] = inst
    return out


def _download(exchange: str) -> dict[str, Instrument]:
    r = httpx.get(f"{INSTRUMENTS_URL}/{exchange}", timeout=30)
    r.raise_for_status()
    return parse_instruments(exchange, r.text)


def _reload() -> None:
    global _instruments, _instruments_at
    failed = []
    for exchange in EXCHANGES:
        try:
            _by_exchange[exchange] = _download(exchange)
        except Exception as e:  # keep the previous list for this exchange if we have one
            failed.append(exchange)
            log.warning("couldn't load %s instruments: %s", exchange, e)
    if not _by_exchange:
        raise KiteError("Couldn't load the instrument lists from Kite")
    _instruments = {k: i for exchange in EXCHANGES for k, i in _by_exchange.get(exchange, {}).items()}
    _instruments_at = time.monotonic() - (INSTRUMENTS_REFRESH - _RETRY_AFTER if failed else 0)


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
        # Stale lists are still served while a refresh runs, so a search never waits on the download.
        if time.monotonic() - _instruments_at >= INSTRUMENTS_REFRESH and _refreshing.acquire(blocking=False):
            threading.Thread(target=_reload_in_background, name="instruments", daemon=True).start()
        return _instruments
    with _instruments_lock:
        if not _instruments:
            _reload()
        return _instruments


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
