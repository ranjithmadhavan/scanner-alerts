"""MA crossover: the backtest end to end on fake Kite candles, and the live scanner's two jobs."""

import time as clock
from datetime import date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import brokers, config, crossover, ma_backtest, ma_scanner, notify, universe
from app.kite import Candle, Instrument
from app.market import IST
from app.routes import ma as routes
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
    for mod in (ma_backtest, ma_scanner, routes, universe):
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
    expected, _ = crossover.trades(kite.candles, crossover.Rules("sma", 2, "sma", 5), crossover.Exits(rr=2),
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
    assert chart["candles"][0]["time"] >= "2026-08-01" and chart["fast"] and chart["slow"] and chart["slow_label"] == "5 SMA"
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
    assert "Run backtest" in page and '<option value="1d" selected>' in page and ">INFY</textarea>" in page
    web.post(f"/ma/scanners/{sc['id']}/delete")
    assert store.list("ma_scanners", user="tara") == []


def _scanner(**more):
    sc = {"id": "s1", "user": "tara", "name": "Infy daily", "spec": {"symbols": ["INFY"]}, "tf": "1d",
          "rules": {"fast_kind": "sma", "fast_len": 2, "slow_kind": "sma", "slow_len": 5, "touch": "", "sides": "both"},
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
    assert ma_scanner.latest_close("1d", s, after_close) == s.close_at(after_close)
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
    assert 'name="fast_len" type="number" min="1" max="400" step="1" required value="10"' in form
    assert 'name="slow_len" type="number" min="1" max="400" step="1" required value="20"' in form
    assert form.count('<option value="ema" selected>') == 2
    assert checked("touch") == "fast" and checked("sides") == "buy"
    assert checked("breakeven") == "1" and checked("ma_exit") == "1"
    assert not re.search(r'name="square_off" value="1"[^>]*checked', form)  # positional, not intraday
    assert 'name="risk" type="number" min="1" step="1" value="1000"' in form
    assert 'name="book" type="number" min="0" max="100" step="5" value="50"' in form  # half at the target, the rest runs
    # The live scanner form keeps its own defaults.
    scanners = web.get("/ma?tab=scanners").text
    assert '<option value="15m" selected>' in scanners
