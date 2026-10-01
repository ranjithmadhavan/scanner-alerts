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
    listed = [Instrument("INFY", "INFOSYS", 408065, False),
              Instrument("SENSEX", "SENSEX", 265, True, "BSE"),
              Instrument("NIFTY26OCT24500CE", "NIFTY 27 OCT 26 24500 CE", 99, False, "NFO", "CE", "2026-10-27")]
    monkeypatch.setattr(kite, "instruments", lambda: {i.key: i for i in listed})
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
    # Both layouts are rendered (CSS picks one from localStorage), with the same alerts in each.
    assert 'class="layout-list' in page and 'class="layout-cards' in page
    assert page.count('data-chart="TCS"') == 2 and 'data-layout-set="cards"' in page

    page = client.get("/alerts/list?sort=symbol").text     # tab remembered from the session
    assert page.index("INFY") < page.index("TCS") and "HDFCBANK" not in page

    page = client.get("/alerts/list?tab=all&q=hdfc").text
    assert "HDFCBANK" in page and "INFY" not in page and "Triggered" in page
    assert "ITC" in client.get("/alerts/list?tab=paused&q=").text
    for aid, *_ in rows:
        store.delete("alerts", aid)


def test_email_via_brevo(monkeypatch):
    posted = {}

    class Resp:
        status_code = 201
        def json(self):
            return {"messageId": "x"}

    def fake_post(url, headers, json, timeout):
        posted.update(url=url, headers=headers, json=json)
        return Resp()

    monkeypatch.setattr(config, "BREVO_API_KEY", "k-123")
    monkeypatch.setattr(config, "EMAIL_FROM", "alerts@example.com")
    monkeypatch.setattr(notify.httpx, "post", fake_post)
    store.put("contacts", "eve", {"email": "eve@example.com"})
    assert notify.email_provider() == "brevo" and notify.sender_ready("email")
    assert notify.send("eve", ["email"], "INFY hit <1500>", "Line one\nLine two") == {"email": "sent"}
    assert posted["url"].endswith("/v3/smtp/email") and posted["headers"]["api-key"] == "k-123"
    body = posted["json"]
    assert body["sender"]["email"] == "alerts@example.com" and body["to"] == [{"email": "eve@example.com"}]
    assert "&lt;1500&gt;" in body["htmlContent"] and body["textContent"] == "Line one\nLine two"

    class Bad(Resp):
        status_code = 400
        def json(self):
            return {"message": "sender not valid"}
    monkeypatch.setattr(notify.httpx, "post", lambda *a, **k: Bad())
    assert notify.send("eve", ["email"], "s", "b") == {"email": "sender not valid"}


def test_email_test_reminds_about_spam(client, monkeypatch):
    login(client, "boss", "boss-pass-123")
    monkeypatch.setattr(config, "BREVO_API_KEY", "k")
    monkeypatch.setattr(config, "EMAIL_FROM", "alerts@example.com")
    monkeypatch.setitem(notify._SENDERS, "email", lambda ct, s, b: None)
    client.post("/notifications/email", data={"email": "boss@example.com"})
    r = client.post("/notifications/email/test")
    assert "spam folder" in r.headers["HX-Trigger"]
    assert "Not spam" in r.text and "Test sent" in r.text


def test_login_notice_only_on_weekdays_once_a_day(monkeypatch):
    store.put("alerts", "w1", {"id": "w1", "user": "kiran", "symbol": "INFY", "token": 1, "condition": "high_above",
                               "level": 1, "timeframe": "", "status": "active", "armed_at": "2026-09-25T10:00:00+05:30"})
    store.put("contacts", "kiran", {"telegram_chat_id": "1", "telegram_bot_token": "x"})
    sent = []
    monkeypatch.setitem(notify._SENDERS, "telegram", lambda ct, s, b: sent.append(b))

    saturday = datetime(2026, 10, 3, 9, 16, tzinfo=IST)
    scanner.run_scan(saturday)
    scanner.run_scan(saturday.replace(day=4))           # Sunday
    assert sent == []                                   # no notices at weekends

    monday = datetime(2026, 10, 5, 9, 15, 20, tzinfo=IST)
    scanner.run_scan(monday.replace(hour=9, minute=10))  # before the open: nothing yet
    assert sent == []
    for minute in (15, 16, 30, 45):                      # first tick after 9:15 notifies, later ticks don't
        scanner.run_scan(monday.replace(minute=minute))
    assert len(sent) == 1 and "Kite" in sent[0]
    scanner.run_scan(monday.replace(day=6))               # next day: one more
    assert len(sent) == 2
    store.delete("alerts", "w1")


def test_levels_fire_one_at_a_time_and_the_rest_stay_on_watch(client, monkeypatch):
    login(client, "boss", "boss-pass-123")
    r = client.post("/alerts", data={
        "symbol": "infy", "condition": "high_above", "level": "1500", "timeframe": "15m",
        "extra_condition": ["low_below", "high_above", "close_above", "high_above"],
        "extra_level": ["1400", "1550", "1600", ""]})          # the empty row is ignored
    assert "Watching INFY at 4 levels" in r.headers["HX-Trigger"]
    a = store.list("alerts", user="boss")[0]
    assert [(lv["condition"], lv["level"], lv["status"]) for lv in a["levels"]] == [
        ("high_above", 1500, "active"), ("low_below", 1400, "active"),
        ("high_above", 1550, "active"), ("close_above", 1600, "active")]
    assert a["timeframe"] == "15m"

    r = client.post("/alerts", data={"symbol": "infy", "condition": "high_above", "level": "1",
                                     "extra_condition": ["high_above"], "extra_level": ["-5"]})
    assert r.headers.get("HX-Reswap") == "none"

    store.put("brokers", "boss", {"mode": "enctoken", "status": "connected", "enctoken": "x"})
    store.put("contacts", "boss", {"telegram_chat_id": "1", "telegram_bot_token": "x"})
    now = datetime(2026, 9, 28, 11, 0, 30, tzinfo=IST)
    store.update("alerts", a["id"], {"armed_at": now.replace(minute=0, second=0).isoformat(), "channels": ["telegram"]})
    high = {"v": 1502.5}

    class FakeKite:
        def profile(self):
            return {}

        def candles(self, token, tf, day):
            return [Candle(now.replace(second=0), 1490, high["v"], 1489, 1495)] if tf == "1m" else []

    sent = []
    monkeypatch.setattr(brokers, "client_for", lambda u, doc=None: FakeKite())
    monkeypatch.setattr(notify, "sender_ready", lambda ch: True)
    monkeypatch.setitem(notify._SENDERS, "telegram", lambda ct, s, b: sent.append((s, b)))

    def statuses():
        return [lv["status"] for lv in store.get("alerts", a["id"])["levels"]]

    # 1500 is crossed: one message, that level switches off, the alert keeps watching.
    scanner.run_scan(now)
    assert statuses() == ["hit", "active", "active", "active"]
    assert store.get("alerts", a["id"])["status"] == "active"
    assert len(sent) == 1 and "trades above 1500" in sent[0][0]
    assert "Still watching: trades below 1400, trades above 1550, closes above 1600 on 15m" in sent[0][1]
    page = client.get("/alerts/list?tab=active&q=").text
    assert "1 of 4 hit" in page and "This level is switched off" in page
    assert 'data-levels="1500.0,1400.0,1550.0,1600.0"' in page

    # Same price on the next scan: the level that already fired stays quiet.
    scanner.run_scan(now.replace(second=45))
    assert len(sent) == 1

    # Pausing and resuming doesn't bring the fired level back.
    client.post(f"/alerts/{a['id']}/pause")
    client.post(f"/alerts/{a['id']}/rearm")
    assert statuses() == ["hit", "active", "active", "active"]
    store.update("alerts", a["id"], {"armed_at": now.replace(minute=0, second=0).isoformat()})

    # A jump through 1550: its own message. Then the low and the close levels finish the alert.
    high["v"] = 1560
    scanner.run_scan(now.replace(minute=1))
    assert statuses() == ["hit", "active", "hit", "active"] and len(sent) == 2
    lv = store.get("alerts", a["id"])["levels"]
    store.update("alerts", a["id"], {"levels": [lv[0], lv[1], lv[2], {**lv[3], "condition": "low_below", "level": 1489.5}]})
    scanner.run_scan(now.replace(minute=2))
    assert statuses() == ["hit", "active", "hit", "hit"] and len(sent) == 3
    assert store.get("alerts", a["id"])["status"] == "active"
    lv = store.get("alerts", a["id"])["levels"]
    store.update("alerts", a["id"], {"levels": [lv[0], {**lv[1], "level": 1489.9}, lv[2], lv[3]]})
    scanner.run_scan(now.replace(minute=3))
    done = store.get("alerts", a["id"])
    assert done["status"] == "triggered" and done["trigger_price"] == 1489 and len(sent) == 4
    assert "That was the last level" in sent[3][1]
    assert "All 4 levels hit" in client.get("/alerts/list?tab=triggered").text

    # Watch again puts every level back on watch.
    client.post(f"/alerts/{a['id']}/rearm")
    assert statuses() == ["active"] * 4 and store.get("alerts", a["id"])["status"] == "active"
    store.delete("alerts", a["id"])


def test_two_levels_crossed_in_one_scan_send_two_messages(monkeypatch):
    now = datetime(2026, 9, 28, 11, 0, 30, tzinfo=IST)
    store.put("alerts", "m1", {
        "id": "m1", "user": "dev", "symbol": "INFY", "token": 1, "timeframe": "", "status": "active",
        "channels": ["telegram"], "armed_at": now.replace(minute=0, second=0).isoformat(),
        "levels": [{"level": 100, "condition": "high_above", "status": "active"},
                   {"level": 110, "condition": "high_above", "status": "active"},
                   {"level": 120, "condition": "high_above", "status": "active"}]})
    store.put("brokers", "dev", {"mode": "enctoken", "status": "connected", "enctoken": "x"})
    store.put("contacts", "dev", {"telegram_chat_id": "1", "telegram_bot_token": "x"})

    class FakeKite:
        def profile(self):
            return {}

        def candles(self, token, tf, day):
            return [Candle(now.replace(second=0), 99, 112, 99, 111)]

    sent = []
    monkeypatch.setattr(brokers, "client_for", lambda u, doc=None: FakeKite())
    monkeypatch.setattr(notify, "sender_ready", lambda ch: True)
    monkeypatch.setitem(notify._SENDERS, "telegram", lambda ct, s, b: sent.append((s, b)))
    scanner.run_scan(now)
    a = store.get("alerts", "m1")
    assert [lv["status"] for lv in a["levels"]] == ["hit", "hit", "active"] and a["status"] == "active"
    assert [s for s, _ in sent] == ["🔔 INFY trades above 100", "🔔 INFY trades above 110"]
    assert "Still watching: trades above 120" in sent[1][1]
    store.delete("alerts", "m1")


def test_other_exchanges_search_alert_and_expiry(client, monkeypatch):
    login(client, "boss", "boss-pass-123")
    page = client.get("/alerts/symbols?symbol=sensex").text
    assert 'data-symbol="BSE:SENSEX"' in page and "Index" in page
    assert 'data-symbol="NFO:NIFTY26OCT24500CE"' in client.get("/alerts/symbols?symbol=nifty 24500 ce").text

    r = client.post("/alerts", data={"symbol": "BSE:SENSEX", "condition": "high_above", "level": "82000"})
    assert "Watching SENSEX" in r.headers["HX-Trigger"] and 'data-chart="BSE:SENSEX"' in r.text
    a = store.list("alerts", user="boss")[0]
    assert (a["exchange"], a["token"]) == ("BSE", 265)
    store.delete("alerts", a["id"])

    # An option alert is paused once its contract has expired, and can't be re-armed.
    client.post("/alerts", data={"symbol": "NFO:NIFTY26OCT24500CE", "condition": "high_above", "level": "150"})
    a = store.list("alerts", user="boss")[0]
    assert a["expiry"] == "2026-10-27"
    monkeypatch.setattr(brokers, "client_for", lambda u, doc=None: pytest.fail("expired contract was scanned"))
    after_expiry = datetime(2026, 10, 28, 10, 0, tzinfo=IST)
    scanner.run_scan(after_expiry)
    assert store.get("alerts", a["id"])["status"] == "paused"
    monkeypatch.setattr("app.routes.alerts.now_ist", lambda: after_expiry)
    assert "Contract expired" in client.get("/alerts/list?tab=paused").text
    r = client.post(f"/alerts/{a['id']}/rearm")
    assert "expired" in r.headers["HX-Trigger"] and store.get("alerts", a["id"])["status"] == "paused"
    store.delete("alerts", a["id"])


def test_crosses_levels_on_both_sides_of_price(client, monkeypatch):
    from app import prices
    prices._cache.clear()
    login(client, "boss", "boss-pass-123")
    now = datetime(2026, 9, 28, 11, 0, 30, tzinfo=IST)
    low = {"v": 1489}

    class FakeKite:
        def profile(self):
            return {}

        def candles(self, token, tf, day):
            return [Candle(now.replace(second=0), 1500, 1512, low["v"], 1505)]

        def candles_range(self, token, tf, start, end):
            return [Candle(now.replace(hour=9, minute=15), 1490, 1505, 1485, 1500)]

    sent = []
    store.put("brokers", "boss", {"mode": "enctoken", "status": "connected", "enctoken": "x"})
    store.put("contacts", "boss", {"telegram_chat_id": "1", "telegram_bot_token": "x"})
    monkeypatch.setattr(brokers, "client_for", lambda u, doc=None: FakeKite())
    monkeypatch.setattr(notify, "sender_ready", lambda ch: True)
    monkeypatch.setitem(notify._SENDERS, "telegram", lambda ct, s, b: sent.append(s))

    client.post("/alerts", data={"symbol": "INFY", "condition": "cross", "level": "1510", "channels": ["telegram"],
                                 "extra_condition": ["cross", "cross"], "extra_level": ["1480", "1530"]})
    a = store.list("alerts", user="boss")[0]
    assert a["armed_price"] == 1500 and a["timeframe"] == ""
    store.update("alerts", a["id"], {"armed_at": now.replace(second=0).isoformat()})

    # Price is at 1500. The level above fires on the way up; the one below is untouched at a low of 1489.
    scanner.run_scan(now)
    assert [lv["status"] for lv in store.get("alerts", a["id"])["levels"]] == ["hit", "active", "active"]
    assert sent == ["🔔 INFY trades above 1510"]
    # A fall through 1480 fires that level, in the other direction, on the same alert.
    low["v"] = 1478
    scanner.run_scan(now.replace(second=50))
    assert [lv["status"] for lv in store.get("alerts", a["id"])["levels"]] == ["hit", "hit", "active"]
    assert sent[1] == "🔔 INFY trades below 1480"
    assert "Crosses" in client.get("/alerts/list?tab=active&q=").text
    store.delete("alerts", a["id"])
    prices._cache.clear()
