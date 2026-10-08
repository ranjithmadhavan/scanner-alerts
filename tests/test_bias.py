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
CFG15 = {**bias.DEFAULTS, "trigger_tf": "15m", "flip_after": 1}  # 15-minute candles; a stop-out shows at once
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
        Candle(at(MON, 10, 0), 103, 103, 93, 93.5),
        Candle(at(MON, 10, 15), 95, 96, 92, 93),
    ]


def test_15_minute_candles_pair_into_half_hours():
    halves = bias.to_30m(friday()[:5], S)
    assert [(c.start.strftime("%H:%M"), c.low) for c in halves] == [("09:15", 100), ("09:45", 100), ("10:15", 100)]
    assert bias.to_30m([Candle(at(FRI, 9, 15), 1, 5, 1, 2), Candle(at(FRI, 9, 30), 2, 7, 0.5, 6)], S)[0] == \
        Candle(at(FRI, 9, 15), 1, 7, 0.5, 6)
    fives = [Candle(at(FRI, 9, 15) + timedelta(minutes=5 * k), 10 + k, 11 + k, 9 + k, 10.5 + k) for k in range(7)]
    assert bias.to_30m(fives, S) == [Candle(at(FRI, 9, 15), 10, 16, 9, 15.5), Candle(at(FRI, 9, 45), 16, 17, 15, 16.5)]


def test_counts_every_5_minutes_and_the_bias_is_looked_at_every_15():
    assert bias.slot(at(MON, 9, 20), S) is None and bias.slot(at(MON, 9, 21), S) == "2026-10-05T09:20"
    assert bias.slot(at(MON, 11, 3), S) == "2026-10-05T11:00" and bias.slot(at(MON, 15, 31), S) == "2026-10-05T15:30"
    assert [bias.bias_mark(f"2026-10-05T{t}", S) for t in ("09:20", "09:30", "09:35", "09:45", "15:25", "15:30")] == \
        [False, True, False, True, False, True]


def test_a_buy_holds_then_is_stopped_out():
    cfg = CFG15
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
    assert {g["status"] for g in bias.signals("INFY", friday() + won, MON, S, CFG15, at(MON, 10, 31))} == {"target"}
    assert bias.read([sig("buy", "stopped")] * 3 + [sig("sell", "target")] * 2)["label"] == "Bearish"


class FakeKite:
    def __init__(self):
        self.calls = 0

    def profile(self):
        return {}

    def candles_range(self, token, tf, start, end):
        assert tf == "15m"  # set in the fixture, to match these candles
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
    store.put("settings", "bias", {"trigger_tf": "15m", "flip_after": 1})
    bias._done.clear()
    bias._past.clear()
    fake = FakeKite()
    monkeypatch.setattr(brokers, "client_for", lambda u, doc=None: fake)
    store.put("users", "meera", {"username": "meera", "name": "Meera", "role": "user", "active": True, "modules": ["bias"],
                                 "password_hash": hash_password("meera-pass-1")})
    store.put("brokers", "meera", {"mode": "enctoken", "status": "connected", "enctoken": "x"})
    yield fake
    store.delete("users", "meera")
    store.delete("brokers", "meera")
    store.delete("bias_alerts", "meera")


def test_new_signals_every_check_and_bias_changes_at_the_quarter_hours(kite, monkeypatch):
    sent, hooks = [], []
    monkeypatch.setattr(notify, "send", lambda user, ch, subject, body: sent.append((user, subject, body)) or {"telegram": "sent"})
    monkeypatch.setattr(bias.webhooks, "send", lambda urls, body: hooks.append((urls, body)) or "sent")
    bias.save_prefs("meera", {"changes": True, "signals": True, "telegram": True, "webhooks": ["https://hook.example/x"],
                              "webhook_payload": '{"desk":"A"}'})
    assert bias.capture_if_due(at(MON, 9, 15)) is None                                  # no candle closed yet
    snap = bias.capture_if_due(at(MON, 10, 0) + timedelta(seconds=30))
    assert snap["id"] == "2026-10-05T10:00" and snap["missing"] == ["NOTLISTED"]
    assert kite.calls == 40                                                              # earlier days once, then today
    assert (snap["label"], snap["bull"], snap["bear"]) == ("Bullish", 40, 0)
    assert [x[1] for x in sent] == ["🧭 40 new fractal signals · 10:00 AM", "🧭 Fractal bias at 10:00 AM: Bullish"]
    assert sent[0][2].startswith("ADANIENT potential buy: sweep holds of the 30 min fractal low") and "stop 94.00, target 105.00" in sent[0][2]
    assert [b["event"] for _, b in hooks] == ["fractal_bias_signals", "fractal_bias_change"]
    assert len(hooks[0][1]["signals"]) == 40 and hooks[0][1]["payload"] == {"desk": "A"} and hooks[1][1]["label"] == "Bullish"
    assert bias.capture_if_due(at(MON, 10, 2)) is None                                   # once per 5-minute slot
    snap = bias.capture_if_due(at(MON, 10, 5) + timedelta(seconds=30))
    assert snap["id"] == "2026-10-05T10:05" and kite.calls == 60 and len(sent) == 2      # today only; nothing new to say
    snap = bias.capture_if_due(at(MON, 10, 10) + timedelta(seconds=30))
    snap = bias.capture_if_due(at(MON, 10, 15) + timedelta(seconds=30))
    assert (snap["label"], snap["bear"]) == ("Bearish", 40)                               # the buys were stopped out
    assert sent[-1][1] == "🧭 Fractal bias turned Bearish (was Bullish) · 10:15 AM" and hooks[-1][1]["was"] == "Bullish"
    day = store.get("bias_days", "2026-10-05")
    assert day["slots"] == ["2026-10-05T10:00", "2026-10-05T10:05", "2026-10-05T10:10", "2026-10-05T10:15"]
    assert [p["bear"] for p in bias.series(day)] == [0, 0, 0, 40]
    # Bias changes only, on Telegram only: no signal messages, no webhooks.
    bias.save_prefs("meera", {"changes": True, "signals": False, "telegram": True, "webhooks": [], "webhook_payload": ""})
    before = len(hooks), len(sent)
    bias._done.clear()
    store.delete("bias_snapshots", "2026-10-05T10:15")
    store.update("bias_days", "2026-10-05", {"slots": day["slots"][:3], "announced": "Bullish"})
    bias.capture_if_due(at(MON, 10, 15) + timedelta(seconds=30))
    assert len(hooks) == before[0] and [x[1] for x in sent[before[1]:]] == ["🧭 Fractal bias turned Bearish (was Bullish) · 10:15 AM"]


def test_older_telegram_switch_still_means_bias_changes():
    store.put("bias_alerts", "old", {"telegram": True})
    assert bias.prefs("old")["changes"] and bias.prefs("old")["telegram"] and not bias.prefs("old")["signals"]
    store.delete("bias_alerts", "old")


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
        # Notifications: Telegram needs setting up first; webhooks are checked and can be tested.
        assert "Notifications page" in c.post("/bias/notify", data={"changes": "1", "telegram": "1"}).headers["HX-Trigger"]
        assert "can't be reached" in c.post("/bias/notify", data={"signals": "1", "webhooks": "http://localhost/x"}).headers["HX-Trigger"]
        monkeypatch.setattr(bias.webhooks, "_is_public", lambda host: True)
        posted = []

        class Resp:
            status_code = 200
        monkeypatch.setattr(bias.webhooks.httpx, "post", lambda url, json, **kw: posted.append((url, json)) or Resp())
        r = c.post("/bias/notify", data={"signals": "1", "webhooks": "https://hook.example/x", "webhook_payload": '{"a": 1}'})
        assert "new signals to 1 webhook" in r.headers["HX-Trigger"] and 'name="signals" value="1" class="mt-1 h-4 w-4 rounded accent-peacock" checked' in r.text
        assert bias.prefs("meera") == {"changes": False, "signals": True, "telegram": False, "webhooks": ["https://hook.example/x"],
                                       "webhook_payload": '{"a":1}', "extremes_only": False}
        assert "Test request sent" in c.post("/bias/notify/test", data={"webhooks": "https://hook.example/x"}).headers["HX-Trigger"]
        assert posted[-1][1]["event"] == "fractal_bias_signals" and posted[-1][1]["test"] is True
        c.post("/logout")
        c.post("/login", data={"username": "boss", "password": "boss-pass-123"})
        assert "Count these signals" in c.get("/bias").text
        c.post("/bias/settings", data={"triggers": ["fail", "bogus"], "min_candles": "3", "trigger_tf": "15m"})
        assert bias.load_settings() == {"triggers": ["fail"], "min_candles": 3, "trigger_tf": "15m", "flip_after": 3}
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
    store.put("oi_days", "2026-10-05", {"date": "2026-10-05", "slots": [oi_snap["id"]]})
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
        store.update("oi_days", "2026-10-05", {"slots": [oi_snap["id"], later["id"]]})
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
        assert 'data-source="/bias/chart?snap=2026-10-05T10:15"' in page and 'data-chart="ADANIENT" data-interval="5m"' in page
        data = c.get("/bias/chart?snap=2026-10-05T10:15&symbol=ADANIENT&range=5D&interval=15m").json()
        assert asked[-1] == ("ADANIENT", "5D", "15m") and len(data["hits"]) == 2
        h = data["hits"][0]
        assert h["time"] == data["candles"][1]["time"]                              # the 9:30 candle that held the sweep
        assert h["signal"] == "buy" and "Sweep holds of the 30 min fractal low" in h["summary"] and "stopped out 10:15 AM" in h["summary"]
        assert c.get("/bias/chart?snap=x&symbol=NOPE").status_code == 404
        bias.capture(kite, at(MON, 10, 0) + timedelta(seconds=30), "2026-10-05T10:00")         # still holding then
        held = c.get("/bias/chart?snap=2026-10-05T10:00&symbol=ADANIENT&range=5D&interval=15m").json()["hits"]
        assert [h["summary"].rsplit(", ", 1)[1] for h in held] == ["holding", "holding"]


def test_gap_flips_count_but_are_not_sent_as_signals(kite, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda user, ch, subject, body: sent.append(subject) or {"telegram": "sent"})
    bias.save_prefs("meera", {"changes": False, "signals": True, "telegram": True, "webhooks": [], "webhook_payload": ""})

    def sig(symbol, flipped):
        return {"key": f"{symbol}:k", "symbol": symbol, "signal": "sell", "trigger": "confirm", "side": "low", "flipped": flipped,
                "level": 100.0, "at": at(MON, 10, 0).isoformat(), "price": 99.0, "stop": 101.0, "target": 95.0, "status": "held"}
    snap = {"id": "2026-10-05T10:05", "date": "2026-10-05", "at": at(MON, 10, 5).isoformat(),
            "signals": [sig("INFY", True), sig("TCS", False)], "label": "Neutral"}
    store.put("bias_snapshots", snap["id"], snap)
    store.put("bias_days", "2026-10-05", {"date": "2026-10-05", "slots": [snap["id"]]})
    assert [g["symbol"] for g in bias.new_signals(snap, None)] == ["TCS"]
    bias.notify_all(snap, S)
    assert sent == ["🧭 TCS potential sell · 10:05 AM"]
    assert bias.read(snap["signals"])["bear"] == 2                                       # the flip still counts


def test_only_at_the_days_high_or_low(kite, monkeypatch):
    """Signals on fractals that are a day's high or low are marked; someone can choose to see and be sent only those."""
    from app import fractals
    candles = friday() + monday()
    held = bias.signals("INFY", candles, MON, S, CFG15, at(MON, 10, 0) + timedelta(seconds=30))
    assert {g["level"]: g["extreme"] for g in held} == {95: True, 100: False}             # 95 was Friday's low, 100 wasn't
    # Directly: Friday's low and high count, a low above Friday's low doesn't; today's fractals count against today so far.
    sweep = len(friday())  # Monday 9:15, the candle that takes them
    for f, expected in ((fractals.Fractal("low", 95.0, at(FRI, 10, 45)), True), (fractals.Fractal("low", 100.0, at(FRI, 9, 15)), False),
                        (fractals.Fractal("high", 105.0, at(FRI, 9, 15)), True)):
        assert fractals.at_day_extreme(fractals.Hit(f, "reject", candles[sweep], sweep, 101), candles) is expected
    today_low = fractals.Fractal("low", 94.0, at(MON, 9, 15))                             # but 10:00 traded to 93 before 10:15 took it
    assert not fractals.at_day_extreme(fractals.Hit(today_low, "reject", candles[-1], len(candles) - 1, 93), candles)

    sigs = [{"key": "A", "extreme": True}, {"key": "B", "extreme": False}, {"key": "C"}]
    assert [g["key"] for g in bias.shown(sigs, False)] == ["A", "B", "C"] and [g["key"] for g in bias.shown(sigs, True)] == ["A"]

    # The page: the choice is per person, narrows the table and cards, and is kept when notifications are saved.
    import app.routes.bias as routes
    monkeypatch.setattr(config, "SUPERADMIN_USERNAME", "boss")
    monkeypatch.setattr(config, "SUPERADMIN_PASSWORD", "boss-pass-123")
    snap = bias.capture(kite, at(MON, 10, 0) + timedelta(seconds=30), "2026-10-05T10:00")
    n = sum(1 for g in snap["signals"] if g["extreme"])
    assert n == 20                                                                         # one of each stock's two
    from app.main import app
    with TestClient(app) as c:
        c.post("/login", data={"username": "meera", "password": "meera-pass-1"})
        page = c.get("/bias?day=2026-10-05&at=2026-10-05T10:00").text
        assert "Signals <span class=\"num text-base font-semibold text-ink-soft\">40</span>" in page and "day low" in page
        assert "high or low" in c.post("/bias/extremes", data={"on": "1"}).headers["HX-Trigger"]
        page = c.get("/bias?day=2026-10-05&at=2026-10-05T10:00").text
        assert f">{n} of 40</span>" in page and page.count("<article") == n
        c.post("/bias/notify", data={"signals": "1"})
        assert bias.prefs("meera")["extremes_only"] is True
        c.post("/bias/extremes", data={})
        assert bias.prefs("meera")["extremes_only"] is False

    # Messages follow the same choice.
    sent = []
    monkeypatch.setattr(notify, "send", lambda user, ch, subject, body: sent.append(subject) or {"telegram": "sent"})
    bias.save_prefs("meera", {**bias.prefs("meera"), "signals": True, "telegram": True, "extremes_only": True})
    fresh = {**snap, "id": "2026-10-05T10:05", "signals": [{**snap["signals"][0], "symbol": "AAA", "key": "AAA", "extreme": False},
                                                             {**snap["signals"][1], "symbol": "BBB", "key": "BBB", "extreme": True}]}
    store.put("bias_snapshots", fresh["id"], fresh)
    store.update("bias_days", "2026-10-05", {"slots": [snap["id"], fresh["id"]]})
    bias.notify_all(fresh, S)
    assert sent == ["🧭 BBB potential buy · 10:05 AM"]                                    # AAA's fractal wasn't a day's low


def test_a_stop_out_turns_the_bias_only_if_price_stays_beyond_the_fractal():
    """The signal goes out at once; breaking its stop only counts against it if, N candles on, price still closes
    beyond the fractal. TECHM, 7 Oct: a sell on the 1,506.70 fractal high, stop 1,508."""
    from app import fractals
    t0 = at(MON, 9, 15)

    def c(k, o, h, l, cl):
        return Candle(t0 + timedelta(minutes=5 * k), o, h, l, cl)
    sell = fractals.Hit(fractals.Fractal("high", 1506.7, t0 - timedelta(days=1)), "confirm", c(1, 1503.4, 1503.7, 1493.6, 1494.7), 1, 1494.7)
    made = [c(0, 1504.0, 1508.0, 1499.7, 1503.4), c(1, 1503.4, 1503.7, 1493.6, 1494.7)]
    pop = [c(2, 1495, 1509, 1495, 1507.5), c(3, 1507.5, 1508, 1503, 1504), c(4, 1504, 1505, 1500, 1501)]   # through the stop, then back below
    run = [c(2, 1495, 1509, 1495, 1507.5), c(3, 1507.5, 1511, 1506, 1510), c(4, 1510, 1513, 1509, 1512)]   # through and staying above
    assert bias.follow(sell, made + pop, 3)["status"] == "held"                                          # back below 1,506.70: still a sell
    assert bias.follow(sell, made + pop, 1)["status"] == "stopped"                                       # one candle: the break itself decides
    out = bias.follow(sell, made + run, 3)
    assert (out["status"], out["until"].start) == ("stopped", t0 + timedelta(minutes=20))                # settled on the third candle
    watching = bias.follow(sell, made + run[:2], 3)
    assert (watching["status"], watching["watch"]) == ("held", [2, 3])                                   # two of three seen
    assert bias.read([{"symbol": "TECHM", "signal": "sell", "status": watching["status"]}])["bear"] == 1  # still leans bearish


def test_no_signal_from_a_sweep_on_the_previous_days_last_candle():
    """Friday's last candle sweeps a fractal high and closes back; Monday gaps down. No sell on Monday."""
    quiet = [Candle(at(FRI, 9, 15) + timedelta(minutes=15 * k), 100, 105 if k != 20 else 108, 99, 102) for k in range(24)]
    quiet.append(Candle(at(FRI, 15, 15), 102, 109, 101, 104))                                   # takes the 108 high, closes back
    gap = [Candle(at(MON, 9, 15), 96, 97, 94, 95), Candle(at(MON, 9, 30), 95, 96, 94, 95)]
    sigs = bias.signals("NESTLEIND", quiet + gap, MON, S, {**CFG15, "triggers": ["confirm", "fail", "reject"], "min_candles": 0}, at(MON, 9, 46))
    assert [g for g in sigs if g["level"] == 108] == []


def test_recount_rebuilds_a_day_with_the_current_rules(kite, monkeypatch):
    """Counts saved under older rules can be rebuilt; nothing is sent while doing it."""
    monkeypatch.setattr(notify, "send", lambda *a: pytest.fail("a recount sends nothing"))
    bias.capture(kite, at(MON, 10, 0) + timedelta(seconds=30), "2026-10-05T10:00")
    old = store.get("bias_snapshots", "2026-10-05T10:00")
    store.put("bias_snapshots", old["id"], {**old, "signals": old["signals"] + [{**old["signals"][0], "key": "STALE", "symbol": "STALE"}]})
    calls = kite.calls
    assert bias.recount_day(kite, MON) == 1
    assert kite.calls - calls == 20                                                        # each stock's candles once
    again = store.get("bias_snapshots", "2026-10-05T10:00")
    assert "STALE" not in {g["symbol"] for g in again["signals"]} and len(again["signals"]) == 40
    assert store.get("bias_days", "2026-10-05")["points"]["2026-10-05T10:00"]["bull"] == 40
    # With no saved counts, every 5-minute close of the day is built.
    store.delete("bias_days", "2026-10-05")
    assert bias.recount_day(kite, MON) == 75
    assert store.get("bias_days", "2026-10-05")["slots"][0] == "2026-10-05T09:20"


def test_only_the_super_admin_can_recount(kite, monkeypatch):
    import app.routes.bias as routes
    monkeypatch.setattr(config, "SUPERADMIN_USERNAME", "boss")
    monkeypatch.setattr(config, "SUPERADMIN_PASSWORD", "boss-pass-123")
    from app.main import app
    with TestClient(app) as c:
        c.post("/login", data={"username": "meera", "password": "meera-pass-1"})
        assert "Only the super admin" in c.post("/bias/recount", data={"day": "2026-10-05"}).headers["HX-Trigger"]
        c.post("/logout")
        c.post("/login", data={"username": "boss", "password": "boss-pass-123"})
        monkeypatch.setattr(routes, "now_ist", lambda: at(MON, 16, 0))
        r = c.post("/bias/recount", data={"day": "2026-10-05"})
        assert r.headers["HX-Redirect"] == "/bias?day=2026-10-05" and "75 counts rebuilt" in r.headers["HX-Trigger"]
        assert "Recount this day" in c.get("/bias").text
