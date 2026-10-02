"""Market-hours logic. All times are evaluated in IST regardless of the server's timezone."""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.store import store

IST = ZoneInfo("Asia/Kolkata")

DEFAULTS = {"open": "09:15", "close": "15:30", "scan_interval": 60}
SCAN_INTERVALS = [30, 60, 120, 300]
# Keep scanning briefly after the bell so the final candle (e.g. 15:15–15:30) gets evaluated.
CLOSE_GRACE = timedelta(minutes=2)

# NSE trading holidays for 2026 that fall on a weekday (from NSE's published list). Used until
# the super admin saves a list of their own on the Market hours page, which then takes over.
DEFAULT_HOLIDAYS = [
    {"date": "2026-01-15", "name": "Municipal Corporation Election - Maharashtra"},
    {"date": "2026-01-26", "name": "Republic Day"},
    {"date": "2026-03-03", "name": "Holi"},
    {"date": "2026-03-26", "name": "Shri Ram Navami"},
    {"date": "2026-03-31", "name": "Shri Mahavir Jayanti"},
    {"date": "2026-04-03", "name": "Good Friday"},
    {"date": "2026-04-14", "name": "Dr. Baba Saheb Ambedkar Jayanti"},
    {"date": "2026-05-01", "name": "Maharashtra Day"},
    {"date": "2026-05-28", "name": "Bakri Id"},
    {"date": "2026-06-26", "name": "Muharram"},
    {"date": "2026-09-14", "name": "Ganesh Chaturthi"},
    {"date": "2026-10-02", "name": "Mahatma Gandhi Jayanti"},
    {"date": "2026-10-20", "name": "Dussehra"},
    {"date": "2026-11-10", "name": "Diwali-Balipratipada"},
    {"date": "2026-11-24", "name": "Prakash Gurpurb Sri Guru Nanak Dev"},
    {"date": "2026-12-25", "name": "Christmas"},
]
NSE_HOLIDAYS_URL = "https://www.nseindia.com/api/holiday-master?type=trading"


@dataclass
class MarketSettings:
    open: time
    close: time
    scan_interval: int
    holidays: dict[str, str] = field(default_factory=dict)  # ISO date -> name

    def open_at(self, day: datetime) -> datetime:
        return datetime.combine(day.date(), self.open, IST)

    def close_at(self, day: datetime) -> datetime:
        return datetime.combine(day.date(), self.close, IST)


def now_ist() -> datetime:
    return datetime.now(IST)


def parse_hhmm(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def load_settings() -> MarketSettings:
    raw = {**DEFAULTS, "holidays": DEFAULT_HOLIDAYS, **(store.get("settings", "market") or {})}
    return MarketSettings(parse_hhmm(raw["open"]), parse_hhmm(raw["close"]), int(raw["scan_interval"]),
                          {h["date"]: h["name"] for h in raw["holidays"]})


def save_settings(open_: str, close: str, scan_interval: int) -> None:
    store.update("settings", "market", {"open": open_, "close": close, "scan_interval": scan_interval})


def save_holidays(holidays: dict[str, str]) -> None:
    store.update("settings", "market", {"holidays": [{"date": d, "name": n} for d, n in sorted(holidays.items())]})


def fetch_nse_holidays() -> dict[str, str]:
    """Trading holidays as published by NSE: {ISO date: name}. Raises RuntimeError if NSE won't answer
    (it often turns away requests from cloud servers)."""
    try:
        r = httpx.get(NSE_HOLIDAYS_URL, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Accept": "application/json", "Referer": "https://www.nseindia.com/resources/exchange-communication-holidays"})
        rows = r.json()["CM"]
        return {datetime.strptime(h["tradingDate"], "%d-%b-%Y").date().isoformat(): h["description"].strip(" *") for h in rows}
    except Exception as e:
        raise RuntimeError("NSE didn't return its holiday list") from e


def holiday_name(day: datetime | date) -> str | None:
    """Name of the market holiday on `day` (IST), or None if it's an ordinary day."""
    if isinstance(day, datetime):
        day = day.astimezone(IST).date()
    return load_settings().holidays.get(day.isoformat())


def is_trading_day(now: datetime) -> bool:
    now = now.astimezone(IST)
    return now.weekday() < 5 and holiday_name(now) is None  # Mon–Fri, minus market holidays


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
    gap = (nxt.date() - now.date()).days
    day = "tomorrow" if gap == 1 else nxt.strftime("%A") if gap < 7 else nxt.strftime("%a %-d %b")
    if holiday := holiday_name(now):
        return False, f"Market holiday today ({holiday}), opens {day} at {fmt(s.open)}"
    return False, f"Market closed, opens {day} at {fmt(s.open)}"
