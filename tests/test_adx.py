"""ADX trend: indicators, signal candles, one trade per trending phase, its own exits, and its page."""

from datetime import date, datetime, timedelta

import pytest

from app import adx
from app.adx import AdxRules, indicators, monthly_expiry, rma
from app.crossover import Exits, trades
from app.kite import Candle
from app.market import IST

T0 = datetime(2026, 9, 1, tzinfo=IST)


def daily(*ohlc):
    return [Candle(T0 + timedelta(days=i), *row) for i, row in enumerate(ohlc)]


def test_rma_and_a_steady_rise():
    assert rma([None, 1, 2, 3, 4], 2) == [None, None, 1.5, 2.25, 3.125]
    # Every candle a higher high and higher low: all the movement is up, so -DI is 0 and ADX heads to 100.
    up = indicators(daily(*[(100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(60)]), 14, 14)
    assert up["mdi"][-1] == 0 and up["pdi"][-1] > 40 and up["adx"][-1] == pytest.approx(100)
    assert up["atr"][-1] == pytest.approx(2.0)  # each candle's true range is 2 (high 101+i, previous close 99.5+i)


def fake(adx_vals, pdi, mdi, atr=None):
    n = len(adx_vals)
    return {"adx": adx_vals, "pdi": pdi, "mdi": mdi, "atr": atr or [2.0] * n}


# Candle 0: quiet (ADX 20). Candles 1-2: trending up (ADX 30, +DI 30, -DI 10); candle 2 stays under candle 1's high.
# Candle 3 breaks candle 2's high. Candle 4 still trending, candle 5 quiet, candle 6 trending again (a new phase).
UP = daily((100, 101, 99, 100.5), (100.5, 102, 100, 101.8), (101.8, 101.9, 101, 101.5), (101.5, 104, 101.4, 103.8),
           (103.8, 104.5, 103, 103.2), (103.2, 103.5, 102, 102.5), (102.5, 104, 102.2, 103.9), (103.9, 105, 103.5, 104.8))
UP_IND = fake([20, 30, 31, 32, 33, 22, 30, 31], [22, 30, 31, 32, 33, 22, 30, 31], [18, 10, 9, 8, 8, 18, 10, 9])


def test_signal_candle_moves_with_the_trend_and_one_trade_per_phase(monkeypatch):
    monkeypatch.setattr(adx, "indicators", lambda c, a, b: UP_IND)
    w = AdxRules().prepare(UP).walk
    # Candle 1 is the first signal candle (high 102); candle 2 replaces it (high 101.9, SL its low 101 less 1 ATR, 2).
    # Candle 3 trades 104 > 101.9: entry. Candle 4 still trends but the phase has had its entry. Candle 5 is quiet;
    # candle 6 starts a new phase and sets up at its high, 104; candle 7 trades 105: the second phase's entry.
    assert [(e.setup.trigger, e.setup.level, e.setup.stop, e.index, e.setup.crossed) for e in w.entries] == \
        [(2, 101.9, 99.0, 3, 1), (6, 104, 100.2, 7, 6)]
    assert w.crossovers == [(1, "buy"), (6, "buy")]


def test_a_phase_under_way_at_the_start_gives_nothing_and_a_failed_signal_is_dropped(monkeypatch):
    # Trending from the very first reading: we can't tell if it had its entry.
    monkeypatch.setattr(adx, "indicators", lambda c, a, b: fake([30] * 4, [30] * 4, [10] * 4))
    w = AdxRules().prepare(UP[:4]).walk
    assert w.entries == [] and w.pending is None
    # Signal on candle 1, but candle 2 closes no longer trending (ADX 24): dropped, so candle 3 trading above it is
    # no entry. (Had candle 2 itself traded above 102, that would have been the entry, before its close.)
    monkeypatch.setattr(adx, "indicators", lambda c, a, b: fake([20, 30, 24, 24], [20, 30, 30, 30], [18, 10, 10, 10]))
    w = AdxRules().prepare(UP[:4]).walk
    assert w.entries == [] and w.pending is None
    # Still trending at the end: the setup waits.
    monkeypatch.setattr(adx, "indicators", lambda c, a, b: fake([20, 30], [20, 30], [18, 10]))
    assert AdxRules().prepare(UP[:2]).walk.pending.level == 102


def test_sells_mirror_and_sides(monkeypatch):
    down = daily((100, 101, 99, 99.5), (99.5, 100, 98, 98.2), (98.2, 98.5, 96.5, 96.8))
    monkeypatch.setattr(adx, "indicators", lambda c, a, b: fake([20, 30, 31], [18, 10, 9], [22, 30, 31], [1.5] * 3))
    w = AdxRules().prepare(down).walk
    # Signal candle 1 (low 98, SL 100 + 1.5 = 101.5); candle 2 trades 96.5 < 98: a sell.
    assert [(e.side, e.setup.level, e.setup.stop, e.index) for e in w.entries] == [("sell", 98, 101.5, 2)]
    assert AdxRules(sides="buy").prepare(down).walk.entries == []
    assert AdxRules(atr_mult=2).prepare(down).walk.entries[0].setup.stop == 103


def test_own_exits_trend_end_and_monthly_expiry(monkeypatch):
    monkeypatch.setattr(adx, "indicators", lambda c, a, b: UP_IND)
    # Trend ends on candle 5 (ADX 22): what's left goes at its close, 102.5. Entry 101.9, SL 99.
    got, _ = trades(UP, AdxRules(), Exits(book=0, trend_exit=True))
    first = got[0]
    assert (first.entry.index, first.fills[-1].reason, first.fills[-1].price) == (3, "trend_end", 102.5)
    assert first.r == pytest.approx((102.5 - 101.9) / (101.9 - 99))
    assert [t.entry.index for t in got] == [3, 7]  # the next phase's entry follows
    # Without it the first trade is still open when the candles end.
    got, _ = trades(UP, AdxRules(), Exits(book=0))
    assert got[0].status == "open"


def test_monthly_expiry_dates():
    assert monthly_expiry(2026, 10) == date(2026, 10, 27)  # last Tuesday
    assert monthly_expiry(2025, 8) == date(2025, 8, 28)  # last Thursday, before the switch
    assert monthly_expiry(2025, 9) == date(2025, 9, 30)
    assert monthly_expiry(2026, 10, lambda d: d != date(2026, 10, 27)) == date(2026, 10, 26)  # a holiday: the day before


def test_expiry_exit_closes_at_the_first_expiry_after_entry(monkeypatch):
    days = [date(2026, 10, 22) + timedelta(days=k) for k in range(8)]
    days = [d for d in days if d.weekday() < 5]  # 22, 23, 26, 27, 28, 29 Oct
    rows = [(100, 101, 99, 100.5), (100.5, 102, 100, 101.8), (101.8, 103, 101.5, 102.5), (102.5, 104, 102, 103.5),
            (103.5, 105, 103, 104.5), (104.5, 106, 104, 105.5)]
    candles = [Candle(datetime(d.year, d.month, d.day, tzinfo=IST), *r) for d, r in zip(days, rows)]
    monkeypatch.setattr(adx, "indicators", lambda c, a, b: fake([20, 30, 31, 32, 33, 34], [20, 30, 31, 32, 33, 34], [18, 10, 9, 8, 8, 8]))
    monkeypatch.setattr(adx, "_trading_day", lambda d: True)
    # Signal on 26 Oct (candle 2, high 103), entry on 27 Oct (candle 3)... which is expiry day: it rolls to November's,
    # so the trade is still open at the end. An entry a day earlier exits at 27 Oct's close.
    got, _ = trades(candles, AdxRules(), Exits(book=0, expiry_exit=True))
    assert got[0].entry.index == 2 and got[0].fills[-1].reason == "expiry" and got[0].fills[-1].price == 103.5
