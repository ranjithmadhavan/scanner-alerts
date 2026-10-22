from datetime import datetime, time, timedelta

import pytest

from app.crossover import Entry, Exits, Rules, Setup, ema, simulate, sma, stats, trades, walk
from app.kite import Candle
from app.market import IST

T0 = datetime(2026, 10, 1, 9, 15, tzinfo=IST)


def series(*ohlc, minutes=15):
    """Candles from (open, high, low, close) rows, `minutes` apart on one day."""
    return [Candle(T0 + timedelta(minutes=minutes * i), *row) for i, row in enumerate(ohlc)]


def daily(*ohlc):
    return [Candle(T0 + timedelta(days=i), *row) for i, row in enumerate(ohlc)]


GREEN = (100, 102, 99, 101)
RED = (101, 102, 98, 99)  # high 102, low 98


def test_sma_and_ema():
    assert sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]
    e = ema([1, 2, 3, 4], 2)
    assert e[:2] == [None, 1.5] and e[2] == pytest.approx(3 * 2 / 3 + 1.5 / 3) and e[3] == pytest.approx(4 * 2 / 3 + e[2] / 3)
    assert ema([1, 2], 3) == [None, None]


def test_red_candle_after_bullish_crossover_is_broken_for_an_entry():
    candles = series(GREEN, GREEN, RED, (100, 101, 97, 99), (99, 103, 98.5, 102))
    fast = [1, 3, 3, 3, 3]
    slow = [2, 2, 2, 2, 2]
    w = walk(candles, Rules(), fast, slow)
    assert w.crossovers == [(1, "buy")]
    # Candle 2 is red: trigger at its high 102. Candle 3 is red too (high 101 doesn't break 102): it takes over
    # as the trigger at 101. Candle 4 trades 103 > 101: entry.
    assert [(e.setup.trigger, e.setup.level, e.index, e.gap) for e in w.entries] == [(3, 101, 4, False)]
    assert w.entries[0].setup.stop == 97 and w.entries[0].setup.crossed == 1
    assert w.pending is None and w.trend == "buy"


def test_crossover_candle_is_never_the_pullback_and_trend_counts_from_the_first_reading():
    candles = series(RED, RED, GREEN)
    # Already above at the start: an uptrend with no crossover seen; candle 0 is the first reading, 1 sets up.
    w = walk(candles, Rules(), [3, 3, 3], [2, 2, 2])
    assert w.crossovers == [] and [(e.setup.trigger, e.setup.crossed) for e in w.entries] == []
    assert w.pending.trigger == 1 and w.pending.crossed is None
    # Crossing on a red candle: that candle doesn't set up.
    w = walk(series(GREEN, RED, (99.5, 100, 98, 99)), Rules(), [1, 3, 3], [2, 2, 2])
    assert w.pending.trigger == 2


def test_opposite_crossover_cancels_and_starts_sells():
    candles = series(GREEN, RED, (99, 100, 97, 98), (98, 99, 96, 99.5), (99.5, 100, 95, 96))
    fast = [3, 3, 1, 1, 1]
    slow = [2, 2, 2, 2, 2]
    w = walk(candles, Rules(), fast, slow)
    # Candle 1 red sets a buy; candle 2 crosses down (cancels it); candle 3 green sets a sell at its low 96;
    # candle 4 trades 95: a sell entry.
    assert w.crossovers == [(2, "sell")]
    assert [(e.side, e.setup.trigger, e.setup.level, e.setup.stop, e.index) for e in w.entries] == [("sell", 3, 96, 99, 4)]


def test_gap_and_sides_and_reentry():
    candles = series(GREEN, RED, (104, 106, 103, 105), (105, 106, 101, 102), (102, 107, 101, 106))
    w = walk(candles, Rules(), [3] * 5, [2] * 5)
    # Candle 2 opens at 104, above the trigger high 102: a gap entry. It isn't a pullback itself (and is green).
    # Candle 3 is red: a new setup at 106, broken by candle 4.
    assert [(e.setup.trigger, e.index, e.gap) for e in w.entries] == [(1, 2, True), (3, 4, False)]
    assert walk(candles, Rules(sides="sell"), [3] * 5, [2] * 5).entries == []


def test_entry_candle_does_not_set_up_the_next_trade():
    candles = series(GREEN, RED, (101, 103, 99, 100), (100, 100.5, 99, 100.2))
    w = walk(candles, Rules(), [3] * 4, [2] * 4)
    assert [e.index for e in w.entries] == [2] and w.pending is None


def test_touch_rule():
    candles = series(GREEN, (101, 102, 98, 99), (99, 100, 95, 96), (96, 101, 95.5, 100))
    fast = [3, 99, 99, 99]
    slow = [2, 96, 96, 96]
    # Candle 1 (low 98) touches the fast MA (99) but not the slow one (96); candle 2 (low 95) touches both.
    assert walk(candles, Rules(touch="fast"), fast, slow).entries[0].setup.trigger == 2
    w = walk(candles[:2], Rules(touch="fast"), fast[:2], slow[:2])
    assert w.pending.trigger == 1
    assert walk(candles[:2], Rules(touch="slow"), fast[:2], slow[:2]).pending is None
    assert walk(candles, Rules(touch="slow"), fast, slow).pending is None and \
        walk(candles, Rules(touch="slow"), fast, slow).entries[0].setup.trigger == 2


def _entry(candles, level=102, stop=98, index=2, gap=False, side="buy"):
    return Entry(Setup(side, 1, level, stop, 0), index, gap)


FLAT = [None, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0]  # moving averages far away unless a test sets them


def test_stop_target_and_both_in_one_candle():
    # Entry 102, stop 98 (risk 4), 2R target 110.
    c = series(GREEN, RED, (101, 103, 100, 102.5), (102.5, 111, 101, 109))
    t = simulate(_entry(c), c, FLAT, FLAT, Exits(rr=2))
    assert (t.price, t.stop, t.risk, t.target) == (102, 98, 4, 110)
    assert t.status == "closed" and t.r == pytest.approx(2) and t.fills[0].index == 3 and t.reason == "Target"
    c = series(GREEN, RED, (101, 103, 100, 102.5), (102.5, 111, 97, 109))
    t = simulate(_entry(c), c, FLAT, FLAT, Exits(rr=2))
    assert t.r == pytest.approx(-1) and t.reason == "SL"
    # A gap down through the stop fills at the open.
    c = series(GREEN, RED, (101, 103, 100, 102.5), (96, 97, 95, 96))
    assert simulate(_entry(c), c, FLAT, FLAT, Exits()).r == pytest.approx(-1.5)


def test_entry_candle_is_read_cautiously():
    # The entry candle's low (97) is under the stop: it may have come before the entry, but it counts as stopped.
    c = series(GREEN, RED, (99, 103, 97, 101), (101, 101.5, 99, 100))
    t = simulate(_entry(c), c, FLAT, FLAT, Exits())
    assert t.r == pytest.approx(-1) and t.fills[0].index == 2
    # Its high passing the target (110) doesn't count unless it closed there.
    c = series(GREEN, RED, (99, 111, 99.5, 105), (105, 106, 104, 105.5))
    assert simulate(_entry(c), c, FLAT, FLAT, Exits(rr=2)).status == "open"
    c = series(GREEN, RED, (99, 111, 99.5, 110.5))
    assert simulate(_entry(c), c, FLAT, FLAT, Exits(rr=2)).r == pytest.approx(2)


def test_gap_fills():
    c = series(GREEN, RED, (104, 112, 103, 111), (111, 115, 110, 114))
    e = _entry(c, gap=True)
    t = simulate(e, c, FLAT, FLAT, Exits(rr=2, fill="open"))
    # At the open 104: risk 6, target 116, not reached.
    assert (t.price, t.risk, t.target, t.status) == (104, 6, 116, "open")
    t = simulate(e, c, FLAT, FLAT, Exits(rr=2, fill="level"))
    # At the level 102: target 110. The entry candle closes at 111, past it.
    assert t.price == 102 and t.r == pytest.approx(2)
    assert simulate(e, c, FLAT, FLAT, Exits(fill="skip")).status == "skipped"


def test_partial_booking_then_candle_trail_and_breakeven():
    c = series(GREEN, RED, (101, 103, 100, 102.5), (103, 107, 102, 106), (106, 109, 105, 108), (108, 108.5, 104, 104.5))
    # 1R target 106 books half on candle 3. Trail by previous candle's low: 102 after candle 3, 105 after candle 4;
    # candle 5 trades 104 < 105, out at 105.
    t = simulate(_entry(c), c, FLAT, FLAT, Exits(rr=1, book=50, trail="candle"))
    assert [(f.part, f.price, f.reason) for f in t.fills] == [(0.5, 106, "target"), (0.5, 105, "trail")]
    assert t.r == pytest.approx(0.5 * 1 + 0.5 * 3 / 4) and t.reason == "Target + Trailing SL"
    # Breakeven only: after 1R the stop is the entry, 102; candle 5 doesn't reach it. No target.
    t = simulate(_entry(c), c, FLAT, FLAT, Exits(book=0, breakeven=True))
    assert t.status == "open" and t.target is None
    c2 = c + [Candle(c[-1].start + timedelta(minutes=15), 104, 104, 101, 101.5)]
    t = simulate(_entry(c2), c2, FLAT, FLAT, Exits(book=0, breakeven=True))
    assert t.fills[-1].reason == "breakeven" and t.r == 0


def test_step_trail():
    # Entry 102, risk 4. Highs reach 110.5 (2.1R) by candle 4: stop to 106 (+1R). Candle 5 trades 105.
    c = series(GREEN, RED, (101, 103, 100, 102.5), (103, 107, 102, 106), (106, 110.5, 105, 109), (109, 109, 105, 105.5))
    t = simulate(_entry(c), c, FLAT, FLAT, Exits(book=0, trail="step"))
    assert t.fills[-1].price == 106 and t.fills[-1].reason == "trail" and t.r == pytest.approx(1)


def test_slow_ma_stop_fast_ma_trail_and_ma_exit():
    c = series(GREEN, RED, (101, 103, 100, 102.5), (102.5, 104, 101, 103), (103, 104, 99, 99.5))
    slow = [None, 100, 100, 100, 100]
    fast = [None, 101, 101, 103.5, 103.5]
    # Slow MA stop: risk = 102 - 100 (the MA at the close before entry) = 2; candle 4 closes 99.5 < 100.
    t = simulate(_entry(c), c, fast, slow, Exits(stop="slow_ma", book=0))
    assert (t.stop, t.risk) == (100, 2) and t.fills[0].reason == "slow_ma" and t.r == pytest.approx(-1.25)
    # Fast MA trail: candle 3 closes 103 < 103.5.
    t = simulate(_entry(c), c, fast, slow, Exits(book=0, trail="fast_ma"))
    assert t.fills[0].index == 3 and t.fills[0].price == 103
    # Close beyond the slow MA with a candle stop (98) that isn't hit.
    t = simulate(_entry(c), c, fast, slow, Exits(book=0, ma_exit=True))
    assert t.fills[0].reason == "ma_exit" and t.fills[0].price == 99.5
    # No room under the slow MA: not a trade.
    assert simulate(_entry(c), c, fast, [None, 103, 103, 103, 103], Exits(stop="slow_ma")).status == "skipped"


def test_sell_mirrors_buy():
    c = series((100, 101, 98, 99), (99, 102, 98.5, 101), (100, 100.5, 97, 97.5), (97.5, 98, 92, 93))
    e = Entry(Setup("sell", 1, 98.5, 102, 0), 2, False)
    t = simulate(e, c, FLAT, FLAT, Exits(rr=1.5))
    assert (t.risk, t.target) == (3.5, pytest.approx(93.25)) and t.r == pytest.approx(1.5)


def test_square_off():
    c = [Candle(datetime(2026, 10, 1, 14, 30, tzinfo=IST) + timedelta(minutes=15 * i), x.open, x.high, x.low, x.close)
         for i, x in enumerate(series(GREEN, RED, (101, 103, 100, 102.5), (102.5, 104, 101, 103.5)))]
    # Candles at 14:30, 14:45, 15:00 (entry), 15:15. Square-off at 15:15: the 15:00 candle is the last; out at its close.
    t = simulate(_entry(c), c, FLAT, FLAT, Exits(book=0, square_off=time(15, 15)), minutes=15)
    assert t.fills[0].reason == "square_off" and t.fills[0].price == 102.5 and t.fills[0].index == 2
    # An entry at or after the square-off time isn't traded.
    assert simulate(_entry(c, index=3), c, FLAT, FLAT, Exits(square_off=time(15, 15)), minutes=15).status == "skipped"
    # A day still in progress before the square-off: open.
    t = simulate(_entry(c), c[:3], FLAT, FLAT, Exits(book=0, square_off=time(15, 30)), minutes=15)
    assert t.status == "open"


def test_one_position_at_a_time_and_stats():
    up = [(100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(40)]
    candles = daily(*up)
    rules = Rules("sma", 2, "sma", 5)
    got, w = trades(candles, rules, Exits())
    assert got == [] and w.trend == "buy"
    # A pullback, a break, a run to target, then a second setup.
    rows = up[:10] + [(110, 110.5, 108, 108.5), (108.5, 111, 108.2, 110.8), (110.8, 116, 110.5, 115.5),
                      (115.5, 116, 114, 114.2), (114.2, 117, 114, 116.5), (116.5, 125, 116, 124)]
    candles = daily(*rows)
    got, _ = trades(candles, rules, Exits(rr=2))
    assert [(t.entry.index, t.status) for t in got] == [(11, "closed"), (14, "closed")]
    assert got[0].r == pytest.approx(2) and got[1].r == pytest.approx(2)
    s = stats([t for t in got if t.status == "closed"])
    assert s["trades"] == 2 and s["win_rate"] == 100 and s["total_r"] == pytest.approx(4) and s["max_dd"] == 0
    s = stats([type("T", (), {"r": r, "pct": 0})() for r in (1, -1, -1, 2)])
    assert s["max_dd"] == 2 and s["profit_factor"] == pytest.approx(1.5)
