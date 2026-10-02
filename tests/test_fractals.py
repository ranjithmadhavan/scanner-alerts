from datetime import datetime, timedelta

from app.fractals import Fractal, find, last_sessions, outcome, replay, target_for, trigger_choices, walk
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
    won = outcome(hits[1], candles)                                    # the sweep at 111, target 100
    assert (won.result, won.stop, won.stopped) == ("target", 111, None)
    assert won.reached.start == candles[5].start                      # fell to the fractal low two candles later
    still_open = outcome(walk(candles[:5])[0][1], candles[:5])
    assert (still_open.result, still_open.reached, still_open.stopped) == ("open", None, None)


def test_stop_loss_is_the_extreme_made_while_taking_the_fractal():
    sweep = (105, 111, 104, 109)                                      # sell: fractal high 110 swept, stop at 111
    # Price goes above 111 before reaching 100, then falls to it anyway.
    candles = series(*SETUP, sweep, (109, 111.5, 108, 110), (110, 110, 99, 101))
    result = outcome(walk(candles)[0][1], candles)
    assert (result.result, result.stop) == ("stop", 111) and result.stopped.start == candles[4].start
    assert result.reached.start == candles[5].start and result.reached_after_stop
    # Stopped out and never gets there.
    candles = series(*SETUP, sweep, (109, 112, 108, 111))
    result = outcome(walk(candles)[0][1], candles)
    assert (result.result, result.reached, result.reached_after_stop) == ("stop", None, False)
    # One candle trades both the stop and the target: counted as the stop.
    candles = series(*SETUP, sweep, (109, 112, 99, 105))
    assert outcome(walk(candles)[0][1], candles).result == "stop"
    # Touching the stop price exactly isn't beyond it.
    candles = series(*SETUP, sweep, (109, 111, 99.5, 100))
    assert outcome(walk(candles)[0][1], candles).result == "target"

    # A failed break's stop covers both of its candles; a buy mirrors everything on the lows.
    candles = series(*SETUP, (105, 112, 104, 111), (111, 111.5, 108, 109), (109, 111.8, 108, 110), (110, 112.5, 109, 112))
    failed = [h for h in walk(candles)[0] if h.trigger == "fail"][0]
    result = outcome(failed, candles)
    assert result.stop == 112 and result.stopped.start == candles[6].start
    candles = series(*SETUP, (104, 105, 99, 101), (101, 102, 98.5, 100))
    buy = outcome(walk(candles)[0][1], candles)
    assert (buy.stop, buy.result) == (99, "stop")


def test_points_earned_and_lost():
    sweep = (105, 111, 104, 109)                                      # sell at the 109 close, stop 111, target 100
    candles = series(*SETUP, sweep, (109, 109, 99.5, 101))
    won = outcome(walk(candles)[0][1], candles)
    assert (won.entry, won.target, won.points) == (109, 100, 9)       # entry to target
    candles = series(*SETUP, sweep, (109, 111.5, 108, 110), (110, 110, 99, 101))
    lost = outcome(walk(candles)[0][1], candles)
    assert lost.points == -2                                          # entry to stop, even though the target came later
    touch = outcome(walk(candles)[0][0], candles)
    assert (touch.entry, touch.points) == (110, -1)                   # a touch is entered at the fractal level
    candles = series(*SETUP, sweep)
    assert outcome(walk(candles)[0][1], candles).points is None       # still open
    candles = series(*SETUP, (104, 105, 99, 101), (101, 102, 98.5, 100))
    buy = outcome(walk(candles)[0][1], candles)
    assert (buy.entry, buy.stop, buy.points) == (101, 99, -2)


def test_last_sessions_keeps_recent_days():
    candles = [Candle(T0 + timedelta(days=d), 1, 2, 0, 1) for d in range(5)]
    assert [c.start.day for c in last_sessions(candles, 2)] == [1, 2]
    assert last_sessions([], 10) == []


def test_sweep_can_be_judged_on_smaller_candles_than_the_fractal():
    half_hour = series(*SETUP)                                        # fractal high 110, in being at 10:45
    found = find(half_hour, lambda c: c.start + timedelta(minutes=30))
    assert [(t, f.side) for t, f in found] == [(T0 + timedelta(minutes=90), "high"), (T0 + timedelta(minutes=90), "low")]

    def five(minute, *ohlc):
        return Candle(T0 + timedelta(minutes=minute), *ohlc)

    # A 5-minute candle wicks above 110 and closes back: swept on the 5-minute close, not half an hour later.
    hits, active = replay(found, [five(90, 105, 109, 104, 108), five(95, 108, 110.6, 107.5, 109.4), five(100, 109.4, 109.8, 108, 108.5)])
    assert keys(hits) == [("high", "touch"), ("high", "reject")]
    assert hits[1].candle.start == T0 + timedelta(minutes=95) and hits[1].price == 109.4
    assert [f.side for f in active] == ["low"]

    # Closes above on one 5-minute candle, back below on the next: a failed break at the second close.
    hits, _ = replay(found, [five(90, 108, 111, 107.5, 110.5), five(95, 110.5, 110.9, 109, 109.2)])
    assert keys(hits) == [("high", "touch"), ("high", "fail")] and hits[1].candle.start == T0 + timedelta(minutes=95)

    # Candles from before the fractal existed can't take it.
    hits, active = replay(found, [five(60, 105, 112, 104, 111)])
    assert hits == [] and len(active) == 2


def test_trigger_candle_choices_fit_inside_the_fractal_timeframe():
    assert trigger_choices("30m") == ["1m", "3m", "5m", "10m", "15m"]
    assert trigger_choices("15m") == ["1m", "3m", "5m"]               # 10 doesn't fit into 15
    assert trigger_choices("1h") == ["1m", "3m", "5m", "10m", "15m", "30m"]
    assert trigger_choices("3m") == ["1m"] and trigger_choices("5m") == ["1m"]
    assert trigger_choices("10m") == ["1m", "5m"]                     # 3 doesn't fit into 10
    assert trigger_choices("1d") == ["1m", "3m", "5m", "10m", "15m", "30m", "1h"]
