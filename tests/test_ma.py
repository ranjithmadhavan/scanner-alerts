"""MA crossover: the backtest end to end on fake Kite candles, and the live scanner's two jobs."""

import time as clock
from datetime import date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import brokers, config, crossover, ma_backtest, ma_scanner, notify, universe
from app import kite as kite_mod
from app.kite import Candle, Instrument
from app.market import IST
from app.routes import indicators as routes
from app.store import store

INFY = Instrument("INFY", "INFOSYS", 408065, False)


def weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = weekdays(date(2026, 7, 1), 66)  # up to Wed 30 Sep 2026
# A steady climb with a red pullback candle every six days (each broken the next day), then one more red
# candle on the last day: a buy setup above its high.
ROWS = [(100.5 + i, 101 + i, 97 + i, 99.6 + i) if i % 6 == 3 and i < len(DAYS) - 8 else (100 + i, 101 + i, 99 + i, 100.5 + i)
        for i in range(len(DAYS) - 1)] + [(166, 166.5, 164, 164.5)]
BREAK = (date(2026, 10, 1), (165, 168, 164.8, 167.5))


def daily(rows, days=DAYS):
    return [Candle(datetime(d.year, d.month, d.day, tzinfo=IST), *r) for d, r in zip(days, rows)]


class FakeKite:
    def __init__(self, candles):
        self.candles = candles
        self.quotes = {}
        self.requests = 0

    def profile(self):
        return {}

    def candles_range(self, token, tf, start, end):
        self.requests += 1
        assert tf == "1d"
        return [c for c in self.candles if start <= c.start.date() <= end]

    def quote(self, keys):
        return {k: self.quotes[k] for k in keys if k in self.quotes}


@pytest.fixture
def kite(monkeypatch):
    fake = FakeKite(daily(ROWS))
    insts = {"INFY": INFY}
    from app import ma_screen
    for mod in (ma_backtest, ma_scanner, ma_screen, routes, universe, kite_mod):
        monkeypatch.setattr(mod, "instruments", lambda: insts)
    monkeypatch.setattr(brokers, "client_for", lambda u, doc=None: fake)
    monkeypatch.setattr(universe.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("offline")))
    store.put("brokers", "tara", {"mode": "enctoken", "status": "connected", "enctoken": "x"})
    return fake


@pytest.fixture
def web(kite, monkeypatch):
    monkeypatch.setattr(config, "SUPERADMIN_USERNAME", "boss")
    monkeypatch.setattr(config, "SUPERADMIN_PASSWORD", "boss-pass-123")
    from app.main import app
    with TestClient(app) as c:
        c.post("/login", data={"username": "boss", "password": "boss-pass-123"})
        c.post("/admin/users", data={"username": "tara", "name": "Tara", "password": "tara-pass-1", "modules": ["ma"]})
        c.post("/logout")
        c.post("/login", data={"username": "tara", "password": "tara-pass-1"})
        yield c


SETUP = {"universe_kind": "symbols", "symbols": "infy", "tf": "1d", "fast_kind": "sma", "fast_len": "2", "slow_kind": "sma",
         "slow_len": "5", "touch": "", "sides": "both"}


def wait(username):
    for _ in range(100):
        run = ma_backtest.current(username)
        if run and run.status != "running":
            return run
        clock.sleep(0.05)
    raise AssertionError("backtest didn't finish")


def test_backtest_end_to_end(web, kite):
    assert web.get("/ma").status_code == 200
    page = web.get("/ma?tab=backtest").text
    assert "Run backtest" in page and "Nifty 500" in page and "F&amp;O stocks" in page
    kite.candles = daily(ROWS + [BREAK[1]], DAYS + [BREAK[0]])
    r = web.post("/ma/backtest", data={**SETUP, "start": "2026-08-01", "end": "2026-10-01", "stop": "candle", "rr": "2",
                                       "book": "100", "trail": "", "fill": "open", "risk": "1000"})
    assert "Backtest started on 1 stock" in r.headers["HX-Trigger"]
    run = wait("tara")
    assert run.status == "done" and run.done == 1
    expected, _ = crossover.trades(kite.candles, crossover.Rules("sma", 2, "sma", 5, once=False), crossover.Exits(rr=2),
                                   0, next(i for i, c in enumerate(kite.candles) if c.start.date() >= date(2026, 8, 1)))
    assert [(t["entry_at"], t["status"], t["r"]) for t in run.trades] == \
        [(kite.candles[t.entry.index].start.isoformat(), t.status, t.r) for t in expected]
    assert sum(t["status"] == "closed" for t in run.trades) >= 2
    assert run.trades[-1]["level"] == 166.5 and run.trades[-1]["status"] == "open"
    html = web.get("/ma/backtest/status").text
    assert "Backtest with real Kite prices" in html and "INFY" in html and "ma-data" in html and "Win rate" in html
    assert "At <span class=\"num font-semibold\">₹1,000</span> risked per trade" in html
    csv = web.get("/ma/backtest/csv")
    assert csv.headers["content-type"].startswith("text/csv") and csv.text.startswith("symbol,side,status") and "rupees" in csv.text
    chart = web.get("/ma/backtest/chart?symbol=INFY").json()
    assert chart["candles"][0]["time"] >= "2026-08-01" and chart["panel"] is None
    assert [(o["label"], bool(o["points"])) for o in chart["overlays"]] == [("2 SMA", True), ("5 SMA", True)]
    assert web.get("/ma/backtest/chart?symbol=TCS").status_code == 404
    # The page shows the finished run too.
    assert "Download CSV" in web.get("/ma?tab=backtest").text


def test_backtest_form_is_checked(web):
    def err(**changes):
        r = web.post("/ma/backtest", data={**SETUP, "start": "2026-08-01", "end": "2026-09-30", **changes})
        assert r.headers.get("HX-Reswap") == "none"
        return r.headers["HX-Trigger"]
    assert "shorter period" in err(fast_len="21", slow_len="9")
    assert "NOPE" in err(symbols="INFY, NOPE")
    assert "first day" in err(start="2026-10-01", end="2026-09-01")
    # 500 stocks of 3-minute candles over a year is far more than Kite would serve in a few minutes.
    assert "Kite requests" in err(universe_kind="list", universe_list="nifty500", tf="3m", start="2025-10-01")


def test_scanner_create_limits_and_list(web):
    r = web.post("/ma/scanners", data={**SETUP, "universe_kind": "list", "universe_list": "nifty500", "tf": "5m"})
    assert "up to 100 stocks" in r.headers["HX-Trigger"]
    r = web.post("/ma/scanners", data={**SETUP, "stop": "candle", "rr": "2", "gap_alerts": "1", "name": "Infy daily"})
    assert "Scanner saved" in r.headers["HX-Trigger"] and "Infy daily" in r.text
    sc = store.list("ma_scanners", user="tara")[0]
    assert sc["spec"] == {"symbols": ["INFY"]} and sc["rules"]["slow_len"] == 5 and sc["status"] == "active"
    r = web.post(f"/ma/scanners/{sc['id']}/pause")
    assert "Paused" in r.headers["HX-Trigger"]
    # "Backtest this" opens the backtest form with the scanner's settings.
    page = web.get("/ma?backtest=" + sc["id"]).text
    assert "Run backtest" in page and '<option value="1d" selected>' in page and 'name="symbols" value="INFY" data-picker-value' in page
    web.post(f"/ma/scanners/{sc['id']}/delete")
    assert store.list("ma_scanners", user="tara") == []


def test_scanner_edit(web):
    web.post("/ma/scanners", data={**SETUP, "stop": "candle", "rr": "2", "once": "1", "second": "1", "name": "Infy daily"})
    sc = store.list("ma_scanners", user="tara")[0]
    store.update("ma_scanners", sc["id"], {"state": {"INFY": {"level": 1}}, "evaluated_at": "2026-09-30T15:30:00+05:30"})
    # The form opens with the scanner's settings and saves back to it.
    page = web.get("/ma?edit=" + sc["id"]).text
    assert "Edit “Infy daily”" in page and f'hx-post="/ma/scanners/{sc["id"]}"' in page and "Save changes" in page
    assert 'value="Infy daily"' in page and '<option value="1d" selected>' in page and 'name="symbols" value="INFY" data-picker-value' in page
    # Only the name and target: it keeps watching what it was.
    r = web.post(f"/ma/scanners/{sc['id']}", data={**SETUP, "stop": "candle", "rr": "3", "once": "1", "second": "1", "name": "Infy 1:3"})
    assert "Scanner updated." in r.headers["HX-Trigger"] and "Infy 1:3" in r.text
    got = store.get("ma_scanners", sc["id"])
    assert got["rr"] == 3 and got["state"] == {"INFY": {"level": 1}} and got["armed_at"] == sc["armed_at"]
    # Other rules (every signal, not just fresh ones): what it waited on is dropped and it starts afresh.
    r = web.post(f"/ma/scanners/{sc['id']}", data={**SETUP, "stop": "candle", "rr": "3", "name": "Infy 1:3"})
    assert "waiting setups were dropped" in r.headers["HX-Trigger"]
    got = store.get("ma_scanners", sc["id"])
    assert got["rules"]["once"] is False and got["state"] == {} and got["evaluated_at"] == "" and got["status"] == "active"
    assert len(store.list("ma_scanners", user="tara")) == 1
    store.delete("ma_scanners", sc["id"])


def test_a_read_started_before_an_edit_is_dropped(kite, sent):
    from app.market import load_settings
    s = load_settings()
    before = _scanner()  # what the candle job read the scanner as
    _scanner(armed_at="2026-10-01T10:00:00+05:30")  # then it was edited and re-armed
    at = datetime(2026, 9, 30, 15, 31, tzinfo=IST)
    ma_scanner.evaluate(before, kite, s, at, s.close_at(at))
    assert sent == [] and store.get("ma_scanners", "s1")["state"] == {}
    store.delete("ma_scanners", "s1")


def _scanner(**more):
    sc = {"id": "s1", "user": "tara", "name": "Infy daily", "spec": {"symbols": ["INFY"]}, "tf": "1d",
          "rules": {"fast_kind": "sma", "fast_len": 2, "slow_kind": "sma", "slow_len": 5, "touch": "", "sides": "both",
                    "once": False},
          "stop": "candle", "rr": 2, "gap_alerts": False, "channels": ["telegram"], "webhooks": [], "status": "active",
          "created_at": "2026-09-01T09:00:00+05:30", "armed_at": "2026-09-29T16:00:00+05:30", "state": {}, "fired": [], **more}
    store.put("ma_scanners", sc["id"], sc)
    return sc


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(notify, "send", lambda user, ch, subject, body: out.append((subject, body)) or {"telegram": "sent"})
    return out


def test_setup_then_entry_from_quotes_then_no_repeat_at_the_close(kite, sent):
    from app.market import load_settings
    s = load_settings()
    sc = _scanner()
    after_close = datetime(2026, 9, 30, 15, 31, tzinfo=IST)
    # At 3:31 the daily candle isn't read yet (yesterday's close is the latest); from 3:45 it is.
    assert ma_scanner.latest_close("1d", s, after_close) == s.close_at(datetime(2026, 9, 29, tzinfo=IST))
    assert ma_scanner.latest_close("1d", s, after_close.replace(minute=45)) == s.close_at(after_close)
    assert ma_scanner.latest_close("15m", s, after_close) == s.close_at(after_close)  # intraday: at the close as before
    sc = ma_scanner.evaluate(sc, kite, s, after_close, s.close_at(after_close))
    assert sent[-1][0] == "📈 INFY: buy above 166.50"
    assert "INFY buy above ₹166.50 · SL ₹164.00 · target ₹171.50 (1:2)" in sent[-1][1]
    assert sc["state"]["INFY"]["level"] == 166.5
    # Next morning the price trades through: one quote request, an entry message.
    kite.quotes = {"NSE:INFY": {"last_price": 166.9, "ohlc": {"open": 165.0}}}
    sc = ma_scanner.check_quotes(sc, kite, s, datetime(2026, 10, 1, 9, 40, tzinfo=IST))
    assert sent[-1][0] == "🚀 INFY buy triggered at 166.50" and "broke ₹166.50, now ₹166.90" in sent[-1][1]
    assert sc["state"] == {} and "INFY:2026-09-30T00:00:00+05:30:entry" in sc["fired"]
    # At the close the candles show the same entry: it isn't sent again.
    kite.candles = daily(ROWS + [BREAK[1]], DAYS + [BREAK[0]])
    n = len(sent)
    oct1 = datetime(2026, 10, 1, 15, 31, tzinfo=IST)
    ma_scanner.evaluate(store.get("ma_scanners", "s1"), kite, s, oct1, s.close_at(oct1))
    assert len(sent) == n
    # An event is logged for the Notifications page, but not as a price-alert hit on charts.
    events = store.list("events", user="tara")
    assert events and all(e["key"] == "ma:s1" for e in events if e["alert_id"] == "s1")


def test_gap_entries_follow_the_setting(kite, sent):
    from app.market import load_settings
    s = load_settings()
    at = datetime(2026, 9, 30, 15, 31, tzinfo=IST)
    for gap_alerts in (False, True):
        sent.clear()
        store.delete("ma_scanners", "s1")
        sc = ma_scanner.evaluate(_scanner(gap_alerts=gap_alerts), kite, s, at, s.close_at(at))
        kite.quotes = {"NSE:INFY": {"last_price": 170.0, "ohlc": {"open": 169.0}}}
        sc = ma_scanner.check_quotes(sc, kite, s, datetime(2026, 10, 1, 9, 16, tzinfo=IST))
        assert sc["state"] == {}
        if gap_alerts:
            assert sent[-1][0] == "🚀 INFY buy triggered at 166.50 (gap)"
            # Entered at the open: SL stays the trigger candle's low, the target is 2R from 169.
            assert "opened at ₹169.00, above ₹166.50 (gap) · SL ₹164.00 · target ₹179.00" in sent[-1][1]
        else:
            assert [x[0] for x in sent] == ["📈 INFY: buy above 166.50"]


def test_nothing_from_before_the_scanner_was_armed(kite, sent):
    from app.market import load_settings
    s = load_settings()
    at = datetime(2026, 9, 30, 15, 31, tzinfo=IST)
    sc = ma_scanner.evaluate(_scanner(armed_at="2026-09-30T20:00:00+05:30"), kite, s, at, s.close_at(at))
    assert sent == [] and sc["state"]["INFY"]["level"] == 166.5  # still watched for its entry


def test_slow_ma_stop_in_messages(kite, sent):
    sc = _scanner(stop="slow_ma", rr=1.5)
    stop, target = ma_scanner.plan(sc, "buy", 166.5, 164, 163.0)
    assert stop == 163.0 and target == pytest.approx(166.5 + 1.5 * 3.5)
    assert ma_scanner.plan(sc, "sell", 100, 104, 103) == (103, pytest.approx(100 - 1.5 * 3))
    assert ma_scanner.plan(sc, "buy", 160, 164, 163.0) == (163.0, None)  # entry under the MA: no target to give


def test_backtest_form_defaults_are_positional(web):
    import re
    page = web.get("/ma?tab=backtest").text
    form = page[page.index('id="ma-backtest"'):]
    checked = lambda name: re.search(rf'name="{name}" value="([^"]*)"[^>]*checked', form).group(1)
    assert '<option value="1d" selected>' in form
    from app.market import now_ist
    a_year_ago = (now_ist().date() - timedelta(days=365)).isoformat()
    assert f'name="start" type="date" value="{a_year_ago}"' in form  # a year back by default
    assert 'name="fast_len" type="number" min="1" max="400" step="1" required value="10"' in form
    assert 'name="slow_len" type="number" min="1" max="400" step="1" required value="20"' in form
    assert form.count('<option value="ema" selected>') == 2
    assert checked("touch") == "fast" and checked("sides") == "buy"
    assert checked("breakeven") == "1" and checked("ma_exit") == "1" and checked("once") == "1"
    assert checked("second") == "1"
    assert not re.search(r'name="square_off" value="1"[^>]*checked', form)  # positional, not intraday
    assert 'name="risk" type="number" min="1" step="1" value="1000"' in form
    assert 'name="book" type="number" min="0" max="100" step="5" value="50"' in form  # half at the target, the rest runs
    # The live scanner form keeps its own defaults.
    scanners = web.get("/ma?tab=scanners").text
    assert '<option value="15m" selected>' in scanners


def test_stock_picker_suggests_nse_stocks(web, monkeypatch):
    insts = {"INFY": INFY, "INFIBEAM": Instrument("INFIBEAM", "INFIBEAM AVENUES", 2, False),
             "BSE:INFY": Instrument("INFY", "INFOSYS", 3, False, "BSE"), "NIFTY 50": Instrument("NIFTY 50", "NIFTY 50", 4, True)}
    monkeypatch.setattr(kite_mod, "instruments", lambda: insts)
    got = web.get("/ma/symbols?q=inf").json()
    assert [g["symbol"] for g in got] == ["INFY", "INFIBEAM"] and got[0]["name"] == "Infosys"  # NSE only
    assert web.get("/ma/symbols?q=nifty").json() == [{"symbol": "NIFTY 50", "name": "Nifty 50", "index": True}]
    assert web.get("/ma/symbols?q=").json() == []


def test_scanner_sends_only_the_first_entry_after_a_crossover(kite, sent):
    from app.market import load_settings
    s = load_settings()
    # Falling for 30 days, then climbing with a red pullback every six days: one crossover up, many pullbacks.
    rows = [(130 - i, 131 - i, 129 - i, 129.5 - i) for i in range(30)]
    rows += [(100.5 + i, 101 + i, 97 + i, 99.6 + i) if i % 6 == 3 else (100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(36)]
    kite.candles = daily(rows)
    w = crossover.walk(kite.candles, crossover.Rules("sma", 2, "sma", 5, once=False))
    assert len([c for c in w.crossovers if c[1] == "buy"]) == 1 and len(w.entries) >= 4
    sc = _scanner(rules={**_scanner()["rules"], "once": True}, armed_at="2026-07-01T09:00:00+05:30")
    at = datetime(2026, 9, 30, 15, 31, tzinfo=IST)
    sc = ma_scanner.evaluate(sc, kite, s, at, s.close_at(at))
    entries = [x for x in sent if x[0].startswith("🚀")]
    assert len(entries) == 1 and sc["state"] == {}  # the later pullbacks set nothing up


def test_four_hour_candles_are_built_from_hourly_ones():
    from app.market import load_settings
    s = load_settings()
    day = datetime(2026, 9, 30, tzinfo=IST)
    hours = [Candle(day.replace(hour=9, minute=15) + timedelta(hours=k), 100 + k, 102 + k, 99 + k, 101 + k) for k in range(7)]

    class Hourly:
        def candles_range(self, token, tf, start, end):
            assert tf == "1h"  # Kite has no 4-hour candles
            return hours

    # Mid-afternoon: the 9:15-13:15 candle is done, the 13:15 one isn't.
    got = ma_backtest.fetch(Hourly(), 1, "4h", day.date(), day.date(), s, day.replace(hour=14, minute=30))
    assert [(c.start.strftime("%H:%M"), c.open, c.high, c.low, c.close) for c in got] == [("09:15", 100, 105, 99, 104)]
    got = ma_backtest.fetch(Hourly(), 1, "4h", day.date(), day.date(), s, day.replace(hour=15, minute=31))
    assert [(c.start.strftime("%H:%M"), c.open, c.high, c.low, c.close) for c in got] == \
        [("09:15", 100, 105, 99, 104), ("13:15", 104, 108, 103, 107)]
    assert ma_backtest.candle_end(got[1], "4h", s).strftime("%H:%M") == "15:30"
    assert ma_backtest.last_boundary("4h", s, day.replace(hour=13, minute=0)) is None
    assert ma_backtest.last_boundary("4h", s, day.replace(hour=14, minute=0)).strftime("%H:%M") == "13:15"
    assert ma_backtest.last_boundary("4h", s, day.replace(hour=15, minute=40)).strftime("%H:%M") == "15:30"
    assert ma_backtest.label("4h") == "4 hour"
    # A year of 4-hour candles, plus the candles before it the averages need, is two hourly requests per stock.
    assert ma_backtest.requests_needed(500, "4h", date(2025, 10, 22), date(2026, 10, 22), crossover.Rules()) == 1000


def test_four_hour_is_offered(web):
    assert '<option value="4h" >4 hour</option>' in web.get("/ma?tab=backtest").text
    r = web.post("/ma/scanners", data={**SETUP, "tf": "4h", "universe_kind": "list", "universe_list": "nifty500",
                                       "stop": "candle", "rr": "2"})
    assert "Scanner saved" in r.headers["HX-Trigger"] and "on 4 hour candles" in r.text


def test_second_entry_needs_first_entry_only(web):
    base = {**SETUP, "stop": "candle", "rr": "2"}
    web.post("/ma/scanners", data={**base, "once": "1", "second": "1", "name": "with second"})
    web.post("/ma/scanners", data={**base, "second": "1", "name": "re-entries"})
    rules = {sc["name"]: sc["rules"] for sc in store.list("ma_scanners", user="tara")}
    assert (rules["with second"]["once"], rules["with second"]["second"]) == (True, True)
    assert (rules["re-entries"]["once"], rules["re-entries"]["second"]) == (False, False)
    assert "a second after a stop-out" in web.get("/ma?tab=scanners").text
    for sc in store.list("ma_scanners", user="tara"):
        store.delete("ma_scanners", sc["id"])


def _screen_params(day, **rules):
    from app import ma_screen
    return ma_screen.Params({"symbols": ["INFY"]}, "1d", day, crossover.Rules("sma", 2, "sma", 5, once=False, **rules), "candle", 2)


def test_screen_sorts_a_stock_into_triggered_formed_or_ready():
    from app import ma_screen
    formed = ma_screen.classify("INFY", daily(ROWS), _screen_params(date(2026, 9, 30)))
    assert (formed["group"], formed["level"], formed["stop"], formed["target"]) == ("formed", 166.5, 164, 171.5)
    assert formed["away"] == pytest.approx((166.5 - 164.5) / 164.5 * 100)
    # The next day it trades through: triggered at the level.
    triggered = ma_screen.classify("INFY", daily(ROWS + [BREAK[1]], DAYS + [BREAK[0]]), _screen_params(BREAK[0]))
    assert (triggered["group"], triggered["price"], triggered["gap"]) == ("triggered", 166.5, False)
    # The next day it stays under the level: still waiting.
    quiet = (165, 166, 164.2, 165.5)
    ready = ma_screen.classify("INFY", daily(ROWS + [quiet], DAYS + [BREAK[0]]), _screen_params(BREAK[0]))
    assert (ready["group"], ready["trigger_at"]) == ("ready", "2026-09-30T00:00:00+05:30")
    # A gap above the level: triggered on a gap, entered at the open.
    gap = ma_screen.classify("INFY", daily(ROWS + [(168, 170, 167.5, 169)], DAYS + [BREAK[0]]), _screen_params(BREAK[0]))
    assert (gap["group"], gap["gap"], gap["price"]) == ("triggered", True, 168)


def test_screen_end_to_end(web, kite):
    from app import ma_screen
    page = web.get("/ma?tab=screen").text
    assert "Scan now" in page and 'name="day" type="date"' in page
    r = web.post("/ma/screen", data={**SETUP, "day": "2026-09-30", "stop": "candle", "rr": "2"})
    assert "Scanning 1 stock as of 30 Sep" in r.headers["HX-Trigger"]
    for _ in range(100):
        if ma_screen.current("tara").status != "running":
            break
        clock.sleep(0.05)
    screen = ma_screen.current("tara")
    assert screen.status == "done" and [r["group"] for r in screen.rows] == ["formed"]
    html = web.get("/ma/screen/status").text
    assert "Trigger candle formed" in html and "INFY" in html and "1 stock with a setup" in html
    chart = web.get("/ma/screen/chart?symbol=INFY").json()
    assert chart["row"]["level"] == 166.5 and chart["trigger"] == "2026-09-30" and chart["candles"][-1]["time"] == "2026-09-30"


def test_signals_feed_shows_what_the_scanners_sent(web, kite, sent):
    from app.market import load_settings
    s = load_settings()
    at = datetime(2026, 9, 30, 15, 31, tzinfo=IST)
    ma_scanner.evaluate(_scanner(user="tara"), kite, s, at, s.close_at(at))
    items = ma_scanner.feed("tara", "2026-09-30")  # newest first; earlier tests added to the same day
    assert (items[0]["kind"], items[0]["symbol"], items[0]["level"], items[0]["scanner"]) == ("setup", "INFY", 166.5, "Infy daily")
    assert items[0]["stop"] == 164 and items[0]["target"] == 171.5
    html = web.get("/ma/signals?day=2026-09-30").text
    assert "Signals" in html and "INFY" in html
    store.delete("ma_scanners", "s1")


def test_a_run_ends_when_the_stock_lists_cant_be_had(web, kite, monkeypatch):
    from app import ma_screen
    def down():
        raise kite_mod.KiteBlocked("Kite asked this server to wait until 8:18 PM before more requests.")
    for mod in (ma_backtest, ma_screen):
        monkeypatch.setattr(mod, "instruments", down)
    web.post("/ma/screen", data={**SETUP, "day": "2026-09-30", "stop": "candle", "rr": "2"})
    web.post("/ma/backtest", data={**SETUP, "start": "2026-08-01", "end": "2026-09-30", "stop": "candle", "rr": "2", "book": "100"})
    for _ in range(100):
        if ma_screen.current("tara").status != "running" and ma_backtest.current("tara").status != "running":
            break
        clock.sleep(0.05)
    for job in (ma_screen.current("tara"), ma_backtest.current("tara")):
        assert job.status == "failed" and "wait until 8:18 PM" in job.error


def test_daily_scanners_are_read_at_3_45(kite, sent, monkeypatch):
    from app import ma_screen
    from app.market import load_settings
    s = load_settings()
    calls = []
    monkeypatch.setattr(ma_scanner, "evaluate", lambda sc, client, s, now, upto: calls.append((now.strftime("%H:%M"), upto.strftime("%d %H:%M"))))
    monkeypatch.setattr(ma_scanner, "_ensure_session", lambda u, n, now: kite)
    _scanner(evaluated_at="2026-09-29T15:30:00+05:30")
    # 3:31 and 3:44: nothing new (yesterday's candle is the latest read); 3:46: today's. (evaluate is stubbed, so
    # the scanner isn't marked as read: 4:10 would read it again.) 4:20 is past the 45 minutes after the close.
    for hh, mm in [(15, 31), (15, 44), (15, 46), (16, 10), (16, 20)]:
        ma_scanner.run_candles(datetime(2026, 9, 30, hh, mm, tzinfo=IST))
    assert calls == [("15:46", "30 15:30"), ("16:10", "30 15:30")]
    # Scan now's default day switches at 3:45 too.
    assert ma_screen.last_session(s, datetime(2026, 9, 30, 15, 40, tzinfo=IST)).isoformat() == "2026-09-29"
    assert ma_screen.last_session(s, datetime(2026, 9, 30, 15, 45, tzinfo=IST)).isoformat() == "2026-09-30"
    store.delete("ma_scanners", "s1")


ADX_SETUP = {"universe_kind": "symbols", "symbols": "infy", "tf": "1d", "adx_len": "14", "atr_len": "14", "adx_min": "25",
             "di_min": "25", "di_max": "20", "atr_mult": "1", "sides": "both"}


def test_adx_trend_page_permissions_and_its_own_scanners(web, kite):
    assert web.get("/adx").status_code == 403  # tara has only MA pullback
    # The super admin sees both, under one heading in the sidebar.
    web.post("/logout")
    web.post("/login", data={"username": "boss", "password": "boss-pass-123"})
    web.post("/admin/users", data={"username": "ravi", "name": "Ravi", "password": "ravi-pass-1", "modules": ["adx"]})
    web.post("/logout")
    web.post("/login", data={"username": "ravi", "password": "ravi-pass-1"})
    store.put("brokers", "ravi", {"mode": "enctoken", "status": "connected", "enctoken": "x"})
    page = web.get("/adx?tab=scanners").text
    assert "Indicator scanners" in page and "ADX trend" in page and 'name="adx_len"' in page and "MA pullback" not in page
    assert web.get("/ma").status_code == 403
    r = web.post("/adx/scanners", data={**ADX_SETUP, "stop": "candle", "rr": "2", "name": "Infy ADX"})
    assert "Scanner saved" in r.headers["HX-Trigger"] and "Infy ADX" in r.text
    sc = store.list("ma_scanners", user="ravi")[0]
    assert sc["strategy"] == "adx" and sc["rules"]["adx_min"] == 25 and sc["rules"]["di_max"] == 20
    assert ma_scanner.describe(sc) == "INFY · ADX 14 > 25 · DI 25 / 20 on daily candles"
    # Bad thresholds are refused.
    r = web.post("/adx/scanners", data={**ADX_SETUP, "di_max": "30", "stop": "candle", "rr": "2"})
    assert "under the trending DI" in r.headers["HX-Trigger"]
    store.delete("ma_scanners", sc["id"])


def test_adx_backtest_and_scan_through_the_pages(web, kite):
    web.post("/logout")
    web.post("/login", data={"username": "boss", "password": "boss-pass-123"})
    store.put("brokers", "boss", {"mode": "enctoken", "status": "connected", "enctoken": "x"})
    assert "ADX trend" in web.get("/adx?tab=backtest").text and "Exit at the monthly expiry" in web.get("/adx?tab=backtest").text
    r = web.post("/adx/backtest", data={**ADX_SETUP, "start": "2026-08-01", "end": "2026-09-30", "stop": "candle", "rr": "2",
                                        "book": "50", "trend_exit": "1", "expiry_exit": "1", "fill": "open"})
    assert "Backtest started on 1 stock" in r.headers["HX-Trigger"]
    for _ in range(100):
        if ma_backtest.current("boss", "adx").status != "running":
            break
        clock.sleep(0.05)
    run = ma_backtest.current("boss", "adx")
    assert run.status == "done" and run.params.strategy == "adx" and run.params.exits.trend_exit and run.params.exits.expiry_exit
    assert ma_backtest.current("boss", "ma") is None or ma_backtest.current("boss", "ma") is not run  # kept apart from MA runs
    html = web.get("/adx/backtest/status").text
    assert "Backtest with real Kite prices" in html and "SL 1 ATR (14) past the signal candle" in html
    chart = web.get("/adx/backtest/chart?symbol=INFY").json()
    assert chart["overlays"] == [] and [ln["label"] for ln in chart["panel"]["lines"]] == ["ADX", "+DI", "−DI"]
    assert chart["panel"]["levels"] == [20, 25]
    r = web.post("/adx/screen", data={**ADX_SETUP, "day": "2026-09-30", "stop": "candle", "rr": "2"})
    assert "Scanning 1 stock" in r.headers["HX-Trigger"]
    from app import ma_screen
    for _ in range(100):
        if ma_screen.current("boss", "adx").status != "running":
            break
        clock.sleep(0.05)
    assert ma_screen.current("boss", "adx").status == "done"
    assert "Signal candle formed" in web.get("/adx/screen/status").text


def test_live_adx_scanner_sends_a_signal_candle_setup(kite, sent, monkeypatch):
    from app import adx
    from app.market import load_settings
    s = load_settings()
    n = len(ROWS)
    # Quiet throughout, then the last candle (30 Sep, high 166.5, low 164) meets all three: a fresh signal candle.
    monkeypatch.setattr(adx, "indicators", lambda c, a, b: {
        "adx": [20.0] * (len(c) - 1) + [30.0], "pdi": [20.0] * (len(c) - 1) + [30.0], "mdi": [18.0] * (len(c) - 1) + [10.0],
        "atr": [2.0] * len(c)})
    sc = _scanner(strategy="adx", name="Infy ADX", rules={"adx_len": 14, "atr_len": 14, "adx_min": 25, "di_min": 25, "di_max": 20,
                                                            "atr_mult": 1, "sides": "both"})
    at = datetime(2026, 9, 30, 15, 46, tzinfo=IST)
    sc = ma_scanner.evaluate(sc, kite, s, at, s.close_at(at))
    assert sent[-1][0] == "📈 INFY: buy above 166.50" and "Signal candle closed" in sent[-1][1]
    assert "INFY buy above ₹166.50 · SL ₹162.00 · target ₹175.50 (1:2)" in sent[-1][1]
    assert sc["state"]["INFY"]["candle_stop"] == 162 and n == len(ROWS)
    items = ma_scanner.feed("tara", "2026-09-30", "adx")
    assert items[0]["strategy"] == "adx" and items[0]["scanner"] == "Infy ADX"
    assert all(i.get("strategy", "ma") == "ma" for i in ma_scanner.feed("tara", "2026-09-30", "ma"))
    store.delete("ma_scanners", "s1")
