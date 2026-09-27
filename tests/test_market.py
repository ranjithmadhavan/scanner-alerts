from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.market import IST, MarketSettings, in_scan_window, is_market_open

S = MarketSettings(time(9, 15), time(15, 30), 60)


def test_open_uses_ist_even_for_utc_times():
    # 04:00 UTC on a Monday = 09:30 IST -> open
    assert is_market_open(S, datetime(2026, 9, 28, 4, 0, tzinfo=ZoneInfo("UTC")))
    # 03:40 UTC = 09:10 IST -> not yet open
    assert not is_market_open(S, datetime(2026, 9, 28, 3, 40, tzinfo=ZoneInfo("UTC")))
    # A US-east time that is 15:29 IST
    assert is_market_open(S, datetime(2026, 9, 28, 5, 59, tzinfo=ZoneInfo("America/New_York")))


def test_closed_on_weekends_and_after_close():
    assert not is_market_open(S, datetime(2026, 9, 27, 11, 0, tzinfo=IST))  # Sunday
    assert not is_market_open(S, datetime(2026, 9, 28, 15, 30, tzinfo=IST))


def test_scan_window_has_grace_after_close():
    assert in_scan_window(S, datetime(2026, 9, 28, 15, 31, tzinfo=IST))
    assert not in_scan_window(S, datetime(2026, 9, 28, 15, 33, tzinfo=IST))
