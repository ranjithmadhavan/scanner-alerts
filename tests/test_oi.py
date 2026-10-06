"""Nifty OI: snapshots on schedule, the reading, and the page."""

import json
import struct
from datetime import datetime, time

import pytest
from fastapi.testclient import TestClient

from app import brokers, config, oi
from app import kite as kite_mod
from app.kite import Instrument
from app.market import IST, MarketSettings
from app.security import hash_password
from app.store import store

S = MarketSettings(time(9, 15), time(15, 30), 60)
MON = datetime(2026, 10, 5, tzinfo=IST)
STRIKES = [24300, 24350, 24400, 24450, 24500, 24550, 24600, 24650, 24700]


def chain():
    out = {}
    for expiry, code in (("2026-10-06", "26O06"), ("2026-10-13", "26O13")):
        for k in STRIKES:
            for kind in ("CE", "PE"):
                sym = f"NIFTY{code}{k}{kind}"
                out[f"NFO:{sym}"] = Instrument(sym, f"NIFTY {k} {kind}", hash(sym) % 10 ** 6, False, "NFO", kind, expiry, "NIFTY", float(k))
    out["NIFTY 50"] = Instrument("NIFTY 50", "NIFTY 50", 256265, True)
    return out


class FakeKite:
    """Quotes from a table the test edits: {symbol: oi}; unknown options get 1 lakh."""
    def __init__(self, spot=24512.0):
        self.spot, self.oi, self.asked = spot, {}, []

    def profile(self):
        return {}

    def quote(self, keys):
        self.asked.append(list(keys))
        out = {}
        for k in keys:
            if k == oi.SPOT_KEY:
                out[k] = {"last_price": self.spot}
            else:
                out[k] = {"last_price": 100.0, "oi": self.oi.get(k.removeprefix("NFO:"), 100_000)}
        return out


@pytest.fixture
def kite(monkeypatch):
    monkeypatch.setattr(oi, "instruments", chain)
    store.delete("settings", "oi")
    for s in store.list("oi_snapshots"):
        store.delete("oi_snapshots", s["id"])
    for d in store.list("oi_days"):
        store.delete("oi_days", d["date"])
    oi._done.clear()
    monkeypatch.setattr(config, "OI_CAPTURE", True)
    fake = FakeKite()
    monkeypatch.setattr(brokers, "client_for", lambda u, doc=None: fake)
    store.put("users", "omkar", {"username": "omkar", "name": "Omkar", "role": "user", "active": True, "modules": ["oi"],
                                 "password_hash": hash_password("omkar-pass-1")})
    store.put("brokers", "omkar", {"mode": "enctoken", "status": "connected", "enctoken": "x"})
    yield fake
    store.delete("users", "omkar")
    store.delete("brokers", "omkar")


def test_capture_takes_the_nearest_expiry_around_the_money(kite):
    store.update("settings", "oi", {"strikes": 2})
    snap = oi.capture(kite, MON.replace(hour=9, minute=16))
    assert snap["expiry"] == "2026-10-06" and snap["spot"] == 24512.0
    assert [r["strike"] for r in snap["rows"]] == [24400, 24450, 24500, 24550, 24600]     # ATM 24500, 2 either side
    assert len(kite.asked) == 2 and len(kite.asked[1]) == 11                                   # 10 options + the index, one request
    assert store.get("oi_snapshots", snap["id"])["rows"][0] == {"strike": 24400, "ce_oi": 100000, "pe_oi": 100000, "ce_ltp": 100.0, "pe_ltp": 100.0}
    assert store.get("oi_days", "2026-10-05")["count"] == 1


def test_snapshots_follow_the_schedule_once_per_slot(kite, monkeypatch):
    taken = []
    for h, m in ((9, 10), (9, 16), (9, 20), (9, 31), (9, 44), (15, 31), (15, 40)):
        if oi.capture_if_due(MON.replace(hour=h, minute=m), S):
            taken.append(f"{h}:{m:02d}")
    assert taken == ["9:16", "9:31", "15:31"]           # not before the open, once per 15 minutes, the close, then stop
    assert [s["id"] for s in oi.day_snapshots("2026-10-05")] == ["2026-10-05T09:15", "2026-10-05T09:30", "2026-10-05T15:30"]
    store.update("settings", "oi", {"interval": 30})
    assert oi.slot(MON.replace(hour=10, minute=50), S, 30) == "2026-10-05T10:45"
    # With no connected account allowed to see OI, nothing is taken.
    monkeypatch.setattr(brokers, "load", lambda username: {"status": "expired"})
    assert oi.capture_if_due(MON.replace(hour=11, minute=20), S) is None


def test_reading_puts_written_faster_than_calls_is_bullish(kite):
    store.update("settings", "oi", {"strikes": 2})
    first = oi.capture(kite, MON.replace(hour=9, minute=16), "2026-10-05T09:15")
    opening = oi.analyse(first, first)
    assert opening["score"] is None and "PCR alone" in opening["reasons"][0] and opening["label"] == "Neutral"
    kite.oi.update({"NIFTY26O0624400PE": 900_000, "NIFTY26O0624450PE": 400_000, "NIFTY26O0624550CE": 250_000})
    kite.spot = 24520.0
    later = oi.capture(kite, MON.replace(hour=10, minute=1), "2026-10-05T10:00")
    r = oi.analyse(later, first)
    assert (r["pe_chg"], r["ce_chg"]) == (1_100_000, 150_000) and r["label"] == "Bullish" and r["tone"] == "up"
    assert r["support"] == 24400 and r["resistance"] == 24550 and r["spot_move"] == 8.0 and r["atm"] == 24500
    assert "Put OI has changed by 11.0L since the open against 1.5L for calls" in r["reasons"][0]
    assert r["pcr"] == pytest.approx(1_600_000 / 650_000)
    # Calls written much faster: bearish.
    kite.oi.update({"NIFTY26O0624600CE": 3_000_000})
    r = oi.analyse(oi.capture(kite, MON.replace(hour=10, minute=16), "2026-10-05T10:15"), first)
    assert r["label"] == "Bearish" and r["resistance"] == 24600
    points = oi.series(oi.day_snapshots("2026-10-05"))
    assert [p["pe"] for p in points] == [0.0, 11.0, 11.0] and [p["label"] for p in points][1:] == ["Bullish", "Bearish"]


def test_the_page(kite, monkeypatch):
    monkeypatch.setattr(config, "SUPERADMIN_USERNAME", "boss")
    monkeypatch.setattr(config, "SUPERADMIN_PASSWORD", "boss-pass-123")
    from app.main import app
    import app.routes.oi as oi_routes
    monkeypatch.setattr(oi_routes, "now_ist", lambda: MON.replace(hour=8, minute=40))
    with TestClient(app) as c:
        c.post("/login", data={"username": "omkar", "password": "omkar-pass-1"})
        assert "No snapshots yet" in c.get("/oi").text
        assert "market is closed" in c.post("/oi/capture").headers["HX-Trigger"]      # before the open OI is yesterday's
        monkeypatch.setattr(oi_routes, "now_ist", lambda: MON.replace(hour=10, minute=2))
        r = c.post("/oi/capture")
        assert r.headers["HX-Redirect"].startswith("/oi?day=") and "Snapshot taken" in r.headers["HX-Trigger"]
        page = c.get("/oi").text
        assert "Nifty OI" in page and "Too early in the day" in page and "ATM" in page and 'id="oi-data"' in page
        assert "Capture every" not in page                                   # settings are for the super admin
        assert "Only the super admin" in c.post("/oi/settings", data={"interval": "30", "strikes": "8"}).headers["HX-Trigger"]
        c.post("/logout")
        store.put("users", "nisha", {"username": "nisha", "name": "Nisha", "role": "user", "active": True, "modules": ["scanner"],
                                     "password_hash": hash_password("nisha-pass-1")})
        c.post("/login", data={"username": "nisha", "password": "nisha-pass-1"})
        assert c.get("/oi").status_code == 403                               # needs the Nifty OI permission
        c.post("/logout")
        store.delete("users", "nisha")
        c.post("/login", data={"username": "boss", "password": "boss-pass-123"})
        assert "Capture every" in c.get("/oi").text
        c.post("/oi/settings", data={"interval": "30", "strikes": "8"})
        assert oi.load_settings() == {"interval": 30, "strikes": 8}


def test_strikes_follow_price_and_new_ones_have_no_change(kite):
    store.update("settings", "oi", {"strikes": 2})
    first = oi.capture(kite, MON.replace(hour=9, minute=16), "2026-10-05T09:15")
    kite.spot = 24610.0                                  # price moved up two strikes
    r = oi.analyse(oi.capture(kite, MON.replace(hour=11, minute=1), "2026-10-05T11:00"), first)
    assert [row["strike"] for row in r["rows"]] == [24500, 24550, 24600, 24650, 24700] and r["atm"] == 24600
    assert [row["pe_chg"] for row in r["rows"]] == [0, 0, 0, None, None]   # 24650 and 24700 weren't in view at the open


def _frame(*packets: bytes) -> bytes:
    return struct.pack(">H", len(packets)) + b"".join(struct.pack(">H", len(p)) + p for p in packets)


def _full_option(token, paise, oi_):
    p = bytearray(184)
    struct.pack_into(">ii", p, 0, token, paise)
    struct.pack_into(">i", p, 48, oi_)
    return bytes(p)


def _index(token, paise):
    p = bytearray(32)
    struct.pack_into(">ii", p, 0, token, paise)
    return bytes(p)


def test_ticks_carry_price_and_oi():
    ticks = kite_mod.parse_ticks(_frame(_index(256265, 2255575), _full_option(10418434, 11190, 7877610)))
    assert ticks == {256265: {"last_price": 22555.75}, 10418434: {"last_price": 111.9, "oi": 7877610}}
    assert kite_mod.parse_ticks(b"\x00") == {}  # heartbeat


def test_web_session_quotes_come_off_the_live_feed(monkeypatch):
    """Kite answers /oms/quote with 400 for a web session, so enctoken mode reads the websocket."""
    insts = chain()
    opt = insts["NFO:NIFTY26O0624500CE"]
    sent = []

    class FakeWS:
        def __init__(self):
            self.frames = [b"\x00", _frame(_index(256265, 2451200)), _frame(_full_option(opt.token, 10050, 4_200_000))]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def send(self, m):
            sent.append(json.loads(m))

        def recv(self, timeout=None):
            if not self.frames:
                raise TimeoutError
            return self.frames.pop(0)

    urls = []
    monkeypatch.setattr(kite_mod, "instruments", lambda: insts)
    monkeypatch.setattr(kite_mod, "ws_connect", lambda url, **kw: urls.append(url) or FakeWS())
    monkeypatch.setattr(kite_mod.KiteClient, "_get", lambda *a: pytest.fail("no REST quote for a web session"))
    c = kite_mod.KiteClient("enctoken", enctoken="ab/c+d==", user_id="ZG1234")
    got = c.quote([oi.SPOT_KEY, "NFO:NIFTY26O0624500CE"])
    assert got == {oi.SPOT_KEY: {"last_price": 24512.0}, "NFO:NIFTY26O0624500CE": {"last_price": 100.5, "oi": 4_200_000}}
    assert "enctoken=ab%2Fc%2Bd%3D%3D" in urls[0] and "user_id=ZG1234" in urls[0]
    assert sent[1] == {"a": "mode", "v": ["full", [256265, opt.token]]}


def test_sentiment_goes_to_telegram_at_the_open_and_when_it_changes(kite, monkeypatch):
    store.update("settings", "oi", {"strikes": 2})
    sent = []
    monkeypatch.setattr(oi.notify, "send", lambda user, channels, subject, body: sent.append((user, channels, subject, body)) or {"telegram": "sent"})
    oi.set_telegram("omkar", True)
    try:
        assert oi.capture_if_due(MON.replace(hour=9, minute=16), S)
        assert [(u, ch, subj) for u, ch, subj, _ in sent] == [("omkar", ["telegram"], "📊 Nifty OI at 9:15 AM: Neutral")]
        body = sent[0][3]
        assert "Nifty 24,512.00" in body and "PCR 1.00" in body and "/oi?day=2026-10-05&at=2026-10-05T09:15" in body
        oi.capture_if_due(MON.replace(hour=9, minute=31), S)
        assert len(sent) == 1                                                 # still Neutral: nothing new to say
        for k in (24400, 24450, 24500):
            kite.oi[f"NIFTY26O06{k}PE"] = 400_000                             # puts written hard
        oi.capture_if_due(MON.replace(hour=9, minute=46), S)
        assert sent[-1][2] == "📊 Nifty OI turned Bullish (was Neutral) · 9:45 AM"
        oi.set_telegram("omkar", False)
        kite.oi.clear()
        oi.capture_if_due(MON.replace(hour=10, minute=1), S)
        assert len(sent) == 2                                                 # opted out
    finally:
        store.delete("oi_alerts", "omkar")


def test_a_snapshot_before_the_open_is_not_the_days_baseline(kite):
    oi.capture(kite, MON.replace(hour=8, minute=40))
    oi.capture(kite, MON.replace(hour=9, minute=16), "2026-10-05T09:15")
    assert [s["id"] for s in oi.day_snapshots("2026-10-05")] == ["2026-10-05T09:15"]


def test_telegram_switch_needs_telegram_set_up(kite, monkeypatch):
    monkeypatch.setattr(config, "SUPERADMIN_USERNAME", "boss")
    monkeypatch.setattr(config, "SUPERADMIN_PASSWORD", "boss-pass-123")
    from app.main import app
    with TestClient(app) as c:
        c.post("/login", data={"username": "omkar", "password": "omkar-pass-1"})
        assert "Set up Telegram first" in c.get("/oi").text
        assert "Notifications page" in c.post("/oi/telegram", data={"on": "1"}).headers["HX-Trigger"]
        store.put("contacts", "omkar", {"telegram_bot_token": "x", "telegram_chat_id": "42"})
        try:
            r = c.post("/oi/telegram", data={"on": "1"})
            assert "checked" in r.text and oi.wants_telegram("omkar")
            assert "checked" not in c.post("/oi/telegram", data={}).text and not oi.wants_telegram("omkar")
        finally:
            store.delete("contacts", "omkar")
            store.delete("oi_alerts", "omkar")


def test_other_alerts_carry_the_latest_reading(kite):
    from app import scanner
    store.update("settings", "oi", {"strikes": 2})
    oi.capture(kite, MON.replace(hour=9, minute=16), "2026-10-05T09:15")
    for k in (24400, 24450, 24500):
        kite.oi[f"NIFTY26O06{k}PE"] = 400_000
    oi.capture(kite, MON.replace(hour=9, minute=31), "2026-10-05T09:30")
    assert scanner._with_sentiment({"user": "omkar"}, MON.replace(hour=9, minute=20)) == \
        "\nNifty OI: Neutral · PCR 1.00 · support 24,400, resistance 24,400 (9:15 AM)"     # the reading as it stood then
    line = scanner._with_sentiment({"user": "omkar"}, MON.replace(hour=10))
    assert line.startswith("\nNifty OI: Bullish · PCR 2.80") and line.endswith("(9:30 AM)")
    assert scanner._sentiment({"user": "omkar"}, MON.replace(hour=10))["label"] == "Bullish"   # webhooks get it as data
    assert scanner._with_sentiment({"user": "omkar"}, MON.replace(day=6, hour=10)) == ""       # nothing yet that day
    store.put("users", "nisha", {"username": "nisha", "role": "user", "active": True, "modules": ["scanner"]})
    assert scanner._with_sentiment({"user": "nisha"}, MON.replace(hour=10)) == ""              # no Nifty OI permission
    store.delete("users", "nisha")
