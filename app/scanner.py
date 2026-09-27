"""Price-alert evaluation and the in-process scheduler that drives it.

Conditions
* close_above / close_below — a *completed* candle of the chosen timeframe closes
  beyond the level. Only candles that finish after the alert was armed count.
* high_above / low_below — price trades beyond the level at any point. This is
  timeframe-independent (a 15m high crosses a level exactly when some trade does),
  so it is checked on 1-minute candles starting from the minute the alert was armed.

An alert fires once, then moves to "triggered" until the user re-arms it.
"""

import logging
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app import brokers, config, notify
from app.kite import TIMEFRAMES, Candle, KiteAuthError, KiteError
from app.market import CLOSE_GRACE, IST, MarketSettings, in_scan_window, is_trading_day, load_settings, now_ist
from app.store import new_id, store

log = logging.getLogger("scanner")

CONDITIONS = {
    "close_above": "Closes above",
    "close_below": "Closes below",
    "high_above": "Trades above",
    "low_below": "Trades below",
}

# Give Kite a few seconds after a candle ends before trusting its close.
SETTLE = timedelta(seconds=10)


def uses_close(condition: str) -> bool:
    return condition.startswith("close_")


def data_timeframe(alert: dict) -> str:
    return alert["timeframe"] if uses_close(alert["condition"]) else "1m"


def candle_end(c: Candle, timeframe: str, s: MarketSettings) -> datetime:
    close_at = s.close_at(c.start.astimezone(IST))
    minutes = TIMEFRAMES[timeframe][1]
    if minutes is None:
        return close_at
    return min(c.start + timedelta(minutes=minutes), close_at)


def last_boundary(timeframe: str, s: MarketSettings, now: datetime) -> datetime | None:
    """End time of the most recent candle that has completed by `now` (today only)."""
    open_at, close_at = s.open_at(now), s.close_at(now)
    minutes = TIMEFRAMES[timeframe][1]
    if now < open_at:
        return None
    if minutes is None:
        return close_at if now >= close_at else None
    k = int((min(now, close_at) - open_at).total_seconds() // (minutes * 60))
    b = open_at + timedelta(minutes=k * minutes)
    if now >= close_at:
        b = close_at  # a short final candle (e.g. 1h: 15:15–15:30) still closes at the bell
    return b if b > open_at else None


def evaluate(alert: dict, candles: list[Candle], s: MarketSettings, now: datetime) -> tuple[Candle, float] | None:
    """Return (candle, price) for the first candle that satisfies the alert, else None."""
    level = float(alert["level"])
    armed_at = datetime.fromisoformat(alert["armed_at"])
    cond = alert["condition"]

    if uses_close(cond):
        tf = alert["timeframe"]
        for c in candles:
            end = candle_end(c, tf, s)
            if armed_at < end <= now - SETTLE:
                if (cond == "close_above" and c.close > level) or (cond == "close_below" and c.close < level):
                    return c, c.close
        return None

    since = armed_at.replace(second=0, microsecond=0)
    for c in candles:
        if c.start < since:
            continue
        if cond == "high_above" and c.high > level:
            return c, c.high
        if cond == "low_below" and c.low < level:
            return c, c.low
    return None


# ---- runtime state (in memory; rebuilt harmlessly after a restart) ----------

last_prices: dict[tuple[str, str], tuple[float, datetime]] = {}  # (user, symbol) -> (price, as_of)
_last_checked: dict[str, datetime] = {}  # alert id -> last candle boundary evaluated
_scan_lock = threading.Lock()


def _due(alert: dict, s: MarketSettings, now: datetime) -> bool:
    if not uses_close(alert["condition"]):
        return True
    b = last_boundary(alert["timeframe"], s, now - SETTLE)
    return b is not None and _last_checked.get(alert["id"]) != b


def _mark_checked(alert: dict, s: MarketSettings, now: datetime) -> None:
    if uses_close(alert["condition"]):
        _last_checked[alert["id"]] = last_boundary(alert["timeframe"], s, now - SETTLE)


def describe(alert: dict) -> str:
    text = f"{alert['symbol']} {CONDITIONS[alert['condition']].lower()} {alert['level']:g}"
    if uses_close(alert["condition"]):
        text += f" on {alert['timeframe']}"
    return text


def fire(alert: dict, candle: Candle, price: float, now: datetime) -> None:
    store.update("alerts", alert["id"], {
        "status": "triggered",
        "triggered_at": now.isoformat(),
        "trigger_price": price,
        "trigger_candle": candle.start.isoformat(),
    })
    subject = f"🔔 {describe(alert)}"
    body = (
        f"{alert['symbol']} hit your level of {alert['level']:g}.\n"
        f"Price: {price:g} (candle {candle.start.astimezone(IST):%-I:%M %p})\n"
        f"Time: {now:%d %b, %-I:%M %p} IST"
    )
    if alert.get("note"):
        body += f"\nNote: {alert['note']}"
    results = notify.send(alert["user"], alert.get("channels", []), subject, body)
    store.put("events", new_id(), {
        "user": alert["user"], "alert_id": alert["id"], "symbol": alert["symbol"],
        "summary": describe(alert), "price": price, "at": now.isoformat(), "delivery": results,
    })
    log.info("fired %s for %s: %s", alert["id"], alert["user"], results)


# ---- simulation (replay a past session with real data) -----------------------

def last_session_day(s: MarketSettings, now: datetime) -> datetime:
    """Most recent weekday whose session has finished."""
    day = now if is_trading_day(now) and now >= s.close_at(now) else now - timedelta(days=1)
    while not is_trading_day(day):
        day -= timedelta(days=1)
    return day


def simulate(alert: dict, client, s: MarketSettings, now: datetime) -> dict | None:
    """Evaluate `alert` as if it had been armed at the open of the last trading session.
    Walks back over holidays (days with no candles). Returns None if nothing found in a week."""
    day = last_session_day(s, now)
    for _ in range(7):
        candles = client.candles(alert["token"], data_timeframe(alert), day.date())
        if candles:
            break
        day -= timedelta(days=1)
        while not is_trading_day(day):
            day -= timedelta(days=1)
    else:
        return None
    replay = {**alert, "armed_at": s.open_at(day).isoformat()}
    hit = evaluate(replay, candles, s, s.close_at(day) + CLOSE_GRACE)
    return {
        "day": s.open_at(day),
        "hit": hit,
        "high": max(c.high for c in candles),
        "low": min(c.low for c in candles),
        "close": candles[-1].close,
    }


def simulation_message(alert: dict, result: dict) -> tuple[str, str]:
    day = f"{result['day']:%a %-d %b}"
    if result["hit"]:
        candle, price = result["hit"]
        subject = f"🧪 Simulation: {describe(alert)}"
        body = (f"On {day} this alert would have fired at {candle.start.astimezone(IST):%-I:%M %p}, "
                f"price {price:,.2f}.")
    else:
        subject = f"🧪 Simulation: {alert['symbol']} would not have fired"
        body = (f"On {day}, {describe(alert)} was not met. "
                f"Day's range {result['low']:,.2f} to {result['high']:,.2f}, close {result['close']:,.2f}.")
    body += "\nThis was a test with real prices from Kite. Your alerts are unchanged."
    return subject, body


# ---- broker session health --------------------------------------------------
# Sessions expire overnight (~6 AM). That's normal, not an error: the scanner just
# waits for market open, checks each user's session once, and if it isn't usable
# tells the user (at most once a day) on their notification channels.

_session_ok_on: dict[str, date] = {}  # user -> day their session was confirmed working


def session_notice(username: str, status: str, reason: str, alert_count: int, now: datetime) -> None:
    """Mark the broker unusable and tell the user, at most once per day."""
    brokers.set_status(username, status, reason)
    _session_ok_on.pop(username, None)
    doc = brokers.load(username)
    today = now.date().isoformat()
    if doc.get("notice_on") == today:
        return
    store.update(brokers.COL, username, {"notice_on": today})
    what = "isn't connected" if status == "not_set" else "session has expired"
    body = (
        f"Your Kite {what}, so your {alert_count} active alert{'s' if alert_count != 1 else ''} "
        f"can't be checked today. Log in from the Broker page and they'll pick up from there."
    )
    results = notify.send(username, notify.ready_channels(username), "⚠️ Kite login needed", body)
    log.info("session notice for %s (%s): %s", username, status, results)


def _ensure_session(username: str, alert_count: int, now: datetime):
    """Return a working KiteClient, or None if the user's session can't be used right now."""
    doc = brokers.load(username)
    if doc.get("status") in (None, "not_set") and not (doc.get("access_token") or doc.get("enctoken")):
        session_notice(username, "not_set", "Kite isn't connected yet.", alert_count, now)
        return None
    if doc.get("status") in ("expired", "error"):
        # Stays paused until the user logs in again (which sets status back to connected).
        session_notice(username, doc["status"], doc.get("message", ""), alert_count, now)
        return None
    try:
        client = brokers.client_for(username, doc)
        if _session_ok_on.get(username) != now.date():
            client.profile()  # first check of the day
            _session_ok_on[username] = now.date()
            if doc.get("status") != "connected":
                brokers.set_status(username, "connected")
        return client
    except KiteAuthError as e:
        session_notice(username, "expired", str(e), alert_count, now)
    except KiteError as e:
        log.warning("couldn't verify Kite session for %s, will retry: %s", username, e)
    return None


def _scan_user(username: str, alerts: list[dict], s: MarketSettings, now: datetime) -> None:
    client = _ensure_session(username, len(alerts), now)
    if not client:
        return
    cache: dict[tuple[int, str], list[Candle]] = {}
    for alert in alerts:
        if not _due(alert, s, now):
            continue
        tf = data_timeframe(alert)
        key = (alert["token"], tf)
        try:
            if key not in cache:
                cache[key] = client.candles(alert["token"], tf, now.date())
        except KiteAuthError as e:
            session_notice(username, "expired", str(e), len(alerts), now)
            return
        except KiteError as e:
            log.warning("candles failed for %s %s: %s", alert["symbol"], tf, e)
            continue
        candles = cache[key]
        if candles:
            last_prices[(username, alert["symbol"])] = (candles[-1].close, now)
        hit = evaluate(alert, candles, s, now)
        _mark_checked(alert, s, now)
        if hit:
            fire(alert, *hit, now)


def run_scan(now: datetime | None = None) -> None:
    if not _scan_lock.acquire(blocking=False):
        return
    try:
        s = load_settings()
        now = now or now_ist()
        if not in_scan_window(s, now):
            return
        by_user: dict[str, list[dict]] = defaultdict(list)
        for a in store.list("alerts", status="active"):
            by_user[a["user"]].append(a)
        for username, alerts in by_user.items():
            try:
                _scan_user(username, alerts, s, now)
            except Exception:
                log.exception("scan failed for %s", username)
    finally:
        _scan_lock.release()


def session_restored(username: str) -> None:
    """Called after a successful login so today's scans resume immediately."""
    _session_ok_on[username] = now_ist().date()


# ---- scheduler + keep-alive ----------------------------------------------------

scheduler = BackgroundScheduler(timezone=IST)
_stop = threading.Event()


def _add_scan_job(interval: int) -> None:
    scheduler.add_job(run_scan, IntervalTrigger(seconds=interval), id="scan",
                      max_instances=1, coalesce=True, replace_existing=True)


def start() -> None:
    interval = load_settings().scan_interval
    _add_scan_job(interval)
    scheduler.start()
    threading.Thread(target=_keep_alive, name="keep-alive", daemon=True).start()
    log.info("scanner started, every %ss during market hours (IST)", interval)


def stop() -> None:
    _stop.set()
    if scheduler.running:
        scheduler.shutdown(wait=False)


def reschedule(seconds: int) -> None:
    if scheduler.running:
        scheduler.reschedule_job("scan", trigger=IntervalTrigger(seconds=seconds))


def _keep_alive() -> None:
    """Ping our public URL so Render's free tier doesn't sleep, and revive the scheduler if it died."""
    while not _stop.wait(config.KEEP_ALIVE_SECONDS):
        try:
            if not scheduler.running:
                log.warning("scheduler was stopped, restarting it")
                scheduler.start()
            if not scheduler.get_job("scan"):
                log.warning("scan job was missing, adding it back")
                _add_scan_job(load_settings().scan_interval)
        except Exception:
            log.exception("scheduler watchdog failed")
        if config.KEEP_ALIVE_URL:
            try:
                httpx.get(config.KEEP_ALIVE_URL + "/health", timeout=20)
            except httpx.HTTPError as e:
                log.warning("keep-alive ping failed: %s", e)
