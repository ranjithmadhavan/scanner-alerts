from datetime import datetime, time, timedelta

from app.kite import Candle
from app.market import IST, MarketSettings
from app.scanner import evaluate, last_boundary

S = MarketSettings(time(9, 15), time(15, 30), 60)
DAY = datetime(2026, 9, 28, tzinfo=IST)


def at(h, m, s=0):
    return DAY.replace(hour=h, minute=m, second=s)


def c(start, o, h, l, cl):
    return Candle(start, o, h, l, cl)


def alert(condition, level, armed, timeframe="15m"):
    return {"condition": condition, "level": level, "timeframe": timeframe, "armed_at": armed.isoformat()}


def test_close_above_waits_for_candle_to_finish():
    candles = [c(at(10, 0), 99, 102, 98, 101)]
    a = alert("close_above", 100, at(9, 50))
    assert evaluate(a, candles, S, at(10, 10)) is None          # still forming
    assert evaluate(a, candles, S, at(10, 15, 5)) is None       # within settle delay
    hit = evaluate(a, candles, S, at(10, 15, 20))
    assert hit and hit[1] == 101


def test_close_ignores_candles_finished_before_arming():
    candles = [c(at(10, 0), 99, 102, 98, 101), c(at(10, 15), 101, 101, 97, 98)]
    assert evaluate(alert("close_above", 100, at(10, 20)), candles, S, at(10, 40)) is None


def test_close_below():
    candles = [c(at(10, 0), 101, 102, 97, 99.5)]
    assert evaluate(alert("close_below", 100, at(9, 30)), candles, S, at(10, 16))[1] == 99.5


def test_high_above_uses_minutes_since_arming():
    candles = [c(at(10, 0), 99, 105, 98, 99), c(at(10, 5), 99, 99.5, 98, 99), c(at(10, 6), 99, 100.4, 99, 100)]
    a = alert("high_above", 100, at(10, 5, 30), timeframe="")
    hit = evaluate(a, candles, S, at(10, 6, 30))
    assert hit and hit[0].start == at(10, 6) and hit[1] == 100.4


def test_low_below_fires_on_forming_minute():
    candles = [c(at(11, 0), 100, 100, 94.9, 95.5)]
    assert evaluate(alert("low_below", 95, at(10, 59), timeframe=""), candles, S, at(11, 0, 20))


def test_daily_close_only_after_bell():
    candles = [c(DAY, 99, 104, 98, 103)]
    a = alert("close_above", 100, at(9, 0), timeframe="1d")
    assert evaluate(a, candles, S, at(15, 0)) is None
    assert evaluate(a, candles, S, at(15, 30, 15))


def test_last_boundary():
    assert last_boundary("15m", S, at(10, 20)) == at(10, 15)
    assert last_boundary("15m", S, at(9, 20)) is None
    assert last_boundary("1h", S, at(15, 31)) == at(15, 30)
    assert last_boundary("1h", S, at(15, 20)) == at(15, 15)
    assert last_boundary("1d", S, at(15, 0)) is None


def test_clean_enctoken_accepts_common_copy_formats():
    from app.kite import clean_enctoken
    t = "AbC+dEf/GhI=="
    for raw in [t, f"enctoken {t}", f"enctoken={t};", f'"{t}"', "AbC%2BdEf%2FGhI%3D%3D", "AbC+dEf/\n GhI=="]:
        assert clean_enctoken(raw) == t, raw


def test_simulate_replays_last_session_and_skips_weekend():
    from app.scanner import last_session_day, simulate, simulation_message
    sunday = DAY.replace(day=27, hour=16)          # Sun 27 Sep 2026
    assert last_session_day(S, sunday).date().isoformat() == "2026-09-25"   # Friday
    assert last_session_day(S, at(10, 0)).date().isoformat() == "2026-09-25"  # Mon before close -> Fri
    assert last_session_day(S, at(15, 45)).date() == DAY.date()               # Mon after close -> Mon

    fri = DAY.replace(day=25)
    asked = []

    class FakeKite:
        def candles(self, token, tf, day):
            asked.append((tf, day.isoformat()))
            return [Candle(fri.replace(hour=10, minute=31), 99, 100.2, 98, 100),
                    Candle(fri.replace(hour=10, minute=32), 100, 101.5, 99.5, 101)]

    alert = {"symbol": "INFY", "token": 1, "condition": "high_above", "level": 101, "timeframe": ""}
    r = simulate(alert, FakeKite(), S, sunday)
    assert asked == [("1m", "2026-09-25")]
    assert r["hit"][1] == 101.5 and r["high"] == 101.5
    subject, body = simulation_message(alert, r)
    assert "Simulation" in subject and "10:32 AM" in body and "unchanged" in body

    r = simulate({**alert, "level": 200}, FakeKite(), S, sunday)
    assert r["hit"] is None
    assert "would not have fired" in simulation_message({**alert, "level": 200}, r)[0]


def test_next_check_times():
    from app.scanner import next_check, next_open
    tick = at(11, 43, 5)
    trades = {"condition": "high_above", "timeframe": ""}
    close15 = {"condition": "close_above", "timeframe": "15m"}
    daily = {"condition": "close_below", "timeframe": "1d"}

    # During market hours
    assert next_check(trades, S, at(11, 42, 10), tick) == {"at": tick, "after_candle": False}
    assert next_check(close15, S, at(11, 42, 10), tick) == {"at": at(11, 45), "after_candle": True}
    assert next_check(daily, S, at(11, 42), tick)["at"] == at(15, 30)

    # Before the open on a Monday, and on a Sunday
    assert next_open(S, at(8, 0)) == at(9, 15)
    assert next_check(close15, S, at(8, 0), None)["at"] == at(9, 30)
    sunday = DAY.replace(day=27, hour=12)
    assert next_check(trades, S, sunday, None)["at"] == at(9, 15)

    # After the close on Monday -> Tuesday
    tue = DAY.replace(day=29)
    assert next_check(close15, S, at(16, 0), None)["at"] == tue.replace(hour=9, minute=30)


def test_instrument_lists_cover_indices_futures_and_options():
    from app import kite
    header = "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange\n"
    bse = kite.parse_instruments("BSE", header + '265,1,SENSEX,"SENSEX",0,,0,0,0,EQ,INDICES,BSE\n'
                                 '128083204,500325,RELIANCE,"RELIANCE INDUSTRIES",0,,0,0.05,1,EQ,BSE,BSE\n')
    assert bse["BSE:SENSEX"].is_index and bse["BSE:SENSEX"].token == 265 and not bse["BSE:RELIANCE"].is_index
    bfo = kite.parse_instruments("BFO", header + '221330181,864571,SENSEX26OCTFUT,"SENSEX",0,2026-10-29,0,0.05,20,FUT,BFO-FUT,BFO\n'
                                 '1,2,SENSEX26O0882000PE,"SENSEX",0,2026-10-08,82000,0.05,20,PE,BFO-OPT,BFO\n')
    assert bfo["BFO:SENSEX26OCTFUT"].name == "SENSEX 29 OCT 26 FUT"
    assert bfo["BFO:SENSEX26O0882000PE"].name == "SENSEX 08 OCT 26 82000 PE"
    nse = kite.parse_instruments("NSE", header + '738561,2885,RELIANCE,"RELIANCE INDUSTRIES",0,,0,0.1,1,EQ,NSE,NSE\n')
    assert nse["RELIANCE"].key == "RELIANCE"   # NSE keeps bare symbols, as alerts saved earlier expect


def test_simulate_reports_each_level():
    from app.scanner import simulate, simulation_message
    fri = DAY.replace(day=25)

    class FakeKite:
        def candles(self, token, tf, day):
            return [Candle(fri.replace(hour=10, minute=32), 100, 101.5, 99.5, 101)]

    alert = {"symbol": "INFY", "token": 1, "timeframe": "", "levels": [
        {"level": 101, "condition": "high_above", "status": "active"},
        {"level": 90, "condition": "low_below", "status": "active"}]}
    r = simulate(alert, FakeKite(), S, DAY.replace(day=27, hour=16))
    assert [bool(lv["hit"]) for lv in r["levels"]] == [True, False] and r["hit"][1] == 101.5
    subject, body = simulation_message(alert, r)
    assert "1 of 2 levels" in subject and "trades above 101: fired at 10:32 AM" in body and "trades below 90: not met" in body


def test_cross_takes_its_direction_from_where_price_starts():
    a = alert("cross", 100, at(10, 5, 30), timeframe="")
    up = [c(at(10, 5), 98, 99, 97, 98.5), c(at(10, 6), 98.5, 100.4, 98, 100)]
    hit = evaluate(a, up, S, at(10, 6, 30))
    assert hit and hit[0].start == at(10, 6) and hit[1] == 100.4          # started below: fires going up
    down = [c(at(10, 5), 103, 104, 101, 102), c(at(10, 6), 102, 102, 99.5, 100)]
    assert evaluate(a, down, S, at(10, 6, 30))[1] == 99.5                  # started above: fires going down
    assert evaluate(a, [c(at(10, 5), 103, 104, 101, 102)], S, at(10, 6)) is None

    # Armed mid-session with a known price: that price sets the side, not the candle's open.
    armed_above = {**a, "armed_price": 101}
    dipped = [c(at(10, 5), 99, 101.5, 98.8, 101.2)]     # opened below, was above when armed
    assert evaluate(armed_above, dipped, S, at(10, 5, 50))[1] == 98.8


def test_gap_through_a_level_flips_its_direction_instead_of_firing():
    friday = (DAY - timedelta(days=3)).replace(hour=14)
    a = {"condition": "cross", "level": 100, "timeframe": "", "armed_at": friday.isoformat(), "armed_price": 105}
    # Price was above 100 on Friday. Monday gaps down to 96: no alert for the gap itself...
    gap = [c(at(9, 15), 96, 97, 95, 96.5), c(at(9, 16), 96.5, 98, 96, 97)]
    assert evaluate(a, gap, S, at(9, 17)) is None
    # ...and the level now waits for a move back up through it.
    back = gap + [c(at(9, 17), 97, 100.6, 97, 100.2)]
    assert evaluate(a, back, S, at(9, 18))[1] == 100.6
    # No gap: Monday opens above the level and falling through it fires as before.
    assert evaluate(a, [c(at(9, 15), 104, 104, 99.4, 100)], S, at(9, 16))[1] == 99.4


def test_close_cross_works_both_ways_and_ignores_a_gap():
    friday = (DAY - timedelta(days=3)).replace(hour=14)
    a = {"condition": "close_cross", "level": 100, "timeframe": "15m", "armed_at": friday.isoformat()}
    gap_down = [c(at(9, 15), 96, 99, 95, 97), c(at(9, 30), 97, 101, 96, 100.5)]
    assert evaluate(a, gap_down[:1], S, at(9, 31)) is None                 # closed below, but it opened below
    assert evaluate(a, gap_down, S, at(9, 46))[1] == 100.5                 # closes back above: fires
    opened_above = [c(at(9, 15), 103, 104, 99, 99.5)]
    assert evaluate(a, opened_above, S, at(9, 29)) is None                 # candle still forming
    assert evaluate(a, opened_above, S, at(9, 31))[1] == 99.5


def test_either_way_hit_is_reported_with_its_direction():
    from app.scanner import describe, resolved
    v = {"symbol": "INFY", "condition": "cross", "level": 100, "timeframe": ""}
    assert describe(resolved(v, 100.4)) == "INFY trades above 100"
    assert describe(resolved(v, 99.5)) == "INFY trades below 100"
    assert describe(resolved({**v, "condition": "close_cross", "timeframe": "15m"}, 99.5)) == "INFY closes below 100 on 15m"
    assert describe(resolved({**v, "condition": "low_below"}, 99.5)) == "INFY trades below 100"
