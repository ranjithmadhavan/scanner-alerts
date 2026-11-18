"""Custom scanners through the page: build, Scan now, save, and the live job alerting on new matches only."""

import json
import time as clock
from datetime import date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import brokers, config, custom_scan, notify, universe, webhooks
from app import kite as kite_mod
from app.kite import Candle, Instrument
from app.market import IST, load_settings
from app.routes import indicators as indicator_routes
from app.store import store

INFY = Instrument("INFY", "INFOSYS", 1, False)
TCS = Instrument("TCS", "TATA CONSULTANCY", 2, False)
LAST = date(2026, 9, 30)  # a Wednesday


def series(start_price: float, step: float, end: date = LAST, days: int = 120) -> list[Candle]:
    out, d, p = [], end - timedelta(days=days), start_price
    while d <= end:
        if d.weekday() < 5:
            out.append(Candle(datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST), p, p + 1, p - 1, p + step / 2, 1000))
            p += step
        d += timedelta(days=1)
    return out


class FakeKite:
    def __init__(self):
        self.by_token = {1: series(100, 1), 2: series(300, -1)}  # INFY rising, TCS falling

    def profile(self):
        return {}

    def candles_range(self, token, tf, start, end):
        assert tf == "1d"
        return [c for c in self.by_token[token] if start <= c.start.date() <= end]


@pytest.fixture
def kite(monkeypatch):
    fake = FakeKite()
    insts = {"INFY": INFY, "TCS": TCS}
    for mod in (custom_scan, indicator_routes, universe, kite_mod):
        monkeypatch.setattr(mod, "instruments", lambda: insts)
    monkeypatch.setattr(brokers, "client_for", lambda u, doc=None: fake)
    monkeypatch.setattr(universe.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("offline")))
    monkeypatch.setattr(custom_scan, "now_ist", lambda: datetime(2026, 9, 30, 16, 0, tzinfo=IST))
    custom_scan._daily.clear()
    custom_scan._past.clear()
    return fake


@pytest.fixture
def web(kite, monkeypatch):
    monkeypatch.setattr(config, "SUPERADMIN_USERNAME", "boss")
    monkeypatch.setattr(config, "SUPERADMIN_PASSWORD", "boss-pass-123")
    from app.main import app
    with TestClient(app) as c:
        c.post("/login", data={"username": "boss", "password": "boss-pass-123"})
        c.post("/admin/users", data={"username": "tara", "name": "Tara", "password": "tara-pass-1", "modules": ["scans"]})
        c.post("/logout")
        c.post("/login", data={"username": "tara", "password": "tara-pass-1"})
        store.put("brokers", "tara", {"mode": "enctoken", "status": "connected", "enctoken": "x"})
        yield c
    for sc in store.list(custom_scan.COL):
        store.delete(custom_scan.COL, sc["id"])
    store.delete("users", "tara")  # other test files make their own tara
    store.delete("brokers", "tara")


ABOVE_EMA = {"op": "all", "items": [{"lhs": {"kind": "price", "field": "close"}, "cmp": ">",
                                     "rhs": {"kind": "ind", "ind": "ema", "params": {"n": 20}}}]}
FORM = {"universe_kind": "symbols", "symbols": "INFY, TCS", "tf": "1d", "name": "Above EMA", "logic": json.dumps(ABOVE_EMA)}


def test_page_builder_and_describe(web):
    page = web.get("/scans").text
    assert "Custom scanners" in page and "data-scan-builder" in page and "Breaks above the previous day" in page
    # A new scan starts from a filled-in condition: the builder draws every indicator setting.
    assert '"params": {"n": 20, "src": "close"}' in page
    assert '<option value="5m" selected>' in page and '<option value="1m" >1 min</option>' in page  # new scans start on 5 min
    assert web.get("/ma").status_code == 403
    assert web.post("/scans/describe", data={"logic": json.dumps(ABOVE_EMA)}).json() == {"text": "Close > EMA(20)"}
    assert "at least one" in web.post("/scans/describe", data={"logic": '{"op": "all", "items": []}'}).json()["error"]
    two = {"op": "all", "items": [ABOVE_EMA["items"][0], {**ABOVE_EMA["items"][0], "off": True}]}
    assert web.post("/scans/describe", data={"logic": json.dumps(two)}).json() == {"text": "Close > EMA(20) (1 condition switched off)"}


def test_starters_are_valid_and_describe():
    from app.routes.scans import STARTERS
    from app.scan_expr import describe
    assert len(STARTERS) >= 5
    assert describe(STARTERS[0][2]) == "Close crosses above Prev daily high"
    assert describe(STARTERS[4][2]) == "Close ≥ 98% of weekly Highest(52)"


def test_scan_now_finds_the_matching_stocks_and_charts_them(web):
    r = web.post("/scans/screen", data=FORM)
    assert 'id="scan-screen"' in r.text  # running, or already done (the fake Kite is quick)
    for _ in range(100):
        if custom_scan.current("tara").status != "running":
            break
        clock.sleep(0.05)
    screen = custom_scan.current("tara")
    assert screen.status == "done" and [row["symbol"] for row in screen.rows] == ["INFY"]
    html = web.get("/scans/screen/status").text
    assert "1 stock match, of 2 scanned" in html and "EMA(20)" in html and 'data-scan-symbol="INFY"' in html
    chart = web.get("/scans/screen/chart?symbol=INFY").json()
    assert chart["candles"][-1]["time"] == "2026-09-30" and chart["lines"][0]["name"] == "EMA(20)"
    assert web.get("/scans/screen/chart?symbol=TCS").status_code == 404
    assert "Pick a candle size" in web.post("/scans/screen", data={**FORM, "tf": "2d"}).headers["HX-Trigger"]


def test_saved_scanner_alerts_only_on_new_matches(web, kite, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda user, ch, subject, body: sent.append((subject, body)) or {"telegram": "sent"})
    r = web.post("/scans/scanners", data={**FORM, "channels": ["telegram"]})
    assert "Scanner saved" in r.headers["HX-Trigger"] and "Close &gt; EMA(20)" in r.text
    sc = store.list(custom_scan.COL, user="tara")[0]
    s = load_settings()

    def close_on(d: date):
        upto = s.close_at(datetime(d.year, d.month, d.day, 12, tzinfo=IST))
        return custom_scan.evaluate(store.get(custom_scan.COL, sc["id"]), kite, s, upto + timedelta(minutes=20), upto)

    close_on(date(2026, 9, 29))
    assert len(sent) == 1 and sent[0][0] == "🔎 INFY matched Above EMA" and "Close > EMA(20)" in sent[0][1]
    close_on(LAST)  # still matching: nothing new
    assert len(sent) == 1 and store.get(custom_scan.COL, sc["id"])["matched"] == ["INFY"]
    # TCS turns up sharply on the last day, INFY drops: TCS is new, INFY drops out quietly.
    kite.by_token[2][-1] = Candle(kite.by_token[2][-1].start, 190, 400, 189, 400, 1000)
    kite.by_token[1][-1] = Candle(kite.by_token[1][-1].start, 200, 201, 50, 50, 1000)
    close_on(LAST)
    assert len(sent) == 2 and sent[1][0] == "🔎 TCS matched Above EMA"
    assert store.get(custom_scan.COL, sc["id"])["matched"] == ["TCS"]
    from app.routes import scans as scan_routes
    monkeypatch.setattr(scan_routes, "now_ist", lambda: datetime(2026, 9, 30, 16, 0, tzinfo=IST))
    today, yesterday = web.get("/scans/feed").text, web.get("/scans/feed?day=2026-09-29").text
    assert "TCS" in today and "INFY" not in today and "INFY" in yesterday

    # Test message, pause, edit (a different scan starts afresh), and someone else's can't be touched.
    assert "Test sent to Telegram" in web.post(f"/scans/scanners/{sc['id']}/test").headers["HX-Trigger"]
    web.post(f"/scans/scanners/{sc['id']}/pause")
    assert store.get(custom_scan.COL, sc["id"])["status"] == "paused"
    r = web.post(f"/scans/scanners/{sc['id']}", data={**FORM, "tf": "1h", "channels": ["telegram"]})
    assert "starts afresh" in r.headers["HX-Trigger"] and store.get(custom_scan.COL, sc["id"])["matched"] == []
    store.put(custom_scan.COL, "theirs", {**sc, "id": "theirs", "user": "someone"})
    assert "gone" in web.post("/scans/scanners/theirs/pause").headers["HX-Trigger"]
    store.delete(custom_scan.COL, "theirs")


def test_live_job_reads_daily_scanners_after_the_close(kite, monkeypatch):
    calls = []
    monkeypatch.setattr(custom_scan, "evaluate", lambda sc, client, s, now, upto: calls.append((now.strftime("%H:%M"), upto.strftime("%d %H:%M"))))
    monkeypatch.setattr(custom_scan, "_ensure_session", lambda u, n, now: kite)
    store.put(custom_scan.COL, "c1", {"id": "c1", "user": "tara", "status": "active", "tf": "1d", "spec": {"symbols": ["INFY"]},
                                      "logic": ABOVE_EMA, "armed_at": "2026-09-29T10:00:00+05:30",
                                      "evaluated_at": "2026-09-29T15:30:00+05:30", "matched": []})
    for hh, mm in [(15, 31), (15, 46), (16, 20)]:
        custom_scan.run_candles(datetime(2026, 9, 30, hh, mm, tzinfo=IST))
    assert calls == [("15:46", "30 15:30")]
    store.delete(custom_scan.COL, "c1")


def test_live_limit_and_scanner_count(web, monkeypatch):
    monkeypatch.setattr(universe, "symbols", lambda key, today, refresh=False: [f"S{i}" for i in range(150)])
    r = web.post("/scans/scanners", data={**FORM, "universe_kind": "list", "universe_list": "nifty200", "tf": "5m"})
    assert "up to 100 stocks" in r.headers["HX-Trigger"]
    r = web.post("/scans/scanners", data={**FORM, "universe_kind": "list", "universe_list": "nifty200", "tf": "1m"})
    assert "up to 20 stocks" in r.headers["HX-Trigger"]
    r = web.post("/scans/scanners", data={**FORM, "universe_kind": "list", "universe_list": "nifty200", "tf": "3m"})
    assert "up to 60 stocks" in r.headers["HX-Trigger"]
    for _ in range(custom_scan.MAX_SCANNERS):
        web.post("/scans/scanners", data=FORM)
    assert "up to 5" in web.post("/scans/scanners", data=FORM).headers["HX-Trigger"]


def test_intraday_scan_reads_daily_candles_once_a_day(kite):
    """A 15-minute scan on 'prev daily high': earlier days' daily candles are read once, today's 15-minute ones each close."""
    wed = datetime(2026, 9, 30, 9, 15, tzinfo=IST)
    fifteen = [Candle(wed + timedelta(minutes=15 * i), p, p + 0.2, p - 0.2, p, 10) for i, p in enumerate([180, 185, 230])]
    calls = []

    class Intraday(FakeKite):
        def candles_range(self, token, tf, start, end):
            calls.append((tf, start, end))
            if tf == "15m":
                return [c for c in fifteen if start <= c.start.date() <= end]
            return super().candles_range(token, tf, start, end)

    client = Intraday()
    logic = {"op": "all", "items": [{"lhs": {"kind": "price", "field": "close"}, "cmp": "crosses_above",
                                     "rhs": {"kind": "price", "field": "high", "tf": "1d", "ago": 1}}]}
    from app.scan_expr import validate, passes
    sc = {"tf": "15m", "logic": validate(logic)}
    s = load_settings()
    prev_high = [c for c in client.by_token[1] if c.start.date() < LAST][-1].high
    assert 185 < prev_high < 230
    seen = []
    for k in (2, 3):  # after the 2nd and 3rd candles
        upto = wed + timedelta(minutes=15 * k)
        stock = custom_scan.read(client, 1, sc, s, upto + timedelta(seconds=30), upto, live=True)
        assert stock.main[-1].start == fifteen[k - 1].start and stock.daily[-1].start.date() < LAST
        seen.append(passes(sc["logic"], stock))
    assert seen == [False, True]
    assert [tf for tf, *_ in calls].count("1d") == 1


def _wait(user="tara"):
    for _ in range(200):
        if custom_scan.current(user).status != "running":
            return custom_scan.current(user)
        clock.sleep(0.05)
    raise AssertionError("scan still running")


def test_scan_a_date_range_lists_each_match(web):
    """Every candle in the range is judged; each one a stock passed on is listed, newest first, with a chart and a CSV."""
    rng = {**FORM, "when": "range", "from": "2026-09-21", "to": "2026-09-30", "first_only": "1"}
    web.post("/scans/screen", data=rng)
    screen = _wait()
    assert screen.status == "done" and screen.first == date(2026, 9, 21)
    days = [r["at"][:10] for r in screen.rows]
    assert {r["symbol"] for r in screen.rows} == {"INFY"} and len(days) == 8  # each trading day, INFY rising
    assert sorted(days)[0] == "2026-09-21" and sorted(days)[-1] == "2026-09-30"
    html = web.get("/scans/screen/status").text
    assert "8 matches in 1 stock, of 2 scanned" in html and "Download CSV" in html and 'data-scan-at="2026-09-21' in html
    body = html[html.index("<tbody"):]
    assert body.index("Wed 30 Sep") < body.index("Mon 21 Sep")  # newest first
    at = next(r["at"] for r in screen.rows if r["at"].startswith("2026-09-23"))
    chart = web.get("/scans/screen/chart", params={"symbol": "INFY", "at": at}).json()
    assert chart["mark"] == "2026-09-23" and chart["candles"][-1]["time"] == "2026-09-30"
    got = web.get("/scans/screen/csv")
    assert got.headers["content-type"].startswith("text/csv")
    lines = got.text.strip().splitlines()
    assert lines[0].startswith("Time,Stock,Close,Change %,EMA(20)") and len(lines) == 9 and lines[1].startswith("2026-09-21")
    # Problems are said before anything is read.
    bad = web.post("/scans/screen", data={**rng, "from": "2026-09-30", "to": "2026-09-21"}).headers["HX-Trigger"]
    assert "start date is after the end date" in bad
    bad = web.post("/scans/screen", data={**rng, "tf": "5m", "from": "2026-07-01"}).headers["HX-Trigger"]
    assert "up to 30 days" in bad
    assert "first and last day" in web.post("/scans/screen", data={**rng, "from": ""}).headers["HX-Trigger"]


def test_range_without_first_only_lists_every_candle(kite):
    """On intraday candles, a stock that keeps passing is listed at each candle, or only once a day with first_only."""
    wed = datetime(2026, 9, 30, 9, 15, tzinfo=IST)
    five = [Candle(wed + timedelta(minutes=5 * i), p, p + 0.2, p - 0.2, p, 10) for i, p in enumerate([100, 101, 102, 103])]
    from app.scan_expr import Stock, validate
    logic = validate({"op": "all", "items": [{"lhs": {"kind": "price", "field": "close"}, "cmp": ">",
                                              "rhs": {"kind": "num", "value": 100.5}}]})
    sc = {"tf": "5m", "logic": logic}
    every = custom_scan.hits("X", sc, Stock("5m", five), LAST, LAST, first_only=False)
    assert [r["close"] for r in every] == [101, 102, 103]
    assert [r["close"] for r in custom_scan.hits("X", sc, Stock("5m", five), LAST, LAST, first_only=True)] == [101]


def test_once_a_day_scanner_sends_the_full_list_at_its_time(web, kite, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda user, ch, subject, body: sent.append((subject, body)) or {"telegram": "sent"})
    monkeypatch.setattr(custom_scan, "_ensure_session", lambda u, n, now: kite)
    monkeypatch.setattr(universe, "symbols", lambda key, today, refresh=False: [f"S{i}" for i in range(150)])
    big = {**FORM, "universe_kind": "list", "universe_list": "nifty200", "tf": "5m", "run_mode": "daily", "run_at": "15:45"}
    assert "Scanner saved. Each trading day at 3:45 PM" in web.post("/scans/scanners", data=big).headers["HX-Trigger"]  # no live cap
    assert "after the market opens" in web.post("/scans/scanners", data={**big, "run_at": "09:00"}).headers["HX-Trigger"]
    for sc in store.list(custom_scan.COL):
        store.delete(custom_scan.COL, sc["id"])

    r = web.post("/scans/scanners", data={**FORM, "run_mode": "daily", "run_at": "15:45", "channels": ["telegram"]})
    assert "runs once a day at 3:45 PM" in r.text
    sc = store.list(custom_scan.COL, user="tara")[0]
    assert sc["run_at"] == "15:45"
    store.update(custom_scan.COL, sc["id"], {"armed_at": "2026-09-28T10:00:00+05:30"})

    custom_scan.run_candles(datetime(2026, 9, 29, 15, 50, tzinfo=IST))  # not judged at candle closes
    custom_scan.run_daily(datetime(2026, 9, 29, 15, 40, tzinfo=IST))  # not yet
    assert sent == []
    custom_scan.run_daily(datetime(2026, 9, 29, 15, 50, tzinfo=IST))
    custom_scan.run_daily(datetime(2026, 9, 29, 15, 55, tzinfo=IST))  # once a day
    assert [s for s, _ in sent] == ["🔎 Above EMA: 1 stock match (as of 3:30 PM)"] and "INFY" in sent[0][1]
    custom_scan.run_daily(datetime(2026, 9, 30, 15, 50, tzinfo=IST))  # the next day: the full list again, not just new ones
    assert len(sent) == 2 and "INFY" in sent[1][1]
    assert store.get(custom_scan.COL, sc["id"])["ran_on"] == "2026-09-30"

    # Saved or resumed after the day's time, it first runs the next day.
    store.update(custom_scan.COL, sc["id"], {"armed_at": "2026-10-01T16:00:00+05:30"})
    custom_scan.run_daily(datetime(2026, 10, 1, 16, 5, tzinfo=IST))
    assert len(sent) == 2
    # Back to every candle close.
    r = web.post(f"/scans/scanners/{sc['id']}", data={**FORM, "run_mode": "close", "channels": ["telegram"]})
    assert "starts afresh" in r.headers["HX-Trigger"] and store.get(custom_scan.COL, sc["id"])["run_at"] == ""


def test_once_a_day_with_nothing_matching_still_reports(kite, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda user, ch, subject, body: sent.append(subject) or {})
    monkeypatch.setattr(custom_scan, "_ensure_session", lambda u, n, now: kite)
    from app.scan_expr import validate
    below = {"op": "all", "items": [{"lhs": {"kind": "price", "field": "close"}, "cmp": "<", "rhs": {"kind": "num", "value": 1}}]}
    store.put(custom_scan.COL, "d1", {"id": "d1", "user": "tara", "name": "Penny", "status": "active", "tf": "1d",
                                      "spec": {"symbols": ["INFY", "TCS"]}, "logic": validate(below), "run_at": "15:45", "channels": [],
                                      "armed_at": "2026-09-28T10:00:00+05:30", "evaluated_at": "", "matched": []})
    custom_scan.run_daily(datetime(2026, 9, 30, 15, 46, tzinfo=IST))
    assert sent == ["🔎 Penny: no stock matches (as of 3:30 PM)"]
    store.delete(custom_scan.COL, "d1")
