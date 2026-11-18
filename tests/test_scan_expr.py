"""Custom scans: indicators, and checking, describing and evaluating a scan."""

import json
from datetime import datetime, timedelta

import pytest

from app import ta
from app.kite import Candle
from app.market import IST
from app.scan_expr import Stock, describe, needs, passes, readings, validate, weekly


def day_candles(closes, start=datetime(2026, 1, 5, 9, 15, tzinfo=IST), highs=None, lows=None, volume=1000):
    """One daily candle per close on weekdays from Monday 5 Jan 2026."""
    out, d = [], start
    for i, c in enumerate(closes):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append(Candle(d, c, highs[i] if highs else c + 1, lows[i] if lows else c - 1, c, volume))
        d += timedelta(days=1)
    return out


def P(field, tf="", ago=0, pct=100):
    return {"kind": "price", "field": field, "tf": tf, "ago": ago, "pct": pct}


def I(ind, out="", tf="", ago=0, pct=100, **params):
    return {"kind": "ind", "ind": ind, "params": params, "out": out, "tf": tf, "ago": ago, "pct": pct}


def N(v):
    return {"kind": "num", "value": v}


def scan(*items, op="all"):
    return validate({"op": op, "items": [{"lhs": a, "cmp": c, "rhs": b} if not isinstance(a, dict) or "items" not in a else a
                                         for a, c, b in items]})


# ---- indicators --------------------------------------------------------------------------------

def test_rsi_matches_wilders_worked_example():
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28,
              46.00, 46.03]
    r = ta.rsi(closes, 14)
    assert r[13] is None
    # StockCharts' worked example shows 70.53, 66.32, 66.55 from rounded averages; unrounded (as TradingView) the first
    # is 100 - 100 / (1 + 0.2386 / 0.1000).
    assert [round(x, 2) for x in r[14:]] == [70.46, 66.25, 66.48]
    assert ta.rsi([float(i) for i in range(30)], 14)[-1] == 100.0


def test_moving_averages_bands_and_extremes():
    v = [1.0, 2, 3, 4, 5]
    assert ta.sma(v, 3) == [None, None, 2, 3, 4]
    assert ta.ema(v, 3) == [None, None, 2, 3, 4]  # a straight line: the EMA follows one step behind, as the SMA
    assert ta.wma(v, 3)[2] == pytest.approx((1 + 4 + 9) / 6)
    assert ta.sma([None, None, 3.0, 5.0], 2) == [None, None, None, 4]  # smooths another indicator's output
    bb = ta.bollinger([10.0] * 25, 20, 2)
    assert bb["upper"][-1] == bb["mid"][-1] == bb["lower"][-1] == 10
    assert ta.highest(v, 2) == [None, 2, 3, 4, 5] and ta.lowest(v, 3) == [None, None, 1, 2, 3]
    m = ta.macd([float(x) for x in range(60)], 12, 26, 9)
    assert m["line"][-1] == pytest.approx(7.0) and m["hist"][-1] == pytest.approx(0, abs=1e-9)


def test_supertrend_flips_with_the_trend_and_stoch_range():
    up = [100 + i for i in range(30)]
    down = [129 - 3 * i for i in range(1, 20)]
    st = ta.supertrend(day_candles(up + down), 10, 3)
    assert st["dir"][29] == 1 and st["line"][29] < 129
    assert st["dir"][-1] == -1 and st["line"][-1] > day_candles(up + down)[-1].close
    k = ta.stoch(day_candles(up), 14, 3, 3)["k"]
    assert 50 < k[-1] <= 100


def test_vwap_starts_afresh_each_day():
    d1 = datetime(2026, 1, 5, 9, 15, tzinfo=IST)
    cs = [Candle(d1, 10, 12, 8, 10, 100), Candle(d1 + timedelta(minutes=5), 20, 22, 18, 20, 300),
          Candle(d1 + timedelta(days=1), 50, 52, 48, 50, 10)]
    assert ta.vwap(cs) == [10, pytest.approx((10 * 100 + 20 * 300) / 400), 50]
    assert ta.vwap([Candle(d1, 1, 1, 1, 1, 0)]) == [None]  # an index has no volume


# ---- checking and describing -------------------------------------------------------------------

def test_validate_fills_defaults_and_refuses_bad_scans():
    s = scan((P("close"), "crosses_above", I("ema", n=20)), (I("rsi"), ">", N(60)))
    assert s["items"][0]["rhs"]["params"] == {"n": 20, "src": "close"}
    assert s["items"][1]["lhs"]["params"] == {"n": 14, "src": "close"}
    assert I("macd")["out"] == "" and validate({"op": "all", "items": [{"lhs": I("macd"), "cmp": ">", "rhs": N(0)}]}
                                               )["items"][0]["lhs"]["out"] == "line"
    for bad, says in [({"op": "all", "items": []}, "at least one"),
                      ({"op": "all", "items": [{"lhs": N(1), "cmp": ">", "rhs": N(0)}]}, "price or an indicator"),
                      ({"op": "all", "items": [{"lhs": I("ema", n=0), "cmp": ">", "rhs": N(0)}]}, "between"),
                      ({"op": "all", "items": [{"lhs": I("macd", fast=30), "cmp": ">", "rhs": N(0)}]}, "shorter"),
                      ({"op": "all", "items": [{"lhs": P("close", ago=99), "cmp": ">", "rhs": N(0)}]}, "Candles ago"),
                      ({"op": "all", "items": [{"lhs": P("bogus"), "cmp": ">", "rhs": N(0)}]}, "open, high"),
                      ({"op": "all", "items": [{"lhs": P("close"), "cmp": "~", "rhs": N(0)}]}, "comparison"),
                      ({"op": "both", "items": [{"lhs": P("close"), "cmp": ">", "rhs": N(0)}]}, "all or any"),
                      ("{not json", "couldn't be read"),
                      ({"op": "all", "items": [{"lhs": P("close"), "cmp": ">", "rhs": N(0)}] * 21}, "up to 20")]:
        with pytest.raises(ValueError, match=says):
            validate(bad)


def test_describe_reads_like_a_sentence():
    s = scan((P("close"), "crosses_above", I("ema", n=20)),
             ({"op": "any", "items": [{"lhs": I("rsi"), "cmp": ">", "rhs": N(60)},
                                      {"lhs": P("volume"), "cmp": ">", "rhs": I("sma", n=20, src="volume", pct=150)}]}, None, None),
             (P("close"), ">=", P("high", tf="1d", ago=1, pct=102)))
    assert describe(s) == ("Close crosses above EMA(20) and (RSI(14) > 60 or Volume > 150% of SMA(20,volume)) "
                           "and Close ≥ 102% of prev daily high")


# ---- evaluating ---------------------------------------------------------------------------------

def test_crosses_only_on_the_candle_it_happens():
    closes = [10.0] * 5 + [12, 13, 14]
    s = scan((P("close"), "crosses_above", I("sma", n=3)))
    stock = Stock("1d", day_candles(closes))
    assert [passes(s, stock, j) for j in range(len(closes))] == [False] * 5 + [True, False, False]
    below = scan((P("close"), "crosses_below", N(13.5)))
    assert not passes(below, stock)
    assert passes(scan((P("close"), ">", I("sma", n=3))), stock)


def test_previous_candle_and_percent_of():
    stock = Stock("1d", day_candles([100, 105], highs=[110, 106]))
    assert passes(scan((P("high"), "<", P("high", ago=1))), stock)
    assert not passes(scan((P("close"), ">", P("high", ago=1))), stock)
    assert passes(scan((P("close"), ">", P("high", ago=1, pct=95))), stock)  # 105 > 104.5


def test_any_and_groups():
    stock = Stock("1d", day_candles([100, 105]))
    yes, no = (P("close"), ">", N(1)), (P("close"), "<", N(1))
    assert passes(scan(yes, no, op="any"), stock) and not passes(scan(yes, no), stock)
    group = {"op": "any", "items": [{"lhs": no[0], "cmp": no[1], "rhs": no[2]}, {"lhs": yes[0], "cmp": yes[1], "rhs": yes[2]}]}
    assert passes(scan(yes, (group, None, None)), stock)


def test_previous_day_high_in_an_intraday_scan():
    """In a 15-minute scan, 'prev daily high' is yesterday's; 'daily high' is today's so far."""
    daily = day_candles([100, 101], highs=[105, 108])  # Mon 5, Tue 6 Jan
    wed = datetime(2026, 1, 7, 9, 15, tzinfo=IST)
    main = [Candle(wed + timedelta(minutes=15 * i), p, p + 0.5, p - 0.5, p, 10) for i, p in enumerate([104, 107, 109])]
    stock = Stock("15m", main, daily)
    s = scan((P("close"), "crosses_above", P("high", tf="1d", ago=1)))
    assert [passes(s, stock, j) for j in range(3)] == [False, False, True]
    assert passes(scan((P("high", tf="1d"), "=", N(109.5))), stock)
    assert passes(scan((P("open", tf="1d"), "=", N(104))), stock, 2)
    assert dict(readings(s, stock))["Prev daily high"] == 108
    # A daily indicator: SMA(2) of daily closes with today's forming close (109): (101 + 109) / 2.
    assert passes(scan((I("sma", n=2, tf="1d"), "=", N(105))), stock)


def test_weekly_from_daily():
    d = day_candles([10, 11, 12, 13, 14, 20, 21], highs=[11, 12, 13, 19, 15, 21, 22])  # Mon-Fri, then Mon-Tue
    w = weekly(d)
    assert [(c.open, c.high, c.close) for c in w] == [(10, 19, 14), (20, 22, 21)]
    stock = Stock("1d", d)
    assert passes(scan((P("close"), ">", P("high", tf="1w", ago=1))), stock)  # 21 > last week's 19
    assert not passes(scan((P("close"), ">", P("high", tf="1w", ago=1))), stock, 4)  # Friday: week 1 is still forming


def test_needs_counts_warmup_ago_and_daily():
    s = scan((P("close"), "crosses_above", I("ema", n=50)), (P("close"), ">", P("high", tf="1d", ago=1)))
    n = needs(s, "15m")
    assert n["main"] >= 150 and 3 <= n["daily"] < 10
    assert needs(s, "1d")["daily"] == 0
    assert needs(scan((P("close"), ">", I("highest", tf="1w", n=52))), "1h")["daily"] > 52 * 5


# ---- a bigger intraday candle size on one line ---------------------------------------------------

def five_min(day, closes):
    """5-minute candles from 9:15 on `day`, one per close, high close+1, low close-1."""
    start = datetime.combine(day, datetime.min.time(), IST).replace(hour=9, minute=15)
    return [Candle(start + timedelta(minutes=5 * i), c, c + 1, c - 1, c, 100) for i, c in enumerate(closes)]


def test_bigger_intraday_candles_are_built_from_the_scans_own():
    from app.scan_expr import group
    from datetime import date, time
    got = group(five_min(date(2026, 1, 5), [10, 11, 12, 13, 14]), 15, time(9, 15))
    assert [(c.start.strftime("%H:%M"), c.open, c.high, c.low, c.close, c.volume) for c in got] == [
        ("09:15", 10, 13, 9, 12, 300), ("09:30", 13, 15, 12, 14, 200)]  # the second is still forming


def test_a_15_minute_value_in_a_5_minute_scan():
    from datetime import date
    candles = five_min(date(2026, 1, 5), [10, 11, 12, 13, 14, 20])
    stock = Stock("5m", candles)
    # Prev 15 min high is 9:15-9:30's (13); the 15 min candle forming now (9:30-9:45) has closed at 20 so far.
    assert stock.value(P("high", tf="15m", ago=1), 5) == 13
    assert stock.value(P("close", tf="15m"), 5) == 20
    assert stock.value(P("open", tf="15m"), 5) == 13
    assert passes(scan((P("close"), ">", P("high", tf="15m", ago=1))), stock)
    assert not passes(scan((P("close"), ">", P("high", tf="15m", ago=1))), stock, 2)  # 12 vs no previous 15 min candle
    assert readings(scan((P("close"), ">", P("high", tf="15m", ago=1))), stock) == [("Close", 20), ("Prev 15 min high", 13)]
    # An indicator on 15 min candles: SMA(2) of 15-minute closes 12 and 20.
    assert stock.value(I("sma", tf="15m", n=2, src="close"), 5) == 16


def test_fit_checks_each_lines_candle_size_against_the_scans():
    from app.scan_expr import fit
    s = scan((P("close", tf="15m"), ">", I("ema", tf="1h", n=20)), (P("close", tf="5m"), ">", P("high", tf="1d", ago=1)))
    got = fit(s, "5m")
    assert got["items"][1]["lhs"]["tf"] == ""  # the scan's own size
    assert got["items"][0]["lhs"]["tf"] == "15m"
    with pytest.raises(ValueError, match="can't be read in a 15 min scan"):
        fit(s, "15m")  # 5 min is smaller than the scan's
    with pytest.raises(ValueError):
        fit(scan((P("close", tf="15m"), ">", N(1))), "10m")  # 15 isn't a whole number of 10s
    with pytest.raises(ValueError):
        fit(scan((P("close", tf="1h"), ">", N(1))), "1d")
    assert fit(scan((P("close", tf="1d"), ">", N(1))), "1d")["items"][0]["lhs"]["tf"] == ""
    assert fit(scan((P("close", tf="5m"), ">", P("high", tf="3m", ago=1))), "1m")["items"][0]["lhs"]["tf"] == "5m"
    with pytest.raises(ValueError):
        fit(scan((P("close", tf="5m"), ">", N(1))), "3m")  # 5 isn't a whole number of 3s
    assert needs(fit(scan((P("close"), ">", I("sma", tf="1h", n=20, src="close"))), "5m"), "5m")["main"] >= 20 * 12
    assert describe(s) == "15 min close > 1 hour EMA(20) and 5 min close > Prev daily high"


def test_a_condition_switched_off_is_kept_but_left_out():
    from datetime import date
    from app.scan_expr import conditions
    stock = Stock("5m", five_min(date(2026, 1, 5), [10, 11, 12]))
    raw = {"op": "all", "items": [{"lhs": P("close"), "cmp": ">", "rhs": N(11)},
                                  {"lhs": P("close"), "cmp": ">", "rhs": N(50), "off": True},
                                  {"op": "any", "items": [{"lhs": P("close"), "cmp": "<", "rhs": N(5), "off": True}]}]}
    s = validate(raw)
    assert s["items"][1]["off"] is True and len(conditions(s, every=True)) == 3  # kept when saved
    assert passes(s, stock)  # close 12 > 50 would fail, but it's off; the group has nothing on, so it's left out
    assert not passes(validate({**raw, "items": [{**raw["items"][1], "off": False}]}), stock)
    assert describe(s) == "Close > 11"
    assert readings(s, stock) == [("Close", 12)]
    with pytest.raises(ValueError, match="switched off"):
        validate({"op": "all", "items": [raw["items"][1]]})



def test_the_days_nth_candle_and_days_ago():
    from datetime import date
    from app.scan_expr import fit
    mon = five_min(date(2026, 1, 5), [50, 51, 52])
    tue = five_min(date(2026, 1, 6), [10, 11, 12, 13, 14, 20])
    stock = Stock("5m", mon + tue)

    def nth(field, n, tf=""):
        return {**P(field, tf=tf), "nth": n}
    s = validate({"op": "all", "items": [{"lhs": P("close"), "cmp": ">", "rhs": nth("high", 1)}]})
    assert s["items"][0]["rhs"] == {**P("high"), "nth": 1}
    assert stock.value(s["items"][0]["rhs"], len(stock.main) - 1) == 11  # Tuesday's first 5-minute candle, not Monday's
    assert stock.value(nth("close", 3), len(stock.main) - 1) == 12
    assert stock.value(nth("close", 9), len(stock.main) - 1) is None  # not formed yet
    assert stock.value(nth("high", 1, "15m"), len(stock.main) - 1) == 13  # 9:15-9:30 on Tuesday
    assert passes(s, stock)
    assert stock.value({**I("sma", n=2, src="close"), "nth": 2}, len(stock.main) - 1) == 10.5
    assert describe(s) == "Close > Day's 1st candle high"
    assert describe_value_of({**P("high", tf="15m"), "nth": 2}) == "Day's 2nd 15 min candle high"
    assert describe_value_of(P("high", tf="1d", ago=2)) == "2 days ago high"
    assert describe_value_of(P("close", tf="1w", ago=3)) == "3 weeks ago close"
    with pytest.raises(ValueError, match="intraday"):
        validate({"op": "all", "items": [{"lhs": P("close"), "cmp": ">", "rhs": nth("high", 1, "1d")}]})
    with pytest.raises(ValueError, match="intraday"):
        fit(s, "1d")


def describe_value_of(v):
    from app.scan_expr import describe_value
    return describe_value(validate({"op": "all", "items": [{"lhs": v, "cmp": ">", "rhs": N(1)}]})["items"][0]["lhs"])


def test_vwap_on_the_days_first_candle():
    """VWAP read at the day's 1st candle is that candle's VWAP, not the latest one's."""
    from datetime import date
    from app.scan_expr import Stock, validate
    main = five_min(date(2026, 1, 5), [100, 110, 120, 130])
    s = Stock("5m", main, [])
    first = {"kind": "ind", "ind": "vwap", "params": {}, "out": "", "tf": "", "ago": 0, "pct": 100, "nth": 1}
    latest = {**first, "nth": 0}
    v = validate({"op": "all", "items": [{"lhs": first, "cmp": ">", "rhs": {"kind": "num", "value": 0}},
                                          {"lhs": latest, "cmp": ">", "rhs": {"kind": "num", "value": 0}}]})
    a, b = (s.value(c["lhs"], len(main) - 1) for c in v["items"])
    assert a == 100  # the first candle's typical price
    assert b == 115  # the whole day so far
