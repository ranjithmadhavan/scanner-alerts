from datetime import datetime, timedelta

from app.fractals import Fractal, last_sessions, target_for, target_reached, walk
from app.kite import Candle
from app.market import IST

T0 = datetime(2026, 9, 28, 9, 15, tzinfo=IST)


def series(*ohlc):
    """30-minute candles from (open, high, low, close) rows."""
    return [Candle(T0 + timedelta(minutes=30 * i), *row) for i, row in enumerate(ohlc)]


# Three candles forming a fractal high at 110 and a fractal low at 100 (both on the middle candle).
SETUP = [(104, 106, 102, 105), (105, 110, 100, 104), (104, 108, 101, 105)]


def keys(hits):
    return [(h.fractal.side, h.trigger) for h in hits]


def test_fractal_needs_three_closed_candles_and_allows_equal_highs():
    assert walk(series(*SETUP[:2]))[1] == []
    _, active = walk(series(*SETUP))
    assert [(f.side, f.level, f.at) for f in active] == [("high", 110, T0 + timedelta(minutes=30)),
                                                         ("low", 100, T0 + timedelta(minutes=30))]
    # Equal highs count (>=), and a flat run of them is one level.
    _, active = walk(series((1, 110, 90, 100), (100, 110, 95, 100), (100, 110, 96, 100), (100, 109, 97, 100)))
    assert [(f.side, f.level) for f in active if f.side == "high"] == [("high", 110)]


def test_touch_and_reject_when_the_candle_closes_back():
    hits, active = walk(series(*SETUP, (105, 111, 104, 109)))        # wick above 110, close below
    assert keys(hits) == [("high", "touch"), ("high", "reject")]
    assert hits[0].price == 110 and hits[1].price == 109 and hits[0].signal == "sell"
    assert hits[0].target == Fractal("low", 100, T0 + timedelta(minutes=30))   # sell aims at the fractal low
    assert [f.side for f in active] == ["low"]                        # the high is mitigated


def test_close_beyond_then_next_candle_closes_back_is_a_failed_break():
    hits, _ = walk(series(*SETUP, (105, 112, 104, 111), (111, 113, 108, 109)))
    assert keys(hits) == [("high", "touch"), ("high", "fail")] and hits[1].price == 109
    # If the next candle also closes beyond, it was a real break: nothing more is reported.
    hits, _ = walk(series(*SETUP, (105, 112, 104, 111), (111, 115, 110.5, 114)))
    assert keys(hits) == [("high", "touch")]


def test_fractal_low_mirrors_it_and_targets_the_fractal_high():
    hits, _ = walk(series(*SETUP, (104, 105, 99, 101)))
    assert keys(hits) == [("low", "touch"), ("low", "reject")]
    assert hits[0].signal == "buy" and hits[0].target.level == 110
    hits, _ = walk(series(*SETUP, (104, 105, 98, 99), (99, 102, 98.5, 101)))
    assert keys(hits) == [("low", "touch"), ("low", "fail")]


def test_gap_through_a_level_mitigates_it_silently():
    hits, active = walk(series(*SETUP, (112, 114, 111, 113)))         # opens above the fractal high
    assert hits == [] and [f.side for f in active] == ["low"]
    hits, active = walk(series(*SETUP, (98, 99, 96, 97)))             # opens below the fractal low
    assert hits == [] and [f.side for f in active] == ["high"]


def test_third_candle_touching_the_level_is_not_a_hit():
    hits, active = walk(series(*SETUP, (105, 110, 104, 106)))         # equal to the level, not beyond it
    assert hits == [] and len(active) == 2


def test_targets_and_backtest_outcome():
    lows = [Fractal("low", 95, T0), Fractal("low", 100, T0), Fractal("high", 120, T0), Fractal("high", 130, T0)]
    assert target_for("high", 110, lows).level == 100                 # nearest fractal low below price
    assert target_for("low", 110, lows).level == 120                  # nearest fractal high above price
    assert target_for("high", 90, lows) is None
    candles = series(*SETUP, (105, 111, 104, 109), (109, 109, 103, 104), (104, 105, 99.5, 101))
    hits, _ = walk(candles)
    assert target_reached(hits[1], candles).start == candles[5].start  # fell to the fractal low two candles later
    assert target_reached(walk(candles[:5])[0][1], candles[:5]) is None


def test_last_sessions_keeps_recent_days():
    candles = [Candle(T0 + timedelta(days=d), 1, 2, 0, 1) for d in range(5)]
    assert [c.start.day for c in last_sessions(candles, 2)] == [1, 2]
    assert last_sessions([], 10) == []
