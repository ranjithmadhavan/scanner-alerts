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
