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
    assert "Hit at" in client.get("/alerts").text


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
