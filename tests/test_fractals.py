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


def test_close_beyond_then_two_closes_back_is_a_failed_break():
    broke = (105, 112, 104, 111)                                        # closes above 110
    hits, _ = walk(series(*SETUP, broke, (111, 113, 108, 109), (109, 110.5, 107, 108)))
    assert keys(hits) == [("high", "touch"), ("high", "fail")] and hits[1].price == 108
    assert hits[1].candle.start == T0 + timedelta(minutes=150)          # decided at the third candle's close
    # One close back isn't enough...
    assert keys(walk(series(*SETUP, broke, (111, 113, 108, 109)))[0]) == [("high", "touch")]
    # ...and nor is closing back, then above again.
    assert keys(walk(series(*SETUP, broke, (111, 113, 108, 109), (109, 112, 108.5, 110.5)))[0]) == [("high", "touch")]
    # If the next candle also closes beyond, it was a real break: nothing more is reported.
    hits, _ = walk(series(*SETUP, (105, 112, 104, 111), (111, 115, 110.5, 114)))
    assert keys(hits) == [("high", "touch")]


def test_fractal_low_mirrors_it_and_targets_the_fractal_high():
    hits, _ = walk(series(*SETUP, (104, 105, 99, 101)))
    assert keys(hits) == [("low", "touch"), ("low", "reject")]
    assert hits[0].signal == "buy" and hits[0].target.level == 110
    hits, _ = walk(series(*SETUP, (104, 105, 98, 99), (99, 102, 98.5, 101), (101, 101.8, 99.5, 101.2)))
    assert keys(hits) == [("low", "touch"), ("low", "fail")]


def test_gap_through_a_fractal_flips_its_role_instead_of_reporting():
    # Opens above the fractal high: nothing is reported, and the high is now support underneath price.
    gap_up = series(*SETUP, (112, 114, 111, 113))
    hits, active = walk(gap_up)
    assert hits == [] and [(f.side, f.role, f.flipped) for f in active] == [("high", "support", True), ("low", "support", False)]
    # Price comes back down to it, dips under and closes back above: a potential buy off the old high.
    hits, active = walk(gap_up + series(*SETUP, (0,) * 4, (113, 113.5, 109.5, 111))[4:])
    assert keys(hits) == [("high", "touch"), ("high", "reject")]
    assert hits[1].signal == "buy" and hits[1].fractal.flipped and hits[1].key.endswith(":flipped:reject")
    assert [(f.side, f.level) for f in active] == [("low", 100), ("high", 114)]   # the old high is spent; the gap candle left a new one

    # Opens below the fractal low: it becomes resistance overhead. A sweep of it from below is a potential sell,
    # aimed at the nearest support underneath (none here).
    gap_down = series(*SETUP, (98, 99, 96, 97))
    hits, active = walk(gap_down)
    assert hits == [] and [(f.side, f.role) for f in active] == [("high", "resistance"), ("low", "resistance")]
    hits, _ = walk(gap_down + series(*SETUP, (0,) * 4, (97, 100.6, 96.5, 99.5))[4:])
    assert keys(hits) == [("low", "touch"), ("low", "reject")]
    assert hits[1].signal == "sell" and hits[1].target is None
    # Closing above it and then back below is a failed break, also a potential sell.
    hits, _ = walk(gap_down + series(*SETUP, (0,) * 4, (97, 101, 96.5, 100.5), (100.5, 100.8, 98, 98.5), (98.5, 99.5, 97, 98))[4:])
    assert keys(hits) == [("low", "touch"), ("low", "fail")] and hits[1].signal == "sell"

    # Without a gap nothing changes: coming down onto the low from above is still a potential buy.
    hits, _ = walk(series(*SETUP, (104, 105, 99, 101)))
    assert hits[1].signal == "buy" and not hits[1].fractal.flipped

    # A second gap, back the other way, flips it back.
    _, active = walk(gap_down + series(*SETUP, (0,) * 4, (102, 103, 101.5, 102.5))[4:])
    assert [(f.level, f.role, f.flipped) for f in active] == [
        (110, "resistance", False), (100, "support", False), (96, "support", False)]   # 96: a new low left by the gap candle


def test_third_candle_touching_the_level_is_not_a_hit():
    hits, active = walk(series(*SETUP, (105, 110, 104, 106)))         # equal to the level, not beyond it
    assert hits == [] and len(active) == 2


def test_targets_and_backtest_outcome():
    lows = [Fractal("low", 95, T0), Fractal("low", 100, T0), Fractal("high", 120, T0), Fractal("high", 130, T0)]
    assert target_for("sell", 110, lows).level == 100                 # nearest support below price
    assert target_for("buy", 110, lows).level == 120                  # nearest resistance above price
    assert target_for("sell", 90, lows) is None
    gapped_low = Fractal("low", 105, T0, flipped=True)                # a low that price gapped below: resistance now
    assert target_for("buy", 101, lows + [gapped_low]) is gapped_low and target_for("sell", 110, [gapped_low]) is None
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

    # A failed break's stop covers all three of its candles; a buy mirrors everything on the lows.
    candles = series(*SETUP, (105, 112, 104, 111), (111, 111.5, 108, 109), (109, 111.8, 108, 110), (110, 112.5, 109, 112))
    failed = [h for h in walk(candles)[0] if h.trigger == "fail"][0]
    result = outcome(failed, candles)
    assert failed.candle.start == candles[5].start and result.stop == 112 and result.stopped.start == candles[6].start
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
    assert keys(hits) == [("high", "touch"), ("high", "reject"), ("high", "confirm")]   # the next 5 min close holds too
    assert hits[1].candle.start == T0 + timedelta(minutes=95) and hits[1].price == 109.4
    assert [f.side for f in active] == ["low"]

    # Closes above on one 5-minute candle, back below on the next two: a failed break at the third close.
    hits, _ = replay(found, [five(90, 108, 111, 107.5, 110.5), five(95, 110.5, 110.9, 109, 109.2), five(100, 109.2, 109.9, 108, 108.4)])
    assert keys(hits) == [("high", "touch"), ("high", "fail")] and hits[1].candle.start == T0 + timedelta(minutes=100)

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


def test_minimum_candles_between_fractal_and_the_candle_that_takes_it():
    from app.fractals import candles_between
    quiet = (105, 107, 103, 106)
    sweep = (105, 111, 104, 109)
    soon = series(*SETUP, sweep)                                       # only the fractal's third candle in between
    assert keys(walk(soon)[0]) == [("high", "touch"), ("high", "reject")]
    hits, active = walk(soon, min_between=5)
    assert hits == [] and [f.side for f in active] == ["low"]          # dropped, and no longer in play
    later = series(*SETUP, quiet, quiet, quiet, quiet, sweep)          # third candle + 4 quiet ones = 5 between
    assert keys(walk(later, min_between=5)[0]) == [("high", "touch"), ("high", "reject")]
    assert walk(later, min_between=6)[0] == []

    # With smaller trigger candles the count is still in fractal candles.
    half_hour = series(*SETUP, quiet, quiet)
    closes = [c.start + timedelta(minutes=30) for c in half_hour]
    found = find(half_hour, lambda c: c.start + timedelta(minutes=30))
    high = [f for _, f in found if f.side == "high"][0]
    assert candles_between(high, T0 + timedelta(minutes=90), closes) == 1      # right after the third candle
    assert candles_between(high, T0 + timedelta(minutes=155), closes) == 3     # inside the next, still-forming half hour
    five = [Candle(T0 + timedelta(minutes=155), 106, 111, 105, 109)]
    assert keys(replay(found, five, closes, 3)[0]) == [("high", "touch"), ("high", "reject")]
    assert replay(found, five, closes, 4)[0] == []


def test_a_sweep_that_holds_needs_the_next_candle_to_close_back_too():
    sweep = (105, 111, 104, 109)                                       # wicks above 110, closes back below
    held = series(*SETUP, sweep, (109, 110.5, 107, 108))               # next candle pokes above but closes below too
    hits, _ = walk(held)
    assert keys(hits) == [("high", "touch"), ("high", "reject"), ("high", "confirm")]
    confirm = hits[2]
    assert confirm.candle.start == held[4].start and confirm.price == 108 and confirm.signal == "sell"
    assert confirm.target.level == 100 and confirm.key.endswith(":confirm")
    assert outcome(confirm, held).stop == 111                          # the stop covers both candles
    # The next candle closing above the level means the sweep didn't hold.
    assert keys(walk(series(*SETUP, sweep, (109, 112, 108.5, 111)))[0]) == [("high", "touch"), ("high", "reject")]
    # A failed break is never also a held sweep: its first candle closed beyond the level.
    assert keys(walk(series(*SETUP, (105, 112, 104, 111), (111, 111.5, 108, 109), (109, 109.5, 107, 108)))[0]) == [("high", "touch"), ("high", "fail")]
    # Mirrored on a fractal low: a potential buy.
    hits, _ = walk(series(*SETUP, (104, 105, 99, 101), (101, 102, 99.6, 100.5)))
    assert keys(hits) == [("low", "touch"), ("low", "reject"), ("low", "confirm")] and hits[2].signal == "buy"


def test_fractal_alerts_can_report_only_at_the_days_high_or_low():
    from app import fractals, scanner
    from app.routes.alerts import _fractal_fields
    fields, error = _fractal_fields("30m", "both", ["reject"], "", 5, "1")
    assert error is None and fields["extremes_only"] is True
    assert _fractal_fields("30m", "both", ["reject"], "", 5, "")[0]["extremes_only"] is False
    fri, mon = datetime(2026, 10, 2, 9, 15, tzinfo=IST), datetime(2026, 10, 5, 9, 15, tzinfo=IST)
    candles = [Candle(fri, 100, 101, 95, 100), Candle(fri + timedelta(minutes=30), 100, 103, 97, 102),
               Candle(mon, 102, 103, 94, 99)]
    day_low, higher_low = Fractal("low", 95.0, fri), Fractal("low", 97.0, fri + timedelta(minutes=30))
    alert = {"sides": "both", "triggers": ["reject"], "extremes_only": True}
    assert scanner.fractal_wanted(alert, fractals.Hit(day_low, "reject", candles[2], 2, 99), candles)       # Friday's low
    assert not scanner.fractal_wanted(alert, fractals.Hit(higher_low, "reject", candles[2], 2, 99), candles)
    assert scanner.fractal_wanted({**alert, "extremes_only": False}, fractals.Hit(higher_low, "reject", candles[2], 2, 99), candles)
    # A live touch (no index) is judged the same way.
    touch = fractals.Hit(day_low, "touch", Candle(mon + timedelta(minutes=30), 99, 99, 94.5, 96), -1, 95.0)
    assert scanner.fractal_wanted({**alert, "triggers": ["touch"]}, touch, candles)


def test_any_earlier_days_high_or_low_counts_not_just_yesterdays():
    """A 30-minute fractal that was the low of a day several sessions back still counts when price comes back to it;
    one that was only a dip inside its day doesn't."""
    from app import fractals
    days = [datetime(2026, 9, 30, 9, 15, tzinfo=IST), datetime(2026, 10, 1, 9, 15, tzinfo=IST), datetime(2026, 10, 5, 9, 15, tzinfo=IST)]
    bars = {  # (open, high, low, close) per 30-minute candle
        days[0]: [(105, 107, 103, 104), (104, 105, 98, 101), (101, 104, 100, 103), (103, 104, 101, 102), (102, 103, 99.5, 101), (101, 103, 100, 102)],
        days[1]: [(102, 106, 101, 105), (105, 108, 104, 107), (107, 109, 105, 108)],
        days[2]: [(108, 108, 102, 103), (103, 104, 97, 101)],
    }
    candles = [Candle(d + timedelta(minutes=30 * k), *b) for d in days for k, b in enumerate(bars[d])]
    hits, _ = fractals.walk(candles)
    taken = {h.fractal.level: fractals.at_day_extreme(h, candles) for h in hits if h.trigger == "reject" and h.fractal.side == "low"}
    assert taken[98] is True        # 30 Sept's low, swept on 5 Oct, two sessions later
    assert taken[99.5] is False     # a later dip on 30 Sept, above that day's low


def test_a_sweep_held_only_by_the_next_days_gap_is_not_a_signal():
    """NESTLEIND, 6-7 Oct: the 1:45 PM fractal high (1,339.80) was swept by 6 Oct's last half-hour (1,340.00,
    closed back), and 7 Oct gapped down to 1,326. The gap is not price holding below the level."""
    from app import fractals, scanner
    d6, d7 = datetime(2026, 10, 6, 13, 15, tzinfo=IST), datetime(2026, 10, 7, 9, 15, tzinfo=IST)
    candles = [Candle(d6, 1322.3, 1337.0, 1322.1, 1329.0), Candle(d6 + timedelta(minutes=30), 1328.7, 1339.8, 1327.7, 1336.4),
               Candle(d6 + timedelta(minutes=60), 1336.8, 1337.7, 1331.1, 1334.3), Candle(d6 + timedelta(minutes=90), 1334.4, 1340.0, 1332.3, 1338.9),
               Candle(d7, 1326.3, 1333.0, 1320.7, 1324.4)]
    hits, _ = fractals.walk(candles)
    held = [h for h in hits if h.trigger == "confirm" and h.fractal.level == 1339.8]
    assert len(held) == 1 and not fractals.within_one_session(held[0], candles)
    assert not scanner.fractal_wanted({"sides": "both", "triggers": ["confirm"]}, held[0], candles)
    # The same sweep with its hold on the same day still counts.
    same_day = candles[:4] + [Candle(d6 + timedelta(minutes=120), 1338.9, 1339.0, 1330.0, 1331.0)]
    held = [h for h in fractals.walk(same_day)[0] if h.trigger == "confirm" and h.fractal.level == 1339.8]
    assert fractals.within_one_session(held[0], same_day)
    assert scanner.fractal_wanted({"sides": "both", "triggers": ["confirm"]}, held[0], same_day)


def test_intraday_square_off_closes_a_trade_the_day_it_was_taken():
    from datetime import time
    from app import fractals
    d1, d2 = datetime(2026, 10, 5, 13, 45, tzinfo=IST), datetime(2026, 10, 6, 9, 15, tzinfo=IST)
    candles = [Candle(d1, 100, 101, 98, 100.5),                                   # the signal candle: a buy at 100.5, stop 98
               Candle(d1 + timedelta(minutes=30), 100.5, 102, 100, 101.5),        # 2:15
               Candle(d1 + timedelta(minutes=60), 101.5, 103, 101, 102.5),        # 2:45, closes 3:15
               Candle(d1 + timedelta(minutes=90), 102.5, 104, 102, 103.5),        # 3:15: after the square-off
               Candle(d2, 104, 111, 103, 110)]                                    # next day reaches the target
    buy = fractals.Hit(Fractal("low", 99.0, d1 - timedelta(days=1)), "reject", candles[0], 0, 100.5,
                       target=Fractal("high", 110.0, d1 - timedelta(days=2)))
    spill = outcome(buy, candles)
    assert spill.result == "target" and spill.reached is candles[4] and spill.points == 9.5       # carried into the next day
    intraday = outcome(buy, candles, time(15, 15))
    assert intraday.result == "squared" and intraday.squared is candles[2] and intraday.exit == 102.5
    assert intraday.points == 2.0                                                                    # 100.5 to 102.5
    # A sell is scored the other way round; the stop and the target still end it early.
    sell = fractals.Hit(Fractal("high", 101.0, d1 - timedelta(days=1)), "reject", candles[0], 0, 100.5)
    assert outcome(sell, candles, time(15, 15)).result == "stop"                                     # 2:15 traded above 101
    quiet = [candles[0], Candle(d1 + timedelta(minutes=30), 100.5, 100.9, 99.5, 99.8), Candle(d1 + timedelta(minutes=60), 99.8, 100, 99, 99.2), candles[3]]
    squared = outcome(sell, quiet, time(15, 15))
    assert squared.result == "squared" and round(squared.points, 2) == 1.3                           # no target, still a trade: 100.5 to 99.2
    assert outcome(buy, candles, time(14, 0)).result == "skipped"                                    # nothing left to trade before 2:00
    late = fractals.Hit(buy.fractal, "reject", candles[3], 3, 103.5, target=buy.target)
    assert outcome(late, candles, time(15, 15)).result == "skipped"                                  # signalled at the square-off
    # The day still in progress: not squared off until the square-off candle has closed.
    assert outcome(buy, candles[:2], time(15, 15)).result == "open"
    assert outcome(buy, candles[:3], time(15, 15)).result == "squared"                               # 2:45 + 30 min reaches 3:15
