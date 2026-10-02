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
    # A fixed, market-closed moment: "next check" wording differs while a real session is open.
    monkeypatch.setattr("app.routes.alerts.now_ist", lambda: datetime(2026, 9, 27, 12, 0, tzinfo=IST))
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
    store.put("contacts", "eve", {"emails": ["eve@example.com"]})
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
    store.update("contacts", "boss", {"emails": ["boss@example.com"]})
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
    assert "That was the last level" in sent[3][1] and "Potential buy. INFY crossed below your level of 1489.9." in sent[3][1]
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
    assert [s for s, _ in sent] == ["🔔 Potential sell: INFY trades above 100", "🔔 Potential sell: INFY trades above 110"]
    assert sent[0][1].startswith("Potential sell. INFY crossed above your level of 100.\nPrice: 112 ")
    assert "Still watching: trades above 120" in sent[1][1]

    # An alert with its own message leads with that instead of the buy/sell reading.
    store.update("alerts", "m1", {"note": "Weekly high swept, look for shorts"})
    FakeKite.candles = lambda self, token, tf, day: [Candle(now.replace(second=0), 111, 121, 111, 120)]
    scanner.run_scan(now.replace(second=50))
    subject, body = sent[2]
    assert subject == "🔔 Weekly high swept, look for shorts: INFY trades above 120"
    assert body.startswith("Weekly high swept, look for shorts\nINFY crossed above your level of 120.\nPrice: 121 ")
    assert "Potential" not in subject + body and "That was the last level" in body
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
    assert sent == ["🔔 Potential sell: INFY trades above 1510"]
    # A fall through 1480 fires that level, in the other direction, on the same alert.
    low["v"] = 1478
    scanner.run_scan(now.replace(second=50))
    assert [lv["status"] for lv in store.get("alerts", a["id"])["levels"]] == ["hit", "hit", "active"]
    assert sent[1] == "🔔 Potential buy: INFY trades below 1480"
    assert "Crosses" in client.get("/alerts/list?tab=active&q=").text
    store.delete("alerts", a["id"])
    prices._cache.clear()


def test_several_recipients_per_channel(client, monkeypatch):
    from app.security import decrypt
    login(client, "boss", "boss-pass-123")
    store.delete("contacts", "boss")
    store.put("contacts", "boss", {})
    monkeypatch.setattr(notify, "telegram_bot_info", lambda t: {"username": "alerts_bot", "name": "Alerts"})

    # Telegram: each Find my chat ID adds the chat that last messaged the bot.
    found = iter([{"id": "42", "name": "Ranjith"}, {"id": "-1007", "name": "Desk group"}, {"id": "42", "name": "Ranjith"}])
    monkeypatch.setattr(notify, "telegram_find_chat", lambda t: next(found))
    r = client.post("/notifications/telegram/detect", data={"telegram_bot_token": "123456:abcdefWXYZ"})
    assert "Linked to Ranjith" in r.headers["HX-Trigger"]
    r = client.post("/notifications/telegram/detect")
    assert "Added Desk group" in r.headers["HX-Trigger"] and "Desk group" in r.text and "Chat ID -1007" in r.text
    assert "already linked" in client.post("/notifications/telegram/detect").headers["HX-Trigger"]
    assert store.get("contacts", "boss")["telegram_chat_id"] == "42, -1007"

    calls = []

    class Ok:
        status_code = 200

    class NotFound:
        status_code = 400
        def json(self):
            return {"description": "Bad Request: chat not found"}

    def telegram_api(method, token, **kw):
        calls.append(kw["json"]["chat_id"])
        return NotFound() if kw["json"]["chat_id"] == "999" else Ok()
    monkeypatch.setattr(notify, "_telegram_call", telegram_api)
    assert notify.send("boss", ["telegram"], "s", "b") == {"telegram": "sent"} and calls == ["42", "-1007"]

    # Editing the box replaces the list; one bad chat doesn't stop the others.
    client.post("/notifications/telegram", data={"telegram_chat_id": "42, 999 , -1007"})
    calls.clear()
    result = notify.send("boss", ["telegram"], "s", "b")["telegram"]
    assert calls == ["42", "999", "-1007"] and result.startswith("Reached 2 of 3. 999: Telegram can't find that chat")
    assert "Send test message to all 3" in client.get("/notifications").text

    # Email: one message per confirmed address.
    mails = []
    monkeypatch.setattr(config, "BREVO_API_KEY", "k")
    monkeypatch.setattr(config, "EMAIL_FROM", "alerts@example.com")
    monkeypatch.setattr(notify, "_email_brevo", lambda to, s, b: mails.append(to))
    store.update("contacts", "boss", {"emails": ["a@example.com", "b@example.com"]})
    assert notify.send("boss", ["email"], "s", "b") == {"email": "sent"} and mails == ["a@example.com", "b@example.com"]

    # WhatsApp: every number has its own key. Adding a number only needs the new key.
    client.post("/notifications/whatsapp", data={"whatsapp_phone": "+91 98765 43210", "whatsapp_apikey": "1111111"})
    r = client.post("/notifications/whatsapp", data={"whatsapp_phone": "+919876543210, +91 91234 56789"})
    assert "needs its own CallMeBot key" in r.headers["HX-Trigger"]
    client.post("/notifications/whatsapp", data={"whatsapp_phone": "+919876543210, +91 91234 56789", "whatsapp_apikey": "2222222"})
    saved = store.get("contacts", "boss")
    assert saved["whatsapp_phone"] == "+919876543210, +919123456789" and decrypt(saved["whatsapp_apikey"]) == "1111111, 2222222"
    hits = []

    class Sent:
        status_code, text = 200, "Message queued"
    monkeypatch.setattr(notify.httpx, "get", lambda url, timeout, params: hits.append((params["phone"], params["apikey"])) or Sent())
    assert notify.send("boss", ["whatsapp"], "s", "b") == {"whatsapp": "sent"}
    assert hits == [("+919876543210", "1111111"), ("+919123456789", "2222222")]
    store.delete("contacts", "boss")


def test_email_address_is_confirmed_with_a_code_before_it_gets_alerts(client, monkeypatch):
    import re
    from app.routes import notifications
    login(client, "boss", "boss-pass-123")
    store.delete("contacts", "boss")
    store.put("contacts", "boss", {"email": "Old@Example.com"})       # saved before codes existed
    mails = []
    monkeypatch.setattr(config, "BREVO_API_KEY", "k")
    monkeypatch.setattr(config, "EMAIL_FROM", "alerts@example.com")
    monkeypatch.setattr(notify, "_email_brevo", lambda to, s, b: mails.append((to, s, b)))

    def post(path="", **data):
        return client.post("/notifications/email" + path, data=data)

    def code_for(mail):
        return re.search(r"\b(\d{6})\b", mail[2]).group(1)

    # The old address gets nothing until it is confirmed, and the page says so.
    assert notify.send("boss", ["email"], "s", "b") == {"email": "No recipient details saved"}
    assert "Not confirmed, nothing is sent" in client.get("/notifications").text

    # One address at a time, and a real one.
    assert "one address at a time" in post(email="a@example.com, b@example.com").headers["HX-Trigger"]
    assert "looks incomplete" in post(email="nope").headers["HX-Trigger"]
    assert mails == []

    r = post(email="A@Example.com", action="add")
    assert "emailed a 6-digit code to a@example.com" in r.headers["HX-Trigger"] and 'name="code"' in r.text
    assert mails[0][0] == "a@example.com" and code_for(mails[0]) in mails[0][1]
    assert store.get("contacts", "boss").get("emails") is None                      # not added yet
    assert code_for(mails[0]) not in str(store.get("contacts", "boss"))             # only a hash is kept
    assert "Give it a minute" in post(email="a@example.com").headers["HX-Trigger"] and len(mails) == 1

    wrong = "000000" if code_for(mails[0]) != "000000" else "111111"
    assert "isn't right. 4 tries left" in post(code=wrong, action="confirm").headers["HX-Trigger"]
    r = post(code=code_for(mails[0]), action="confirm")
    assert "a@example.com is confirmed" in r.headers["HX-Trigger"] and "Confirmed" in r.text
    assert store.get("contacts", "boss")["emails"] == ["a@example.com"]
    assert "already confirmed" in post(email="a@example.com").headers["HX-Trigger"]

    # Too many wrong guesses, or an old code, means asking for a new one.
    post(email="b@example.com")
    for _ in range(notifications.CODE_TRIES):
        r = post(code=wrong if code_for(mails[1]) != wrong else "222222", action="confirm")
    assert "Send a new one" in r.headers["HX-Trigger"]
    assert "Too many wrong codes" in post(code=code_for(mails[1]), action="confirm").headers["HX-Trigger"]
    pending = store.get("contacts", "boss")["email_pending"]
    store.update("contacts", "boss", {"email_pending": {**pending, "tries": 0, "expires": "2020-01-01T00:00:00+05:30",
                                                        "sent_at": "2020-01-01T00:00:00+05:30"}})
    assert "expired" in post(code=code_for(mails[1]), action="confirm").headers["HX-Trigger"]
    post(email="b@example.com")
    post(code=code_for(mails[2]))                                                    # Enter in the code box
    post(email="old@example.com")                                                    # confirm the legacy address too
    post(code=code_for(mails[3]), action="confirm")
    saved = store.get("contacts", "boss")
    assert saved["emails"] == ["a@example.com", "b@example.com", "old@example.com"] and not saved.get("email")

    mails.clear()
    assert notify.send("boss", ["email"], "s", "b") == {"email": "sent"}
    assert [m[0] for m in mails] == ["a@example.com", "b@example.com", "old@example.com"]

    # Dropping one address leaves the others; cancelling a pending code adds nothing.
    post("/drop", address="b@example.com")
    post(email="c@example.com")
    assert "Nothing was added" in post("/cancel").headers["HX-Trigger"]
    saved = store.get("contacts", "boss")
    assert saved["emails"] == ["a@example.com", "old@example.com"] and saved["email_pending"] is None
    store.delete("contacts", "boss")


def test_edit_alert_add_change_remove_and_rearm_levels(client, monkeypatch):
    login(client, "boss", "boss-pass-123")
    monkeypatch.setattr("app.routes.alerts._armed_price", lambda u, a: None)
    hit = {"level": 1500.0, "condition": "cross", "status": "hit", "hit_at": "2026-09-28T10:00:00+05:30", "hit_price": 1502.5}
    store.put("alerts", "e1", {
        "id": "e1", "user": "boss", "symbol": "INFY", "name": "INFOSYS", "token": 1, "timeframe": "", "note": "",
        "status": "active", "channels": [], "created_at": "2026-09-28", "armed_at": "2026-09-28T09:15:00+05:30",
        "levels": [hit, {"level": 1400.0, "condition": "cross", "status": "active"},
                   {"level": 1300.0, "condition": "low_below", "status": "active"}]})

    form = client.get("/alerts/e1/edit").text
    assert "Edit INFY alert" in form and 'value="1400"' in form and "hit at ₹1,502.50" in form and "Watch again" in form
    assert client.get("/alerts/nope/edit").status_code == 404
    assert 'hx-get="/alerts/e1/edit"' in client.get("/alerts/list?tab=all&q=").text

    def save(rows, **more):
        return client.post("/alerts/e1/edit", data={
            "extra_index": [r[0] for r in rows], "extra_condition": [r[1] for r in rows],
            "extra_level": [r[2] for r in rows], **more})

    # Keep the fired level and 1400 as they are, drop 1300, add two new levels and a message.
    r = save([("0", "cross", "1500"), ("1", "cross", "1400"), ("new", "close_cross", "1600"), ("new", "cross", "1650"),
              ("new", "cross", "")], timeframe="5m", note="Range high")
    assert "INFY alert updated" in r.headers["HX-Trigger"]
    a = store.get("alerts", "e1")
    assert a["levels"] == [hit, {"level": 1400.0, "condition": "cross", "status": "active"},
                           {"level": 1600.0, "condition": "close_cross", "status": "active"},
                           {"level": 1650.0, "condition": "cross", "status": "active"}]
    assert (a["timeframe"], a["note"], a["status"]) == ("5m", "Range high", "active")
    assert a["armed_at"] > "2026-09-28T09:15:00+05:30"       # new levels only count moves from now on

    # Changing a level's price starts it afresh; nothing new means the arming time is left alone.
    armed = a["armed_at"]
    save([("0", "cross", "1500"), ("1", "cross", "1400"), ("2", "close_cross", "1600"), ("3", "cross", "1650")], timeframe="5m")
    assert store.get("alerts", "e1")["armed_at"] == armed and store.get("alerts", "e1")["note"] == ""

    # Removing every waiting level leaves a triggered alert; adding one to it puts it back on watch.
    save([("0", "cross", "1500")])
    a = store.get("alerts", "e1")
    assert (a["status"], a["trigger_price"], a["timeframe"]) == ("triggered", 1502.5, "")
    save([("0", "cross", "1500"), ("new", "high_above", "1700")])
    a = store.get("alerts", "e1")
    assert a["status"] == "active" and [lv["status"] for lv in a["levels"]] == ["hit", "active"]

    # Watch again on the fired level re-arms just that one.
    save([("0", "cross", "1500"), ("1", "high_above", "1700")], rearm=["0"])
    assert store.get("alerts", "e1")["levels"][0] == {"level": 1500.0, "condition": "cross", "status": "active"}

    assert save([("new", "cross", "")]).headers.get("HX-Reswap") == "none"          # no levels left
    assert save([("new", "nope", "10")]).headers.get("HX-Reswap") == "none"
    assert len(store.get("alerts", "e1")["levels"]) == 2

    # Someone else's alert can't be opened or saved.
    store.put("alerts", "e2", {**store.get("alerts", "e1"), "id": "e2", "user": "other"})
    assert client.get("/alerts/e2/edit").status_code == 404
    assert client.post("/alerts/e2/edit", data={"extra_index": ["new"], "extra_condition": ["cross"], "extra_level": ["1"]}).status_code == 404
    store.delete("alerts", "e1")
    store.delete("alerts", "e2")


def test_webhooks_are_optional_and_post_json_when_a_level_is_hit(client, monkeypatch):
    from app import webhooks
    login(client, "boss", "boss-pass-123")
    monkeypatch.setattr("app.routes.alerts._armed_price", lambda u, a: None)
    monkeypatch.setattr(webhooks, "_is_public", lambda host: host != "localhost")
    posted = []

    class Resp:
        def __init__(self, code):
            self.status_code = code

    def fake_post(url, json, timeout, follow_redirects, headers):
        posted.append((url, json))
        return Resp(500 if "down" in url else 200)
    monkeypatch.setattr(webhooks.httpx, "post", fake_post)

    def create(**more):
        return client.post("/alerts", data={"symbol": "INFY", "condition": "cross", "level": "1510", **more})

    # Bad input is refused and nothing is saved.
    for bad in ({"webhooks": "ftp://example.com/x"}, {"webhooks": "http://localhost/hook"},
                {"webhooks": "https://a.example/h", "webhook_payload": "{not json"}, {"webhook_payload": '{"a": 1}'},
                {"webhooks": "\n".join(f"https://h{i}.example/x" for i in range(6))}):
        assert create(**bad).headers.get("HX-Reswap") == "none", bad
    assert store.list("alerts", user="boss") == []

    r = create(webhooks="https://bot.example/hook\nhttps://down.example/hook\nhttps://bot.example/hook",
               webhook_payload='{"strategy": "sweep",\n "qty": 50, "tags": [["a"], ["b"]]}')
    assert "2 webhooks" in r.text
    a = store.list("alerts", user="boss")[0]
    assert a["webhooks"] == ["https://bot.example/hook", "https://down.example/hook"]
    assert a["webhook_payload"] == '{"strategy":"sweep","qty":50,"tags":[["a"],["b"]]}'

    # The edit panel shows them, can test them, and can change them.
    form = client.get(f"/alerts/{a['id']}/edit").text
    assert "https://bot.example/hook" in form and "&#34;strategy&#34;" in form and "Send a test request" in form
    r = client.post(f"/alerts/{a['id']}/webhooks/test", data={"webhooks": "https://bot.example/hook", "webhook_payload": '{"x": 1}'})
    assert "Test request sent to 1 webhook URL" in r.headers["HX-Trigger"]
    assert posted[-1][1]["test"] is True and posted[-1][1]["payload"] == {"x": 1} and posted[-1][1]["symbol"] == "INFY"
    assert "HTTP 500" in client.post(f"/alerts/{a['id']}/webhooks/test", data={"webhooks": "https://down.example/h"}).headers["HX-Trigger"]
    assert store.get("alerts", a["id"])["webhooks"] == a["webhooks"]          # testing saves nothing

    # A hit posts to every URL; one failing endpoint doesn't stop the other, and the result is logged.
    now = datetime(2026, 9, 28, 11, 0, 30, tzinfo=IST)
    store.put("brokers", "boss", {"mode": "enctoken", "status": "connected", "enctoken": "x"})
    store.update("alerts", a["id"], {"armed_at": now.replace(second=0).isoformat(), "note": "Range high"})

    class FakeKite:
        def profile(self):
            return {}

        def candles(self, token, tf, day):
            return [Candle(now.replace(second=0), 1500, 1512, 1499, 1505)]
    monkeypatch.setattr(brokers, "client_for", lambda u, doc=None: FakeKite())
    posted.clear()
    scanner.run_scan(now)
    assert [url for url, _ in posted] == a["webhooks"]
    body = posted[0][1]
    assert body["symbol"] == "INFY" and body["message"] == "Range high" and body["test"] is False
    assert (body["direction"], body["level"], body["price"], body["condition"]) == ("above", 1510.0, 1512, "high_above")
    assert body["text"] == "INFY crossed above your level of 1510." and body["time"] == now.isoformat()
    assert body["payload"] == {"strategy": "sweep", "qty": 50, "tags": [["a"], ["b"]]}
    event = [e for e in store.list("events", user="boss") if e["alert_id"] == a["id"]][0]
    assert event["delivery"]["webhook"] == "Reached 1 of 2. down.example: HTTP 500"

    # Clearing the box in the edit panel removes the webhooks.
    client.post(f"/alerts/{a['id']}/edit", data={"extra_index": ["0"], "extra_condition": ["cross"], "extra_level": ["1510"]})
    saved = store.get("alerts", a["id"])
    assert saved["webhooks"] == [] and saved["webhook_payload"] == ""
    store.delete("alerts", a["id"])


def test_webhook_urls_must_be_public():
    from app import webhooks
    assert "isn't a web address" in webhooks.url_problem("javascript:alert(1)")
    for private in ("http://127.0.0.1/x", "http://10.0.0.5/x", "http://169.254.169.254/latest/meta-data", "http://[::1]/x"):
        assert "can't be reached from the internet" in webhooks.url_problem(private), private
    assert webhooks.send(["http://127.0.0.1/x"], {}) == "127.0.0.1: not a public address"


def test_nothing_is_scanned_outside_market_hours_or_on_holidays(client, monkeypatch):
    from app import market
    store.put("alerts", "h1", {"id": "h1", "user": "hari", "symbol": "INFY", "token": 1, "timeframe": "", "status": "active",
                               "channels": ["telegram"], "armed_at": "2026-09-25T10:00:00+05:30",
                               "levels": [{"level": 1, "condition": "high_above", "status": "active"}]})
    store.put("contacts", "hari", {"telegram_chat_id": "1", "telegram_bot_token": "x"})
    sent, asked = [], []
    monkeypatch.setitem(notify._SENDERS, "telegram", lambda ct, s, b: sent.append(s))

    def no_kite(u, doc=None):
        asked.append(u)
        raise AssertionError("Kite was called outside market hours")
    monkeypatch.setattr(brokers, "client_for", no_kite)

    thursday = datetime(2026, 10, 1, tzinfo=IST)
    closed = [thursday.replace(hour=9, minute=14, second=59),     # a second before the open
              thursday.replace(hour=15, minute=32),               # after the close and its 2-minute grace
              thursday.replace(hour=23), thursday.replace(hour=3),
              datetime(2026, 10, 2, 11, 0, tzinfo=IST),            # Gandhi Jayanti, a Friday
              datetime(2026, 10, 3, 11, 0, tzinfo=IST)]            # Saturday
    for when in closed:
        scanner.run_scan(when)
    assert asked == [] and sent == []                              # no Kite calls, no alerts, no login notices
    assert store.get("alerts", "h1")["status"] == "active"
    assert scanner.next_open(market.load_settings(), closed[4]) == datetime(2026, 10, 5, 9, 15, tzinfo=IST)

    # The same alert is picked up as soon as the market is open.
    scanner.run_scan(thursday.replace(hour=9, minute=15, second=5))
    assert len(sent) == 1 and "Kite" in sent[0]                    # broker isn't connected: the usual notice
    store.delete("alerts", "h1")

    # The super admin manages the holiday list.
    login(client, "boss", "boss-pass-123")
    assert "Mahatma Gandhi Jayanti" in client.get("/admin/market").text or datetime.now(IST).date().isoformat() > "2026-10-02"
    r = client.post("/admin/market/holidays", data={"day": "2026-12-31", "name": "Year end"})
    assert "Year end" in r.text and not market.is_trading_day(datetime(2026, 12, 31, 11, tzinfo=IST))
    client.post("/admin/market", data={"open_time": "09:15", "close_time": "15:30", "scan_interval": "60"})
    assert "2026-12-31" in market.load_settings().holidays                      # saving hours keeps the holidays
    client.post("/admin/market/holidays/remove", data={"day": "2026-12-31"})
    assert market.is_trading_day(datetime(2026, 12, 31, 11, tzinfo=IST))
    monkeypatch.setattr("app.routes.admin.fetch_nse_holidays", lambda: {"2027-01-26": "Republic Day", "2026-12-25": "Christmas"})
    r = client.post("/admin/market/holidays/fetch")
    assert "Added 1 holiday from NSE" in r.headers["HX-Trigger"] and "Republic Day" in r.text
    store.delete("settings", "market")


def test_fractal_alert_end_to_end(client, monkeypatch):
    from datetime import timedelta
    from app import webhooks
    login(client, "boss", "boss-pass-123")
    scanner._fractal_history.clear()
    store.put("brokers", "boss", {"mode": "enctoken", "status": "connected", "enctoken": "x"})
    store.put("contacts", "boss", {"telegram_chat_id": "1", "telegram_bot_token": "x"})
    sent, posted = [], []
    monkeypatch.setattr(notify, "sender_ready", lambda ch: True)
    monkeypatch.setitem(notify._SENDERS, "telegram", lambda ct, s, b: sent.append((s, b)))
    monkeypatch.setattr(webhooks, "_is_public", lambda host: True)

    class Ok:
        status_code = 200
    monkeypatch.setattr(webhooks.httpx, "post", lambda url, json, timeout, follow_redirects, headers: posted.append(json) or Ok())

    friday = datetime(2026, 9, 25, 9, 15, tzinfo=IST)
    monday = datetime(2026, 9, 28, 9, 15, tzinfo=IST)
    # Friday leaves a fractal high at 110 and a fractal low at 100 (the 9:45 candle).
    history = [Candle(friday + timedelta(minutes=30 * i), *row) for i, row in enumerate(
        [(104, 106, 102, 105), (105, 110, 100, 104), (104, 108, 101, 105), (105, 107, 103, 106)])]
    state = {"minutes": [], "today": []}

    class FakeKite:
        def profile(self):
            return {}

        def candles(self, token, tf, day):
            assert tf == "1m"
            return state["minutes"]

        def candles_range(self, token, tf, start, end):
            assert tf == "30m"
            return history + state["today"]
    monkeypatch.setattr(brokers, "client_for", lambda u, doc=None: FakeKite())

    # Created from the New alert form in Fractals mode: no price level needed.
    r = client.post("/alerts", data={"symbol": "INFY", "kind": "fractal", "fractal_timeframe": "30m", "sides": "both",
                                     "triggers": ["touch", "reject"], "channels": ["telegram"],
                                     "webhooks": "https://bot.example/hook", "webhook_payload": '{"qty": 50}'})
    assert "Watching INFY 30 min fractals" in r.headers["HX-Trigger"] and "Fractal 30 min" in r.text
    assert "levels appear after the first scan" in r.text
    a = store.list("alerts", user="boss")[0]
    assert (a["kind"], a["timeframe"], a["sides"], a["triggers"], a["levels"]) == ("fractal", "30m", "both", ["touch", "reject"], [])
    assert client.post("/alerts", data={"symbol": "INFY", "kind": "fractal", "triggers": []}).headers.get("HX-Reswap") == "none"
    store.update("alerts", a["id"], {"armed_at": friday.replace(hour=14).isoformat()})

    def minute(m, *ohlc):
        return Candle(monday.replace(minute=m), *ohlc)

    # 9:18 on Monday: price is between the two fractals, nothing to report, levels are known.
    state["minutes"] = [minute(15, 106, 107, 105, 106.5), minute(16, 106.5, 108, 106, 107), minute(17, 107, 109.5, 106.8, 109)]
    scanner.run_scan(monday.replace(minute=18, second=5))
    assert sent == []
    page = client.get("/alerts/list?tab=active&q=").text
    assert "110.00" in page and "100.00" in page and "2 unmitigated" in page and 'data-levels="110,100"' in page

    # 9:20: a trade goes above the fractal high. One "taken" message, with the fractal low as the target.
    state["minutes"].append(minute(20, 109, 110.8, 108.9, 110.2))
    scanner.run_scan(monday.replace(minute=20, second=40))
    assert len(sent) == 1
    subject, body = sent[0]
    assert subject == "🔔 Potential sell: INFY 30 min fractal high 110 taken"
    assert "INFY traded above the 30 min fractal high of 110." in body and "Price: 110.8" in body
    assert "Target: 100, the nearest unmitigated fractal low." in body and "Fractal formed 25 Sep, 9:45 AM." in body
    hook = posted[0]
    assert (hook["event"], hook["side"], hook["trigger"], hook["signal"]) == ("fractal_hit", "high", "touch", "sell")
    assert (hook["level"], hook["price"], hook["target"], hook["timeframe"]) == (110, 110.8, 100, "30m")
    assert hook["payload"] == {"qty": 50} and hook["fractal_time"] == history[1].start.isoformat()

    # Later scans in the same candle stay quiet, and the alert keeps watching.
    scanner.run_scan(monday.replace(minute=21, second=40))
    assert len(sent) == 1 and store.get("alerts", a["id"])["status"] == "active"
    assert "1 unmitigated" in client.get("/alerts/list").text

    # 9:45: the 30-minute candle closes back below 110, so the sweep is reported too.
    state["today"] = [Candle(monday, 106, 110.8, 105, 109)]
    state["minutes"].append(minute(44, 109.5, 109.8, 108.8, 109))
    scanner.run_scan(monday.replace(minute=45, second=20))
    assert len(sent) == 2 and sent[1][0] == "🔔 Potential sell: INFY 30 min fractal high 110 swept"
    assert "swept the 30 min fractal high of 110 and closed back below it at 109." in sent[1][1]
    assert posted[1]["trigger"] == "reject"
    scanner.run_scan(monday.replace(minute=46, second=20))
    assert len(sent) == 2
    saved = store.get("alerts", a["id"])
    assert len(saved["fired"]) == 2 and saved["last_hit"]["signal"] == "sell"
    assert "Last: INFY 30 min fractal high 110 swept" in client.get("/alerts/list").text
    events = [e for e in store.list("events", user="boss") if e["alert_id"] == a["id"]]
    assert {e["delivery"].get("webhook") for e in events} == {"sent"}

    # The edit panel is the fractal one; changing the timeframe starts afresh.
    form = client.get(f"/alerts/{a['id']}/edit").text
    assert "Edit INFY fractal alert" in form and "Break fails" in form and "https://bot.example/hook" in form
    r = client.post(f"/alerts/{a['id']}/webhooks/test", data={"webhooks": "https://bot.example/hook"})
    assert "Test request sent" in r.headers["HX-Trigger"] and posted[-1]["test"] is True and posted[-1]["event"] == "fractal_hit"
    r = client.post(f"/alerts/{a['id']}/edit", data={"fractal_timeframe": "1h", "sides": "low", "triggers": ["fail"], "note": "Sweep"})
    assert "INFY fractal alert updated" in r.headers["HX-Trigger"]
    saved = store.get("alerts", a["id"])
    assert (saved["timeframe"], saved["sides"], saved["triggers"], saved["note"], saved["fired"]) == ("1h", "low", ["fail"], "Sweep", [])
    assert saved["webhooks"] == []                                            # the box was left empty
    client.post(f"/alerts/{a['id']}/pause")
    client.post(f"/alerts/{a['id']}/rearm")
    assert store.get("alerts", a["id"])["status"] == "active"
    store.delete("alerts", a["id"])
    scanner._fractal_history.clear()


def test_fractal_gap_is_silent_and_sides_are_respected(monkeypatch):
    from datetime import timedelta
    scanner._fractal_history.clear()
    friday = datetime(2026, 9, 25, 9, 15, tzinfo=IST)
    monday = datetime(2026, 9, 28, 9, 15, tzinfo=IST)
    history = [Candle(friday + timedelta(minutes=30 * i), *row) for i, row in enumerate(
        [(104, 106, 102, 105), (105, 110, 100, 104), (104, 108, 101, 105), (105, 107, 103, 106)])]
    minutes = []

    class FakeKite:
        def profile(self):
            return {}

        def candles(self, token, tf, day):
            return minutes

        def candles_range(self, token, tf, start, end):
            return history
    monkeypatch.setattr(brokers, "client_for", lambda u, doc=None: FakeKite())
    monkeypatch.setattr(notify, "sender_ready", lambda ch: True)
    sent = []
    monkeypatch.setitem(notify._SENDERS, "telegram", lambda ct, s, b: sent.append(s))
    store.put("brokers", "gita", {"mode": "enctoken", "status": "connected", "enctoken": "x"})
    store.put("contacts", "gita", {"telegram_chat_id": "1", "telegram_bot_token": "x"})
    base = {"user": "gita", "kind": "fractal", "symbol": "INFY", "token": 7, "timeframe": "30m", "levels": [], "fired": [],
            "triggers": ["touch", "reject", "fail"], "status": "active", "channels": ["telegram"],
            "armed_at": friday.replace(hour=14).isoformat()}
    store.put("alerts", "g1", {**base, "id": "g1", "sides": "both"})
    store.put("alerts", "g2", {**base, "id": "g2", "sides": "high"})

    # Monday opens above the fractal high: it is retired without a message.
    minutes.append(Candle(monday, 111, 112, 110.5, 111.5))
    scanner.run_scan(monday.replace(minute=16, second=5))
    assert sent == [] and scanner.fractal_levels["g1"]["highs"] == []
    assert [f.level for f in scanner.fractal_levels["g1"]["lows"]] == [100]

    # Price then falls through the fractal low: the both-sides alert reports it, the highs-only one doesn't.
    minutes.append(Candle(monday.replace(minute=17), 104, 104, 99.5, 100.2))
    scanner.run_scan(monday.replace(minute=17, second=40))
    assert sent == ["🔔 Potential buy: INFY 30 min fractal low 100 taken"]
    assert store.get("alerts", "g2").get("fired") == []

    # A fractal alert armed after the move doesn't report it.
    store.put("alerts", "g3", {**base, "id": "g3", "sides": "both", "armed_at": monday.replace(minute=18).isoformat()})
    scanner.run_scan(monday.replace(minute=18, second=40))
    assert len(sent) == 1
    for aid in ("g1", "g2", "g3"):
        store.delete("alerts", aid)
    scanner._fractal_history.clear()


def test_fractal_backtest_shows_signals_targets_and_outcomes(client, monkeypatch):
    from datetime import timedelta
    login(client, "boss", "boss-pass-123")
    day = datetime(2026, 9, 25, 9, 15, tzinfo=IST)
    rows = [(104, 106, 102, 105), (105, 110, 100, 104), (104, 108, 101, 105),      # fractal high 110, low 100
            (105, 111, 104, 109),                                                    # sweeps 110, closes back
            (109, 109, 103, 104), (104, 105, 99.5, 101)]                             # falls to the target, sweeps 100
    candles = [Candle(day + timedelta(minutes=30 * i), *row) for i, row in enumerate(rows)]

    class FakeKite:
        def candles_range(self, token, tf, start, end):
            assert tf == "30m"
            return candles
    monkeypatch.setattr(brokers, "client_for", lambda u, doc=None: FakeKite())
    monkeypatch.setattr("app.routes.alerts.now_ist", lambda: datetime(2026, 9, 26, 12, 0, tzinfo=IST))

    r = client.post("/alerts/simulate", data={"symbol": "INFY", "kind": "fractal", "fractal_timeframe": "30m",
                                              "sides": "both", "triggers": ["touch", "reject", "fail"]})
    page = r.text
    assert "Backtest ready: 4 signals" in r.headers["HX-Trigger"]
    assert "INFY: 4 signals, 2 of 4 reached their target" in page                   # both sell signals reached 100
    assert "Potential sell" in page and "Potential buy" in page and "Swept, closed back" in page
    assert "₹100.00" in page and "Reached 25 Sep" in page
    assert "₹111.00" in page and "Not reached yet" in page       # the buys aim at the new fractal high left by the sweep
    assert store.list("alerts", user="boss") == []                                  # nothing saved

    r = client.post("/alerts/simulate", data={"symbol": "INFY", "kind": "fractal", "sides": "low", "triggers": ["touch"]})
    assert "INFY: 1 signal" in r.text
