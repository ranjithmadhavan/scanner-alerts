"""Current price and chart data for the alerts page, from the user's own Kite session.

Uses historical candles (works for both Kite Connect and enctoken). Results are cached
briefly so opening a chart twice, or several people looking at the same stock, doesn't
eat into Kite's ~3 requests/second limit.
"""

import threading
import time
from datetime import timedelta

from app import brokers
from app.kite import Candle, Instrument
from app.market import IST, is_market_open, is_trading_day, load_settings, now_ist

IST_OFFSET = 19800  # lightweight-charts draws UTC; shift so the axis reads in IST

# range tab -> (candle timeframe, calendar days to fetch; 0 = latest session only)
RANGES = {
    "1D": ("5m", 0),
    "5D": ("15m", 7),
    "1M": ("1h", 31),
    "6M": ("1d", 183),
    "1Y": ("1d", 366),
}

_cache: dict[tuple, tuple[float, object]] = {}
_lock = threading.Lock()


def _cached(key: tuple, ttl_open: float, ttl_closed: float, fetch):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    value = fetch()
    ttl = ttl_open if is_market_open(load_settings(), now_ist()) else ttl_closed
    with _lock:
        _cache[key] = (now + ttl, value)
    return value


def quote(username: str, inst: Instrument) -> dict | None:
    """Last price and change vs previous close. Raises KiteError/KiteAuthError."""
    def fetch():
        today = now_ist().date()
        days = brokers.client_for(username).candles_range(inst.token, "1d", today - timedelta(days=14), today)
        if not days:
            return None
        last = days[-1]
        prev = days[-2].close if len(days) > 1 else last.open
        change = last.close - prev
        return {
            "price": last.close,
            "change": change,
            "pct": (change / prev * 100) if prev else 0.0,
            "day": last.start.astimezone(IST).date(),
            "live": last.start.astimezone(IST).date() == today and is_market_open(load_settings(), now_ist()),
            "at": now_ist(),
        }
    return _cached(("quote", username, inst.token), 15, 600, fetch)


def _latest_session(client, token: int, timeframe: str) -> list[Candle]:
    """Today's candles once the market has opened, else the last session that has data."""
    s, now = load_settings(), now_ist()
    day = now.date() if is_trading_day(now) and now >= s.open_at(now) else None
    if day:
        candles = client.candles(token, timeframe, day)
        if candles:
            return candles
    # Walk back over weekends/holidays in one call, then keep only the last day.
    candles = client.candles_range(token, timeframe, now.date() - timedelta(days=7), now.date())
    if not candles:
        return []
    last_day = candles[-1].start.astimezone(IST).date()
    return [c for c in candles if c.start.astimezone(IST).date() == last_day]


def chart(username: str, inst: Instrument, range_key: str) -> dict:
    """Candles shaped for lightweight-charts. Raises KiteError/KiteAuthError."""
    timeframe, days = RANGES.get(range_key, RANGES["5D"])

    def fetch():
        client = brokers.client_for(username)
        if days == 0:
            candles = _latest_session(client, inst.token, timeframe)
        else:
            today = now_ist().date()
            candles = client.candles_range(inst.token, timeframe, today - timedelta(days=days), today)
        daily = timeframe == "1d"
        return {
            "symbol": inst.symbol,
            "range": range_key,
            "daily": daily,
            "candles": [
                {
                    "time": c.start.astimezone(IST).date().isoformat() if daily
                    else int(c.start.timestamp()) + IST_OFFSET,
                    "open": c.open, "high": c.high, "low": c.low, "close": c.close,
                }
                for c in candles
            ],
        }
    return _cached(("chart", username, inst.token, range_key), 30, 600, fetch)
