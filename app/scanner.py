"""Price-alert evaluation and the in-process scheduler that drives it.

Conditions
* close_above / close_below — a *completed* candle of the chosen timeframe closes
  beyond the level. Only candles that finish after the alert was armed count.
* high_above / low_below — price trades beyond the level at any point. This is
  timeframe-independent (a 15m high crosses a level exactly when some trade does),
  so it is checked on 1-minute candles starting from the minute the alert was armed.
* cross / close_cross — the same two checks without a fixed direction. The level fires
  when price gets to the other side of it from where it started: the day's open, or the
  price when the alert was armed if that was during today's session. An overnight gap
  through a level therefore doesn't fire it; the level just waits in the other direction.

An alert holds one or more levels, each with its own condition. A level fires once and is
then switched off; the alert keeps watching its other levels and moves to "triggered"
only when the last one has fired. It stays there until the user re-arms it.

A fractal alert (kind "fractal") has no levels of its own: its levels are the unmitigated
fractals of the instrument on the alert's timeframe (see fractals.py), recalculated as candles
close. It reports each fractal once per trigger and keeps watching until paused or removed.
"""

import json
import logging
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta

import httpx
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app import brokers, config, fractals, notify, oi, webhooks
from app.kite import TIMEFRAMES, Candle, KiteAuthError, KiteError
from app.market import CLOSE_GRACE, IST, MarketSettings, in_scan_window, is_trading_day, load_settings, now_ist
from app.store import new_id, store

log = logging.getLogger("scanner")

CONDITIONS = {
    "cross": "Crosses",
    "high_above": "Trades above",
    "low_below": "Trades below",
    "close_cross": "Closes across",
    "close_above": "Closes above",
    "close_below": "Closes below",
}
EITHER_WAY = ("cross", "close_cross")

# Give Kite a few seconds after a candle ends before trusting its close.
SETTLE = timedelta(seconds=10)


def uses_close(condition: str) -> bool:
    return condition.startswith("close_")


def data_timeframe(alert: dict) -> str:
    return alert["timeframe"] if uses_close(alert["condition"]) else "1m"


def levels_of(alert: dict) -> list[dict]:
    """The alert's levels as {level, condition, status: active|hit, hit_at, hit_price}.
    Alerts saved before multi-level support have a single top-level level/condition."""
    if "levels" in alert:
        return [dict(lv) for lv in alert["levels"]]
    hit = alert.get("status") == "triggered"
    return [{
        "level": alert.get("level"), "condition": alert["condition"], "status": "hit" if hit else "active",
        "hit_at": alert.get("triggered_at") if hit else None,
        "hit_price": alert.get("trigger_price") if hit else None,
    }]


def level_view(alert: dict, lv: dict) -> dict:
    """The alert seen through one of its levels: what evaluate() and describe() work on."""
    return {**alert, "level": lv["level"], "condition": lv["condition"]}


def open_views(alert: dict) -> list[tuple[int, dict]]:
    """(index, view) for each level that hasn't fired yet."""
    return [(i, level_view(alert, lv)) for i, lv in enumerate(levels_of(alert)) if lv["status"] == "active"]


def alert_key(alert: dict) -> str:
    """Matches Instrument.key: bare symbol on NSE, EXCHANGE:SYMBOL elsewhere."""
    exchange = alert.get("exchange") or "NSE"
    return alert["symbol"] if exchange == "NSE" else f"{exchange}:{alert['symbol']}"


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

    # Either-way conditions: which side price started on. Set from the first candle that counts.
    start = None

    def started(c: Candle) -> float:
        armed_now = alert.get("armed_price") is not None and armed_at >= c.start
        return float(alert["armed_price"]) if armed_now else c.open

    if uses_close(cond):
        tf = alert["timeframe"]
        for c in candles:
            end = candle_end(c, tf, s)
            if end <= armed_at:
                continue
            if start is None:
                start = started(c)
            if end > now - SETTLE:
                continue
            up = cond == "close_above" or (cond == "close_cross" and start <= level)
            down = cond == "close_below" or (cond == "close_cross" and start >= level)
            if (up and c.close > level) or (down and c.close < level):
                return c, c.close
        return None

    since = armed_at.replace(second=0, microsecond=0)
    for c in candles:
        if c.start < since:
            continue
        if start is None:
            start = started(c)
        if (cond == "high_above" or (cond == "cross" and start <= level)) and c.high > level:
            return c, c.high
        if (cond == "low_below" or (cond == "cross" and start >= level)) and c.low < level:
            return c, c.low
    return None


def resolved(alert: dict, price: float) -> dict:
    """An either-way level that fired at `price`, restated with the direction it went."""
    cond = alert["condition"]
    if cond in EITHER_WAY:
        side = "above" if price > float(alert["level"]) else "below"
        cond = f"close_{side}" if uses_close(cond) else ("high_above" if side == "above" else "low_below")
    return {**alert, "condition": cond}


# ---- runtime state (in memory; rebuilt harmlessly after a restart) ----------

last_prices: dict[tuple[str, str], tuple[float, datetime]] = {}  # (user, alert_key) -> (price, as_of)
last_checked_at: dict[str, datetime] = {}  # alert id -> when the scanner last evaluated it
last_run: dict = {}  # {"at": datetime, "alerts": int} for the most recent scan inside market hours
_last_checked: dict[str, datetime] = {}  # alert id -> last candle boundary evaluated
fractal_levels: dict[str, dict] = {}  # fractal alert id -> {"resistance": [Fractal], "support": [Fractal]} still unmitigated
_fractal_history: dict[tuple, tuple[tuple, list[Candle]]] = {}  # (user, token, tf, what) -> (fetched for, candles)
_scan_lock = threading.Lock()


def _due(alert: dict, s: MarketSettings, now: datetime) -> bool:
    """`alert` is a level view. Close levels of one alert share its timeframe, so one marker per alert."""
    if not uses_close(alert["condition"]):
        return True
    b = last_boundary(alert["timeframe"], s, now - SETTLE)
    return b is not None and _last_checked.get(alert["id"]) != b


def _mark_checked(alert: dict, s: MarketSettings, now: datetime) -> None:
    _last_checked[alert["id"]] = last_boundary(alert["timeframe"], s, now - SETTLE)


def describe(alert: dict) -> str:
    text = f"{alert['symbol']} {CONDITIONS[alert['condition']].lower()} {alert['level']:g}"
    if uses_close(alert["condition"]):
        text += f" on {alert['timeframe']}"
    return text


def fire(alert: dict, index: int, candle: Candle, price: float, now: datetime) -> dict:
    """Level `index` was hit: switch it off, tell the user, and return the alert as now stored.
    The alert itself stays active while any other level is still waiting."""
    levels = levels_of(alert)
    levels[index] = {**levels[index], "status": "hit", "hit_at": now.isoformat(), "hit_price": price}
    changes = {"levels": levels}
    if not any(lv["status"] == "active" for lv in levels):
        changes.update(status="triggered", triggered_at=now.isoformat(), trigger_price=price,
                       trigger_candle=candle.start.isoformat())
    store.update("alerts", alert["id"], changes)
    hit, alert = resolved(level_view(alert, levels[index]), price), {**alert, **changes}
    # The alert's own message leads if it has one. Otherwise read the hit as a liquidity trade:
    # a push up through a level is a potential sell, a drop through it a potential buy.
    up = hit["condition"] in ("high_above", "close_above")
    custom = (alert.get("note") or "").strip()
    signal = custom or ("Potential sell" if up else "Potential buy")
    moved = ("closed" if uses_close(hit["condition"]) else "crossed") + (" above" if up else " below")
    subject = f"🔔 {signal if len(signal) <= 60 else signal[:59] + '…'}: {describe(hit)}"
    what = (f"{alert['symbol']} {moved} your level of {hit['level']:g}"
            f"{' on the ' + hit['timeframe'] + ' candle' if uses_close(hit['condition']) else ''}.")
    body = (
        f"{signal}{chr(10) if custom else '. '}{what}\n"
        f"Price: {price:g} (candle {candle.start.astimezone(IST):%-I:%M %p})\n"
        f"Time: {now:%d %b, %-I:%M %p} IST"
    )
    if len(levels) > 1:
        waiting = [describe(v).removeprefix(alert["symbol"] + " ") for _, v in open_views(alert)]
        body += f"\nStill watching: {', '.join(waiting)}" if waiting else "\nThat was the last level on this alert."
    results = notify.send(alert["user"], alert.get("channels", []), subject, body)
    if alert.get("webhooks"):
        results["webhook"] = webhooks.send(alert["webhooks"], webhook_body(alert, hit, signal, what, price, candle, now))
    store.put("events", new_id(), {
        "user": alert["user"], "alert_id": alert["id"], "symbol": alert["symbol"], "key": alert_key(alert),
        "summary": describe(hit), "price": price, "level": hit["level"], "signal": "sell" if up else "buy",
        "at": now.isoformat(), "delivery": results,
    })
    log.info("fired %s for %s: %s", alert["id"], alert["user"], results)
    return alert


def webhook_body(alert: dict, hit: dict, message: str, text: str, price: float, candle: Candle | None,
                 now: datetime, test: bool = False) -> dict:
    """What is POSTed to an alert's webhooks. `hit` is the level view that fired, with its direction resolved."""
    return {
        "event": "level_hit",
        "test": test,
        "alert_id": alert["id"],
        "symbol": alert["symbol"],
        "exchange": alert.get("exchange") or "NSE",
        "name": alert.get("name", ""),
        "message": message,
        "text": text,
        "condition": hit["condition"],
        "direction": "above" if hit["condition"] in ("high_above", "close_above") else "below",
        "level": hit["level"],
        "price": price,
        "timeframe": hit["timeframe"] if uses_close(hit["condition"]) else None,
        "candle": candle.start.isoformat() if candle else None,
        "time": now.isoformat(),
        "still_watching": [{"condition": v["condition"], "level": v["level"]} for _, v in open_views(alert)],
        "payload": json.loads(alert["webhook_payload"]) if alert.get("webhook_payload") else None,
    }


# ---- fractal alerts ---------------------------------------------------------------

FRACTAL_OUTCOME = {"touch": "taken", "reject": "swept", "confirm": "sweep held", "fail": "break failed"}


def is_fractal(alert: dict) -> bool:
    return alert.get("kind") == "fractal"


def fractal_candles(client, alert: dict, s: MarketSettings, now: datetime) -> list[Candle]:
    """Completed candles of the alert's timeframe, for as many sessions as fractals are searched over."""
    tf = alert["timeframe"]
    sessions = fractals.TIMEFRAMES[tf][1]
    history = client.candles_range(alert["token"], tf, now.date() - timedelta(days=int(sessions * 1.5) + 7), now.date())
    return fractals.last_sessions([c for c in history if candle_end(c, tf, s) <= now - SETTLE], sessions)


def trigger_timeframe(alert: dict) -> str:
    """The candle size sweeps and failed breaks are judged on: the alert's own choice, else the fractal timeframe."""
    return alert.get("confirm_timeframe") or alert["timeframe"]


def fractal_trigger_candles(client, alert: dict, since: date, s: MarketSettings, now: datetime) -> list[Candle]:
    """Completed trigger candles from `since` (or as far back as Kite serves that candle size in one request)."""
    tf = trigger_timeframe(alert)
    start = max(since, now.date() - timedelta(days=fractals.MAX_DAYS[tf] - 1))
    return [c for c in client.candles_range(alert["token"], tf, start, now.date()) if candle_end(c, tf, s) <= now - SETTLE]


def fractal_stream(alert: dict, history: list[Candle], trigger_candles: list[Candle] | None,
                   s: MarketSettings) -> tuple[list, list[Candle], int]:
    """(fractals with the moment each came into being, candles to replay past them, index where the
    trigger candles start). Before the trigger candles begin, the fractal timeframe's own candles
    stand in, which is enough to know what was already mitigated."""
    tf = alert["timeframe"]
    found = fractals.find(history, lambda c: candle_end(c, tf, s))
    if trigger_candles is None:  # same candle size for both
        return found, history, 0
    if not trigger_candles:
        return found, history, len(history)
    earlier = [c for c in history if candle_end(c, tf, s) <= trigger_candles[0].start]
    return found, earlier + trigger_candles, len(earlier)


def min_between(alert: dict) -> int:
    return int(alert.get("min_candles", fractals.DEFAULT_MIN_BETWEEN))


def fractal_run(alert: dict, history: list[Candle], trigger_candles: list[Candle] | None, s: MarketSettings):
    """Replay an alert's candles. Returns (hits, unmitigated fractals, the candles replayed, index where
    the trigger candles start, closing times of the fractal candles)."""
    found, stream, first = fractal_stream(alert, history, trigger_candles, s)
    closes = [candle_end(c, alert["timeframe"], s) for c in history]
    hits, unmitigated = fractals.replay(found, stream, closes, min_between(alert))
    return hits, unmitigated, stream, first, closes


def _cached(what: str, client, username: str, alert: dict, tf: str, s: MarketSettings, now: datetime, fetch) -> list[Candle]:
    """Candles only change when one of that size completes, so fetch once per candle."""
    key = (username, alert["token"], tf, what)
    fetched_for = (now.date(), last_boundary(tf, s, now - SETTLE))
    if key not in _fractal_history or _fractal_history[key][0] != fetched_for:
        _fractal_history[key] = (fetched_for, fetch())
    return _fractal_history[key][1]


def fractal_wanted(alert: dict, hit: fractals.Hit) -> bool:
    return alert.get("sides", "both") in ("both", hit.fractal.side) and hit.trigger in alert.get("triggers", [])


def fractal_text(alert: dict, hit: fractals.Hit) -> tuple[str, str, str]:
    """(what happened, when the fractal formed, the target) in words, for messages and backtests."""
    f, tf = hit.fractal, alert["timeframe"]
    name = f"{fractals.TIMEFRAMES[tf][0].lower()} fractal {f.side} of {f.level:g}"
    if f.flipped:  # price gapped through it earlier, so it is being met from the other side
        name += f" (now {f.role} after a gap)"
    beyond, back = ("above", "below") if f.role == "resistance" else ("below", "above")
    candle = f"{fractals.label(trigger_timeframe(alert)).lower()} candle"  # the candle size the close is judged on
    what = {
        "touch": f"{alert['symbol']} traded {beyond} the {name}.",
        "reject": f"{alert['symbol']} swept the {name} and the {candle} closed back {back} it at {hit.price:g}.",
        "confirm": (f"{alert['symbol']} swept the {name} and the {candle} closed back {back} it; "
                    f"the next one closed {back} it too, at {hit.price:g}."),
        "fail": (f"{alert['symbol']} closed a {candle} {beyond} the {name}, "
                 f"then the next two closed back {back} it, the second at {hit.price:g}."),
    }[hit.trigger]
    formed = "Fractal formed " + f.at.astimezone(IST).strftime("%d %b" if tf == "1d" else "%d %b, %-I:%M %p")
    target = (f"Target: {hit.target.level:g}, the nearest unmitigated fractal {hit.target.side}"
              f"{' (now ' + hit.target.role + ')' if hit.target.flipped else ''}" if hit.target
              else f"Target: none, there is no unmitigated fractal {back} price to aim at")
    return what, formed, target


def fractal_webhook_body(alert: dict, hit: fractals.Hit, message: str, text: str, price: float, now: datetime,
                         test: bool = False) -> dict:
    f = hit.fractal
    return {
        "event": "fractal_hit",
        "test": test,
        "alert_id": alert["id"],
        "symbol": alert["symbol"],
        "exchange": alert.get("exchange") or "NSE",
        "name": alert.get("name", ""),
        "message": message,
        "text": text,
        "side": f.side,
        "role": f.role,
        "flipped": f.flipped,
        "trigger": hit.trigger,
        "signal": hit.signal,
        "level": f.level,
        "price": price,
        "timeframe": alert["timeframe"],
        "trigger_timeframe": trigger_timeframe(alert),
        "min_candles": min_between(alert),
        "fractal_time": f.at.isoformat(),
        "target": hit.target.level if hit.target else None,
        "candle": hit.candle.start.isoformat(),
        "time": now.isoformat(),
        "payload": json.loads(alert["webhook_payload"]) if alert.get("webhook_payload") else None,
    }


def fire_fractal(alert: dict, hit: fractals.Hit, price: float, now: datetime) -> dict:
    """Tell the user about one fractal hit and remember it so it isn't reported twice."""
    what, formed, target = fractal_text(alert, hit)
    custom = (alert.get("note") or "").strip()
    signal = custom or f"Potential {hit.signal}"
    f = hit.fractal
    short = f"{alert['symbol']} {fractals.TIMEFRAMES[alert['timeframe']][0].lower()} fractal {f.side} {f.level:g}"
    summary = f"{short} {FRACTAL_OUTCOME[hit.trigger]}" + (f" as {f.role}" if f.flipped else "")
    subject = f"🔔 {signal if len(signal) <= 60 else signal[:59] + '…'}: {summary}"
    body = (
        f"{signal}{chr(10) if custom else '. '}{what}\n"
        f"{target}.\n"
        f"Price: {price:g} (candle {hit.candle.start.astimezone(IST):%-I:%M %p})\n"
        f"{formed}.\n"
        f"Time: {now:%d %b, %-I:%M %p} IST"
    )
    changes = {"fired": (alert.get("fired", []) + [hit.key])[-300:],
               "last_hit": {"at": now.isoformat(), "text": summary, "price": price, "signal": hit.signal}}
    store.update("alerts", alert["id"], changes)
    results = notify.send(alert["user"], alert.get("channels", []), subject, body)
    if alert.get("webhooks"):
        results["webhook"] = webhooks.send(alert["webhooks"], fractal_webhook_body(alert, hit, signal, what, price, now))
    store.put("events", new_id(), {
        "user": alert["user"], "alert_id": alert["id"], "symbol": alert["symbol"], "key": alert_key(alert),
        "summary": summary, "price": price, "level": f.level, "signal": hit.signal,
        "at": now.isoformat(), "delivery": results,
    })
    log.info("fractal %s for %s: %s %s", alert["id"], alert["user"], hit.key, results)
    return {**alert, **changes}


def _scan_fractal(username: str, alert: dict, client, cache: dict, s: MarketSettings, now: datetime) -> None:
    tf, trigger_tf = alert["timeframe"], trigger_timeframe(alert)
    history = _cached("history", client, username, alert, tf, s, now, lambda: fractal_candles(client, alert, s, now))
    key = (alert["token"], "1m")
    if key not in cache:
        cache[key] = client.candles(alert["token"], "1m", now.date())
    minutes = cache[key]
    if minutes:
        last_prices[(username, alert_key(alert))] = (minutes[-1].close, now)

    # Today's completed trigger candles, when they aren't the fractal timeframe's own.
    if trigger_tf == tf:
        today = None
    elif trigger_tf == "1m":
        today = [m for m in minutes if candle_end(m, "1m", s) <= now - SETTLE]
    else:
        today = _cached("today", client, username, alert, trigger_tf, s, now,
                        lambda: fractal_trigger_candles(client, alert, now.date(), s, now))
    hits, unmitigated, stream, first, closes = fractal_run(alert, history, today, s)
    armed_at = datetime.fromisoformat(alert["armed_at"])
    fired = set(alert.get("fired", []))
    to_fire: list[tuple[fractals.Hit, float]] = []

    # What today's completed trigger candles decided: sweeps, failed breaks, and any touch not caught live.
    for hit in hits:
        end = candle_end(hit.candle, trigger_tf, s)
        if (hit.index >= first and end.date() == now.date() and end > armed_at
                and fractal_wanted(alert, hit) and hit.key not in fired):
            traded = hit.candle.high if hit.signal == "sell" else hit.candle.low
            to_fire.append((hit, traded if hit.trigger == "touch" else hit.price))
            fired.add(hit.key)

    # Touches are caught as they happen, on the minutes of the trigger candle that is still forming.
    done_today = [c for c in stream[first:] if c.start.date() == now.date()]
    forming_from = candle_end(done_today[-1], trigger_tf, s) if done_today and trigger_tf != "1d" else s.open_at(now)
    forming = [m for m in minutes if m.start >= forming_from]
    touched, touches, live = [], [], []
    for f in unmitigated:
        if forming and f.is_beyond(forming[0].open):
            f = f.flip()  # the forming candle opened beyond it: a gap, so it now plays the other role
        live.append(f)
        first_beyond = next((m for m in forming if f.traded_beyond(m)), None)
        if not first_beyond:
            continue
        touched.append(f)
        too_soon = fractals.candles_between(f, first_beyond.start, closes) < min_between(alert)
        hit = fractals.Hit(f, "touch", first_beyond, -1, f.level)
        if (not too_soon and first_beyond.start >= armed_at.replace(second=0, microsecond=0)
                and fractal_wanted(alert, hit) and hit.key not in fired):
            touches.append((hit, first_beyond.high if hit.signal == "sell" else first_beyond.low))
    remaining = [f for f in live if f not in touched]
    for hit, _ in touches:
        hit.target = fractals.target_for(hit.signal, hit.price, remaining)
    fractal_levels[alert["id"]] = {
        "resistance": sorted((f for f in remaining if f.role == "resistance"), key=lambda f: f.level),
        "support": sorted((f for f in remaining if f.role == "support"), key=lambda f: f.level, reverse=True),
    }
    last_checked_at[alert["id"]] = now
    for hit, price in to_fire + touches:
        alert = fire_fractal(alert, hit, price, now)


# ---- schedule info for the UI ----------------------------------------------------

def next_tick() -> datetime | None:
    """When the scheduler will next run a scan (None if the scanner isn't running)."""
    job = scheduler.get_job("scan") if scheduler.running else None
    return job.next_run_time.astimezone(IST) if job and job.next_run_time else None


def next_open(s: MarketSettings, now: datetime) -> datetime:
    """Start of the next scan window (today's open if it's still ahead)."""
    day = now
    if is_trading_day(day) and now < s.open_at(day):
        return s.open_at(day)
    day += timedelta(days=1)
    while not is_trading_day(day):
        day += timedelta(days=1)
    return s.open_at(day)


def next_check(alert: dict, s: MarketSettings, now: datetime, tick: datetime | None) -> dict:
    """When this alert will next be looked at, for display.
    Returns {"at": datetime | None, "after_candle": bool}."""
    # Any trades level means the alert is looked at on every scan.
    views = open_views(alert)
    close_rule = bool(views) and all(uses_close(v["condition"]) for _, v in views)
    minutes = TIMEFRAMES[alert["timeframe"]][1] if close_rule else None
    if not in_scan_window(s, now):
        opens = next_open(s, now)
        if not close_rule:
            return {"at": opens, "after_candle": False}
        first = s.close_at(opens) if minutes is None else min(opens + timedelta(minutes=minutes), s.close_at(opens))
        return {"at": first, "after_candle": True}
    if not close_rule:
        return {"at": tick, "after_candle": False}
    last = last_boundary(alert["timeframe"], s, now - SETTLE) or s.open_at(now)
    if minutes is None:
        nxt = s.close_at(now)
    else:
        nxt = min(last + timedelta(minutes=minutes), s.close_at(now))
        if nxt <= now - SETTLE:  # today's last candle already checked
            return next_check(alert, s, s.close_at(now) + CLOSE_GRACE, tick)
    return {"at": nxt, "after_candle": True}


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
    views = [level_view(alert, lv) for lv in levels_of(alert)]
    timeframes = list(dict.fromkeys(data_timeframe(v) for v in views))
    for _ in range(7):
        candles = client.candles(alert["token"], timeframes[0], day.date())
        if candles:
            break
        day -= timedelta(days=1)
        while not is_trading_day(day):
            day -= timedelta(days=1)
    else:
        return None
    data = {timeframes[0]: candles}
    for tf in timeframes[1:]:
        data[tf] = client.candles(alert["token"], tf, day.date())
    armed, end = s.open_at(day).isoformat(), s.close_at(day) + CLOSE_GRACE
    levels = [{"view": v, "hit": evaluate({**v, "armed_at": armed, "armed_price": None}, data[data_timeframe(v)], s, end)}
              for v in views]
    hits = [lv["hit"] for lv in levels if lv["hit"]]
    return {
        "day": s.open_at(day),
        "levels": levels,
        "hit": min(hits, key=lambda h: h[0].start) if hits else None,  # the first level to fire
        "high": max(c.high for c in candles),
        "low": min(c.low for c in candles),
        "close": candles[-1].close,
    }


def simulation_message(alert: dict, result: dict) -> tuple[str, str]:
    day = f"{result['day']:%a %-d %b}"
    levels = result["levels"]
    fired = [lv for lv in levels if lv["hit"]]
    if len(levels) > 1:
        subject = f"🧪 Simulation: {alert['symbol']}, {len(fired)} of {len(levels)} levels would have fired"
        lines = [
            f"{describe(lv['view'])}: " + (f"fired at {lv['hit'][0].start.astimezone(IST):%-I:%M %p}, "
                                           f"price {lv['hit'][1]:,.2f}" if lv["hit"] else "not met")
            for lv in levels
        ]
        body = (f"On {day}:\n" + "\n".join(lines)
                + f"\nDay's range {result['low']:,.2f} to {result['high']:,.2f}, close {result['close']:,.2f}.")
    elif fired:
        candle, price = fired[0]["hit"]
        subject = f"🧪 Simulation: {describe(resolved(levels[0]['view'], price))}"
        body = (f"On {day} this alert would have fired at {candle.start.astimezone(IST):%-I:%M %p}, "
                f"price {price:,.2f}.")
    else:
        subject = f"🧪 Simulation: {alert['symbol']} would not have fired"
        body = (f"On {day}, {describe(levels[0]['view'])} was not met. "
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
        if is_fractal(alert):
            try:
                _scan_fractal(username, alert, client, cache, s, now)
            except KiteAuthError as e:
                session_notice(username, "expired", str(e), len(alerts), now)
                return
            except KiteError as e:
                log.warning("fractal scan failed for %s: %s", alert["symbol"], e)
            continue
        hits, close_checked = [], False
        for index, view in open_views(alert):
            if not _due(view, s, now):
                continue
            tf = data_timeframe(view)
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
                last_prices[(username, alert_key(alert))] = (candles[-1].close, now)
            close_checked = close_checked or uses_close(view["condition"])
            last_checked_at[alert["id"]] = now
            if hit := evaluate(view, candles, s, now):
                hits.append((index, *hit))
        if close_checked:
            _mark_checked(alert, s, now)
        # One message per level, even when a jump in price takes out several in the same scan.
        for index, candle, price in hits:
            alert = fire(alert, index, candle, price, now)


def _retire_expired(alert: dict, now: datetime) -> bool:
    """Futures and options stop trading on expiry day; pause the alert rather than poll a dead contract."""
    if alert.get("expiry") and alert["expiry"] < now.date().isoformat():
        store.update("alerts", alert["id"], {"status": "paused"})
        log.info("paused %s: %s expired on %s", alert["id"], alert["symbol"], alert["expiry"])
        return True
    return False


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
            if not _retire_expired(a, now):
                by_user[a["user"]].append(a)
        for username, alerts in by_user.items():
            try:
                _scan_user(username, alerts, s, now)
            except Exception:
                log.exception("scan failed for %s", username)
        last_run.update(at=now, alerts=sum(len(a) for a in by_user.values()))
        try:
            oi.capture_if_due(now, s)  # Nifty OI snapshots ride on the same tick
        except Exception:
            log.exception("OI capture failed")
    finally:
        _scan_lock.release()


def session_restored(username: str) -> None:
    """Called after a successful login so today's scans resume immediately."""
    _session_ok_on[username] = now_ist().date()


# ---- scheduler + keep-alive ----------------------------------------------------

# One worker thread runs every scan, whatever the number of alerts (max_instances=1 below).
scheduler = BackgroundScheduler(timezone=IST, executors={"default": ThreadPoolExecutor(max_workers=1)})
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
