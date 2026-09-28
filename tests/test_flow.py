"""End-to-end: super admin creates a user, user adds an alert, scanner fires it."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import brokers, config, kite, notify, scanner
from app.kite import Candle, Instrument
from app.market import IST
from app.store import store


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "SUPERADMIN_USERNAME", "boss")
    monkeypatch.setattr(config, "SUPERADMIN_PASSWORD", "boss-pass-123")
    monkeypatch.setattr(kite, "instruments", lambda: {"INFY": Instrument("INFY", "INFOSYS", 408065, False)})
    monkeypatch.setattr("app.routes.alerts.instruments", kite.instruments)
    from app.main import app
    with TestClient(app) as c:
        yield c


def login(c, u, p):
    return c.post("/login", data={"username": u, "password": p}, follow_redirects=False)


def test_permissions_and_alert_fires(client, monkeypatch):
    assert login(client, "boss", "wrong").status_code == 401
    assert login(client, "boss", "boss-pass-123").status_code == 303
    r = client.post("/admin/users", data={"username": "asha", "name": "Asha", "password": "asha-pass-1",
                                          "modules": ["scanner"]})
    assert "asha" in r.text
    client.post("/logout")

    assert login(client, "asha", "asha-pass-1").status_code == 303
    assert client.get("/admin/users").status_code == 403
    assert client.get("/broker").status_code == 403
    r = client.post("/alerts", data={"symbol": "infy", "condition": "high_above", "level": "1500"})
    assert "Watching INFY" in r.headers["HX-Trigger"]

    # Bad symbol is rejected without swapping
    r = client.post("/alerts", data={"symbol": "NOPE", "condition": "high_above", "level": "1"})
    assert r.headers.get("HX-Reswap") == "none"

    # Scanner: connected broker returns a minute candle above the level.
    store.put("brokers", "asha", {"mode": "enctoken", "status": "connected", "enctoken": "x"})
    store.put("contacts", "asha", {"telegram_chat_id": "1", "telegram_bot_token": "x"})
    now = datetime.now(IST).replace(year=2026, month=9, day=28, hour=11, minute=0, second=30)

    class FakeKite:
        def profile(self):
            return {}

        def candles(self, token, tf, day):
            assert tf == "1m"
            return [Candle(now.replace(second=0), 1490, 1502.5, 1489, 1501)]

    sent = []
    monkeypatch.setattr(brokers, "client_for", lambda u, doc=None: FakeKite())
    monkeypatch.setattr(notify, "sender_ready", lambda ch: True)
    monkeypatch.setitem(notify._SENDERS, "telegram", lambda ct, s, b: sent.append(s))
    a = store.list("alerts", user="asha")[0]
    store.update("alerts", a["id"], {"armed_at": now.replace(minute=0, second=0).isoformat(), "channels": ["telegram"]})

    scanner.run_scan(now)
    a = store.get("alerts", a["id"])
    assert a["status"] == "triggered" and a["trigger_price"] == 1502.5
    assert sent and "INFY" in sent[0]
    assert "Hit at" not in client.get("/alerts").text          # default tab is Watching
    assert "Hit at" in client.get("/alerts/list?tab=triggered").text


def test_session_problems_notify_once_per_day(monkeypatch):
    from app.kite import KiteAuthError

    store.put("alerts", "s1", {"id": "s1", "user": "ravi", "symbol": "INFY", "token": 1, "condition": "high_above",
                               "level": 1, "timeframe": "", "status": "active", "armed_at": "2026-09-25T10:00:00+05:30"})
    store.put("contacts", "ravi", {"telegram_chat_id": "1", "telegram_bot_token": "x"})
    sent = []
    monkeypatch.setitem(notify._SENDERS, "telegram", lambda ct, s, b: sent.append(b))
    open_ = datetime(2026, 9, 28, 9, 15, 30, tzinfo=IST)

    # No broker at all: one notice at open, none on later ticks.
    scanner.run_scan(open_)
    scanner.run_scan(open_.replace(minute=20))
    assert len(sent) == 1 and "isn't connected" in sent[0]

    # Next day, connected yesterday but the token died overnight.
    store.put("brokers", "ravi", {"mode": "enctoken", "status": "connected", "enctoken": "old"})

    class DeadKite:
        def profile(self):
            raise KiteAuthError("TokenException")

    monkeypatch.setattr(brokers, "client_for", lambda u, doc=None: DeadKite())
    tue = open_.replace(day=29)
    scanner.run_scan(tue)
    scanner.run_scan(tue.replace(minute=30))
    assert len(sent) == 2 and "expired" in sent[1]
    assert store.get("brokers", "ravi")["status"] == "expired"
    store.delete("alerts", "s1")


def test_send_test_message_records_result(client, monkeypatch):
    login(client, "boss", "boss-pass-123")
    monkeypatch.setattr(notify, "telegram_bot_info", lambda t: {"username": "ranjith_alerts_bot", "name": "Alerts"})
    client.post("/notifications/telegram", data={"telegram_bot_token": "123456:abcdefWXYZ", "telegram_chat_id": "42"})
    page = client.get("/notifications").text
    assert "not tested" in page.lower()
    assert "@ranjith_alerts_bot" in page and "••••WXYZ" in page and "123456:abcdefWXYZ" not in page

    sent = []
    monkeypatch.setitem(notify._SENDERS, "telegram", lambda ct, s, b: sent.append((ct["telegram_chat_id"], s)))
    r = client.post("/notifications/telegram/test")
    assert sent and sent[0][0] == "42" and "Test sent" in r.headers["HX-Trigger"]
    assert store.get("contacts", "boss")["test_telegram"]["ok"] is True
    assert "Test delivered" in r.text

    def boom(ct, s, b):
        raise RuntimeError("Unauthorized")
    monkeypatch.setitem(notify._SENDERS, "telegram", boom)
    r = client.post("/notifications/telegram/test")
    assert "Last test failed" in r.text and "Unauthorized" in r.text

    # Changing details clears the old result; an empty chat box never wipes the linked chat
    client.post("/notifications/telegram", data={"telegram_chat_id": "43"})
    assert store.get("contacts", "boss")["test_telegram"] is None
    client.post("/notifications/telegram", data={"telegram_chat_id": ""})
    assert store.get("contacts", "boss")["telegram_chat_id"] == "43"

    # Remove clears everything for the channel
    r = client.post("/notifications/telegram/remove")
    assert not notify.configured("telegram", store.get("contacts", "boss"))
    assert "Needs setting up" in r.text


def test_user_changes_own_password_and_other_sessions_end(client):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.security import hash_password
    store.put("users", "meera", {"username": "meera", "name": "Meera", "role": "user", "active": True,
                                 "modules": [], "password_hash": hash_password("old-pass-1")})
    login(client, "meera", "old-pass-1")
    other = TestClient(app)  # a second device
    login(other, "meera", "old-pass-1")
    assert other.get("/settings").status_code == 200

    def change(current, new, confirm=None):
        return client.post("/settings/password", data={"current": current, "new": new, "confirm": confirm or new})

    assert "isn't right" in change("wrong", "new-pass-22").headers["HX-Trigger"]
    assert "8 characters" in change("old-pass-1", "short").headers["HX-Trigger"]
    assert "don't match" in change("old-pass-1", "new-pass-22", "new-pass-23").headers["HX-Trigger"]
    r = change("old-pass-1", "new-pass-22")
    assert "Password changed" in r.headers["HX-Trigger"]

    assert client.get("/settings").status_code == 200            # this browser stays signed in
    assert other.get("/settings", follow_redirects=False).status_code == 303  # other device signed out
    client.post("/logout")
    assert login(client, "meera", "old-pass-1").status_code == 401
    assert login(client, "meera", "new-pass-22").status_code == 303


def test_superadmin_password_not_changeable_in_ui(client):
    login(client, "boss", "boss-pass-123")
    assert "SUPERADMIN_PASSWORD" in client.get("/settings").text
    r = client.post("/settings/password", data={"current": "boss-pass-123", "new": "x" * 10, "confirm": "x" * 10})
    assert "SUPERADMIN_PASSWORD" in r.headers["HX-Trigger"]


def test_quote_and_chart_endpoints(client, monkeypatch):
    from datetime import timedelta
    from app import prices
    from app.kite import KiteAuthError
    prices._cache.clear()
    login(client, "boss", "boss-pass-123")
    base = datetime(2026, 9, 25, 9, 15, tzinfo=IST)
    calls = []

    class FakeKite:
        def candles(self, token, tf, day):
            return self.candles_range(token, tf, day, day)

        def candles_range(self, token, tf, start, end):
            calls.append(tf)
            if tf == "1d":
                return [Candle(base - timedelta(days=1), 1480, 1495, 1470, 1490),
                        Candle(base, 1490, 1510, 1485, 1502.5)]
            return [Candle(base + timedelta(minutes=5 * i), 1490 + i, 1492 + i, 1489 + i, 1491 + i) for i in range(3)]

    monkeypatch.setattr(brokers, "client_for", lambda u, doc=None: FakeKite())
    r = client.get("/alerts/quote?symbol=infy")
    assert "1,502.50" in r.text and "+12.50" in r.text and "View chart" in r.text
    client.get("/alerts/quote?symbol=INFY")
    assert calls.count("1d") == 1  # second look served from cache
    assert client.get("/alerts/quote?symbol=NOPE").text == ""

    d = client.get("/alerts/chart?symbol=INFY&range=5D").json()
    assert d["symbol"] == "INFY" and not d["daily"] and len(d["candles"]) == 3
    assert d["candles"][0]["time"] == int(base.timestamp()) + prices.IST_OFFSET
    d = client.get("/alerts/chart?symbol=INFY&range=6M").json()
    assert d["daily"] and d["candles"][-1]["time"] == "2026-09-25"

    prices._cache.clear()

    def dead(u, doc=None):
        raise KiteAuthError("expired")
    monkeypatch.setattr(brokers, "client_for", dead)
    assert "Connect Kite" in client.get("/alerts/quote?symbol=INFY").text
    r = client.get("/alerts/chart?symbol=INFY&range=1D")
    assert r.status_code == 409 and "Broker" in r.json()["error"]


def test_alert_list_tabs_search_sort_and_scan_times(client, monkeypatch):
    from app.scanner import last_checked_at, last_prices
    login(client, "boss", "boss-pass-123")
    base = {"user": "boss", "name": "X", "token": 1, "timeframe": "", "channels": [], "note": "",
            "armed_at": "2026-09-28T09:15:00+05:30"}
    rows = [
        ("f1", "INFY", "high_above", 1500, "active", "2026-09-01"),
        ("f2", "TCS", "high_above", 2100, "active", "2026-09-02"),
        ("f3", "HDFCBANK", "low_below", 1700, "triggered", "2026-09-03"),
        ("f4", "ITC", "high_above", 500, "paused", "2026-09-04"),
    ]
    for aid, sym, cond, lvl, st, created in rows:
        store.put("alerts", aid, {**base, "id": aid, "symbol": sym, "condition": cond, "level": lvl,
                                  "status": st, "created_at": created,
                                  "triggered_at": "2026-09-28T10:00:00+05:30", "trigger_price": 1699})
    last_prices[("boss", "INFY")] = (1400.0, datetime.now(IST))   # 6.7% away
    last_prices[("boss", "TCS")] = (2090.0, datetime.now(IST))    # 0.5% away
    last_checked_at["f2"] = datetime.now(IST)
    store.put("brokers", "boss", {"mode": "enctoken", "status": "connected", "enctoken": "x"})

    page = client.get("/alerts/list?tab=active&sort=near&q=").text
    assert page.index("TCS") < page.index("INFY")          # closest to level first
    assert "HDFCBANK" not in page and "ITC" not in page     # other tabs hidden
    assert "Checked" in page and "Not checked yet" in page and ", next" in page

    page = client.get("/alerts/list?sort=symbol").text     # tab remembered from the session
    assert page.index("INFY") < page.index("TCS") and "HDFCBANK" not in page

    page = client.get("/alerts/list?tab=all&q=hdfc").text
    assert "HDFCBANK" in page and "INFY" not in page and "Triggered" in page
    assert "ITC" in client.get("/alerts/list?tab=paused&q=").text
    for aid, *_ in rows:
        store.delete("alerts", aid)
