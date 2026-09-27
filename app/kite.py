"""Zerodha Kite client supporting two auth modes.

* ``connect``  — official Kite Connect API (api_key + daily access_token via login redirect).
* ``enctoken`` — the Kite web session token (pasted daily), same as algo-nisha.

Only what the scanner needs today: profile check and historical candles. Order
placement can be added here later without touching the scanner.
"""

import csv
import hashlib
import io
import threading
import time
from urllib.parse import unquote
from dataclasses import dataclass
from datetime import date, datetime

import httpx

CONNECT_BASE = "https://api.kite.trade"
WEB_BASE = "https://kite.zerodha.com/oms"
INSTRUMENTS_URL = "https://api.kite.trade/instruments/NSE"

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
        interval = TIMEFRAMES[timeframe][0]
        params = {"from": f"{day} 00:00:00", "to": f"{day} 23:59:59"}
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


# ---- Instruments (public dump, cached per day) ------------------------------

@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    token: int
    is_index: bool


_instruments: dict[str, Instrument] = {}
_instruments_day: date | None = None
_instruments_lock = threading.Lock()


def instruments() -> dict[str, Instrument]:
    global _instruments, _instruments_day
    with _instruments_lock:
        today = date.today()
        if _instruments and _instruments_day == today:
            return _instruments
        r = httpx.get(INSTRUMENTS_URL, timeout=30)
        r.raise_for_status()
        out = {}
        for row in csv.DictReader(io.StringIO(r.text)):
            if row["instrument_type"] == "EQ" and row["segment"] in ("NSE", "INDICES"):
                out[row["tradingsymbol"]] = Instrument(
                    row["tradingsymbol"], row["name"] or row["tradingsymbol"],
                    int(row["instrument_token"]), row["segment"] == "INDICES",
                )
        _instruments, _instruments_day = out, today
        return out


def search_instruments(query: str, limit: int = 8) -> list[Instrument]:
    q = query.strip().upper()
    if not q:
        return []
    items = instruments().values()
    starts = [i for i in items if i.symbol.startswith(q)]
    contains = [i for i in items if not i.symbol.startswith(q) and (q in i.symbol or q in i.name.upper())]
    return (sorted(starts, key=lambda i: len(i.symbol)) + contains)[:limit]
