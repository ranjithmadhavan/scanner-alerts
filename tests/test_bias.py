"""Fractal bias: signals across the Nifty 50, how they've gone since, and the day's reading."""

from datetime import date, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from app import bias, brokers, config, notify
from app.kite import Candle, Instrument
from app.market import IST, MarketSettings
from app.security import hash_password
from app.store import store

S = MarketSettings(time(9, 15), time(15, 30), 60)
FRI, MON = date(2026, 10, 2), date(2026, 10, 5)


def at(day: date, h: int, m: int) -> datetime:
    return datetime.combine(day, time(h, m), IST)


def friday() -> list[Candle]:
    """A quiet day, 15-minute candles, with one dip to 95 in the 10:45 half-hour: a fractal low."""
    out = []
    for k in range(25):
        start = at(FRI, 9, 15) + timedelta(minutes=15 * k)
        out.append(Candle(start, 102, 105, 95 if k in (6, 7) else 100, 102))
    return out


def monday() -> list[Candle]:
    """Sweeps the lows at 9:15 and holds at 9:30 (a sweep that holds: potential buy); 10:00 breaks the stop."""
    return [
        Candle(at(MON, 9, 15), 102, 103, 94, 101),
        Candle(at(MON, 9, 30), 101, 104, 100, 103),
        Candle(at(MON, 9, 45), 103, 104, 101, 103),       # short of the 105 target
        Candle(at(MON, 10, 0), 103, 103, 93, 95),
        Candle(at(MON, 10, 15), 95, 96, 92, 93),
    ]


def test_15_minute_candles_pair_into_half_hours():
    halves = bias.to_30m(friday()[:5], S)
    assert [(c.start.strftime("%H:%M"), c.low) for c in halves] == [("09:15", 100), ("09:45", 100), ("10:15", 100)]
    assert bias.to_30m([Candle(at(FRI, 9, 15), 1, 5, 1, 2), Candle(at(FRI, 9, 30), 2, 7, 0.5, 6)], S)[0] == \
        Candle(at(FRI, 9, 15), 1, 7, 0.5, 6)


def test_a_buy_holds_then_is_stopped_out():
    cfg = bias.DEFAULTS
    candles = friday() + monday()
    held = bias.signals("INFY", candles, MON, S, cfg, at(MON, 10, 0) + timedelta(seconds=30))
    assert sorted((g["signal"], g["trigger"], g["level"], g["status"]) for g in held) == \
        [("buy", "confirm", 95, "held"), ("buy", "confirm", 100, "held")]
    assert held[0]["stop"] == 94 and held[0]["target"] == 105 and held[0]["at"] == at(MON, 9, 45).isoformat()
    assert bias.read(held)["bull"] == 2 and bias.read(held)["label"] == "Neutral"      # too few to call
    later = bias.signals("INFY", candles, MON, S, cfg, at(MON, 10, 15) + timedelta(seconds=30))
    assert {g["status"] for g in later} == {"stopped"} and later[0]["until"] == at(MON, 10, 15).isoformat()
    assert [bias.leaning(g) for g in later] == ["bear", "bear"]                          # failed buys lean bearish
    # Only sweeps that hold are asked for here, so a plain sweep doesn't count twice.
    assert bias.signals("INFY", candles, MON, S, {**cfg, "triggers": ["reject"]}, at(MON, 10, 1))[0]["trigger"] == "reject"


def test_reading_weighs_held_against_stopped():
    def sig(signal, status):
        return {"symbol": "X", "signal": signal, "status": status}
    r = bias.read([sig("buy", "held")] * 4 + [sig("sell", "stopped")] * 2 + [sig("sell", "held")])
    assert (r["label"], r["bull"], r["bear"]) == ("Bullish", 6, 1)
    assert "4 potential buys from fractal lows: 4 holding, 0 stopped out." in r["reasons"]
    # Reaching the target first makes it a win, whatever happens after.
    won = monday()[:2] + [Candle(at(MON, 9, 45), 103, 105, 101, 103)] + monday()[3:]
    assert {g["status"] for g in bias.signals("INFY", friday() + won, MON, S, bias.DEFAULTS, at(MON, 10, 31))} == {"target"}
    assert bias.read([sig("buy", "stopped")] * 3 + [sig("sell", "target")] * 2)["label"] == "Bearish"


class FakeKite:
    def __init__(self):
        self.calls = 0

    def profile(self):
        return {}

    def candles_range(self, token, tf, start, end):
        assert tf == "15m"
        self.calls += 1
        return friday() + monday()


@pytest.fixture
def kite(monkeypatch):
    insts = {s: Instrument(s, s, k + 1, False) for k, s in enumerate(bias.NIFTY50)}
    monkeypatch.setattr(bias, "instruments", lambda: insts)
    monkeypatch.setattr(bias, "stocks", lambda today: bias.NIFTY50[:20] + ["NOTLISTED"])
    monkeypatch.setattr(bias, "market_settings", lambda: S)
    monkeypatch.setattr(config, "BIAS_CAPTURE", True)
    for coll in ("bias_snapshots", "bias_days"):
        for d in store.list(coll):
            store.delete(coll, d.get("id") or d["date"])
    store.delete("settings", "bias")
    bias._done.clear()
    fake = FakeKite()
    monkeypatch.setattr(brokers, "client_for", lambda u, doc=None: fake)
    store.put("users", "meera", {"username": "meera", "name": "Meera", "role": "user", "active": True, "modules": ["bias"],
                                 "password_hash": hash_password("meera-pass-1")})
    store.put("brokers", "meera", {"mode": "enctoken", "status": "connected", "enctoken": "x"})
    yield fake
    store.delete("users", "meera")
    store.delete("brokers", "meera")
    store.delete("bias_alerts", "meera")


def test_counts_every_15_minutes_and_telegrams_changes(kite, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda user, ch, subject, body: sent.append((user, subject, body)) or {"telegram": "sent"})
    bias.set_telegram("meera", True)
    assert bias.capture_if_due(at(MON, 9, 25)) is None                                  # first candle not closed yet
    snap = bias.capture_if_due(at(MON, 10, 0) + timedelta(seconds=30))
    assert snap["id"] == "2026-10-05T10:00" and snap["missing"] == ["NOTLISTED"] and kite.calls == 20
    assert (snap["label"], snap["bull"], snap["bear"]) == ("Bullish", 40, 0)
    assert sent[-1][1] == "🧭 Fractal bias at 10:00 AM: Bullish" and "40 potential buys" in sent[-1][2]
    assert bias.capture_if_due(at(MON, 10, 5)) is None                                   # once per slot
    snap = bias.capture_if_due(at(MON, 10, 15) + timedelta(seconds=30))
    assert (snap["label"], snap["bear"]) == ("Bearish", 40)                               # the buys were stopped out
    assert sent[-1][1] == "🧭 Fractal bias turned Bearish (was Bullish) · 10:15 AM"
    assert [s["id"] for s in bias.day_snapshots("2026-10-05")] == ["2026-10-05T10:00", "2026-10-05T10:15"]
    assert [p["bear"] for p in bias.series(bias.day_snapshots("2026-10-05"))] == [0, 40]


def test_the_page(kite, monkeypatch):
    import app.routes.bias as routes
    monkeypatch.setattr(config, "SUPERADMIN_USERNAME", "boss")
    monkeypatch.setattr(config, "SUPERADMIN_PASSWORD", "boss-pass-123")
    monkeypatch.setattr(routes, "market_settings", lambda: S)
    from app.main import app
    with TestClient(app) as c:
        c.post("/login", data={"username": "meera", "password": "meera-pass-1"})
        assert "No counts yet" in c.get("/bias").text
        monkeypatch.setattr(routes, "now_ist", lambda: at(MON, 8, 50))
        assert "market is closed" in c.post("/bias/capture").headers["HX-Trigger"]
        bias.capture(kite, at(MON, 10, 0) + timedelta(seconds=30), "2026-10-05T10:00")
        monkeypatch.setattr(routes, "now_ist", lambda: at(MON, 10, 16))
        r = c.post("/bias/capture")
        assert r.headers["HX-Redirect"] == "/bias?day=2026-10-05&at=2026-10-05T10:16" and "bearish" in r.headers["HX-Trigger"]
        page = c.get("/bias?day=2026-10-05&at=2026-10-05T10:16").text
        assert "Bearish" in page and "Stopped out" in page and "changed" in page and 'id="bias-data"' in page
        assert "Not counted: NOTLISTED" in page and "Count these signals" not in page
        assert 'data-layout-for="bias"' in page and page.count('class="bias-cards') == 1 and page.count("<article") == 40   # cards as well as the list
        assert "Only the super admin" in c.post("/bias/settings", data={"triggers": ["fail"], "min_candles": "3"}).headers["HX-Trigger"]
        assert "Notifications page" in c.post("/bias/telegram", data={"on": "1"}).headers["HX-Trigger"]
        c.post("/logout")
        c.post("/login", data={"username": "boss", "password": "boss-pass-123"})
        assert "Count these signals" in c.get("/bias").text
        c.post("/bias/settings", data={"triggers": ["fail", "bogus"], "min_candles": "3"})
        assert bias.load_settings() == {"triggers": ["fail"], "min_candles": 3}
        assert "at least one" in c.post("/bias/settings", data={"min_candles": "3"}).headers["HX-Trigger"]
        c.post("/logout")
        store.put("users", "nisha", {"username": "nisha", "role": "user", "active": True, "modules": ["oi"],
                                     "password_hash": hash_password("nisha-pass-1")})
        c.post("/login", data={"username": "nisha", "password": "nisha-pass-1"})
        assert c.get("/bias").status_code == 403
        store.delete("users", "nisha")


def test_each_reading_carries_the_other(kite, monkeypatch):
    """Fractal bias messages and page carry the Nifty OI reading at that moment, and OI messages the fractal bias."""
    from app import oi
    oi_snap = {"id": "2026-10-05T09:45", "date": "2026-10-05", "at": at(MON, 9, 46).isoformat(), "expiry": "2026-10-06",
               "spot": 24510.0, "rows": [{"strike": 24500.0, "ce_oi": 100_000, "pe_oi": 150_000, "ce_ltp": 90.0, "pe_ltp": 80.0}]}
    store.put("oi_snapshots", oi_snap["id"], oi_snap)
    monkeypatch.setattr(oi, "market_settings", lambda: S)
    store.update("users", "meera", {"modules": ["bias", "oi"]})
    bias.set_telegram("meera", True)
    oi.set_telegram("meera", True)
    sent = []
    monkeypatch.setattr(notify, "send", lambda user, ch, subject, body: sent.append((subject, body)) or {"telegram": "sent"})
    try:
        bias.capture_if_due(at(MON, 10, 0) + timedelta(seconds=30))
        lines = sent[-1][1].split("\n")
        assert lines[-2] == "Nifty OI: Mildly bullish · PCR 1.50 · support 24,500, resistance 24,500 (9:45 AM)"
        assert lines[-1].endswith("/bias?day=2026-10-05&at=2026-10-05T10:00")
        later = {**oi_snap, "id": "2026-10-05T10:15", "at": at(MON, 10, 16).isoformat(),
                 "rows": [{**oi_snap["rows"][0], "ce_oi": 400_000}]}
        store.put("oi_snapshots", later["id"], later)
        oi.announce(later)
        assert sent[-1][0] == "📊 Nifty OI at 10:15 AM: Bearish"
        assert sent[-1][1].split("\n")[-2] == "Fractal bias: Bullish · 40 leaning bullish, 0 bearish (10:00 AM)"
        import app.routes.bias as routes
        monkeypatch.setattr(config, "SUPERADMIN_USERNAME", "boss")
        monkeypatch.setattr(config, "SUPERADMIN_PASSWORD", "boss-pass-123")
        from app.main import app
        with TestClient(app) as c:
            c.post("/login", data={"username": "meera", "password": "meera-pass-1"})
            page = c.get("/bias?day=2026-10-05&at=2026-10-05T10:00").text
            assert "Nifty OI at 9:45 AM" in page and "/oi?day=2026-10-05&at=2026-10-05T09:45" in page
            oi_page = c.get("/oi?day=2026-10-05&at=2026-10-05T10:15").text            # and the other way round
            assert "Fractal bias at 10:00 AM" in oi_page and "/bias?day=2026-10-05&at=2026-10-05T10:00" in oi_page
            store.update("users", "meera", {"modules": ["bias"]})
            assert "Nifty OI at" not in c.get("/bias").text                         # only for those who can see OI
    finally:
        for sid in ("2026-10-05T09:45", "2026-10-05T10:15"):
            store.delete("oi_snapshots", sid)
        store.delete("oi_days", "2026-10-05")
        store.delete("oi_alerts", "meera")


def test_a_signals_chart(kite, monkeypatch):
    """Each signal's stock opens on a chart with the day's signals for it marked, at any candle size."""
    from app import prices
    bias.capture(kite, at(MON, 10, 15) + timedelta(seconds=30), "2026-10-05T10:15")
    asked = []

    def fake_chart(username, inst, range_key, interval=""):
        asked.append((inst.symbol, range_key, interval))
        step = 900 if interval == "15m" else 300
        first = int(at(MON, 9, 15).timestamp()) + prices.IST_OFFSET
        return {"symbol": inst.symbol, "range": range_key, "interval": interval, "daily": False, "trimmed": False,
                "candles": [{"time": first + step * k, "open": 1, "high": 1, "low": 1, "close": 1} for k in range(6)]}
    monkeypatch.setattr(prices, "chart", fake_chart)
    monkeypatch.setattr(config, "SUPERADMIN_USERNAME", "boss")
    monkeypatch.setattr(config, "SUPERADMIN_PASSWORD", "boss-pass-123")
    from app.main import app
    with TestClient(app) as c:
        c.post("/login", data={"username": "meera", "password": "meera-pass-1"})
        page = c.get("/bias?day=2026-10-05&at=2026-10-05T10:15").text
        assert 'data-source="/bias/chart?snap=2026-10-05T10:15"' in page and 'data-chart="ADANIENT" data-interval="15m"' in page
        data = c.get("/bias/chart?snap=2026-10-05T10:15&symbol=ADANIENT&range=5D&interval=15m").json()
        assert asked[-1] == ("ADANIENT", "5D", "15m") and len(data["hits"]) == 2
        h = data["hits"][0]
        assert h["time"] == data["candles"][1]["time"]                              # the 9:30 candle that held the sweep
        assert h["signal"] == "buy" and "Sweep holds of the 30 min fractal low" in h["summary"] and "stopped out 10:15 AM" in h["summary"]
        assert c.get("/bias/chart?snap=x&symbol=NOPE").status_code == 404
        bias.capture(kite, at(MON, 10, 0) + timedelta(seconds=30), "2026-10-05T10:00")         # still holding then
        held = c.get("/bias/chart?snap=2026-10-05T10:00&symbol=ADANIENT&range=5D&interval=15m").json()["hits"]
        assert [h["summary"].rsplit(", ", 1)[1] for h in held] == ["holding", "holding"]
