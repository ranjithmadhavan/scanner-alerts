"""Market-hours logic. All times are evaluated in IST regardless of the server's timezone."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.store import store

IST = ZoneInfo("Asia/Kolkata")

DEFAULTS = {"open": "09:15", "close": "15:30", "scan_interval": 60}
SCAN_INTERVALS = [30, 60, 120, 300]
# Keep scanning briefly after the bell so the final candle (e.g. 15:15–15:30) gets evaluated.
CLOSE_GRACE = timedelta(minutes=2)


@dataclass
class MarketSettings:
    open: time
    close: time
    scan_interval: int

    def open_at(self, day: datetime) -> datetime:
        return datetime.combine(day.date(), self.open, IST)

    def close_at(self, day: datetime) -> datetime:
        return datetime.combine(day.date(), self.close, IST)


def now_ist() -> datetime:
    return datetime.now(IST)


def parse_hhmm(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def load_settings() -> MarketSettings:
    raw = {**DEFAULTS, **(store.get("settings", "market") or {})}
    return MarketSettings(parse_hhmm(raw["open"]), parse_hhmm(raw["close"]), int(raw["scan_interval"]))


def save_settings(open_: str, close: str, scan_interval: int) -> None:
    store.put("settings", "market", {"open": open_, "close": close, "scan_interval": scan_interval})


def is_trading_day(now: datetime) -> bool:
    return now.astimezone(IST).weekday() < 5  # Mon–Fri; holidays not handled yet


def is_market_open(s: MarketSettings, now: datetime) -> bool:
    now = now.astimezone(IST)
    return is_trading_day(now) and s.open_at(now) <= now < s.close_at(now)


def in_scan_window(s: MarketSettings, now: datetime) -> bool:
    now = now.astimezone(IST)
    return is_trading_day(now) and s.open_at(now) <= now < s.close_at(now) + CLOSE_GRACE


def status_text(s: MarketSettings, now: datetime) -> tuple[bool, str]:
    """(is_open, human sentence) for the top bar."""
    now = now.astimezone(IST)
    fmt = lambda t: t.strftime("%-I:%M %p").lower()
    if is_market_open(s, now):
        return True, f"Market open, closes at {fmt(s.close)}"
    if is_trading_day(now) and now < s.open_at(now):
        return False, f"Opens today at {fmt(s.open)}"
    nxt = now + timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += timedelta(days=1)
    day = "tomorrow" if (nxt.date() - now.date()).days == 1 else nxt.strftime("%A")
    return False, f"Market closed, opens {day} at {fmt(s.open)}"
