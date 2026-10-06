"""Fractal bias: what the Nifty 50 stocks' fractal sweeps say about the market, through the day.

Every 5 minutes, for each Nifty 50 stock, fractals are found on 30-minute candles (10 sessions,
as fractal alerts do) and today's 5-minute candles (15-minute is a setting) are run past them:

* a fractal low swept (or broken and failed) is a potential buy, a fractal high a potential sell;
  which of sweep / sweep holds / break fails counts is a setting (sweep holds and break fails by default);
* each signal is then followed: it *holds* while price stays on the right side of its stop (the
  extreme of the candles that made it), reaches its *target* (the next fractal the other way), or is
  *stopped* when price trades through the stop and carries on.

Buys that hold and sells that are stopped out lean bullish; sells that hold and buys that are stopped
out lean bearish. The reading is (bullish - bearish) / (bullish + bearish) over everything since the
open, so it is cumulative: each snapshot recounts the day, and signals stopped out since the last
one change sides. Snapshots are saved (bias_snapshots, with a summary per day in bias_days that also
holds each count's totals for the chart) so any moment can be looked at again.

Notifications, per person (bias_alerts/{username}), to Telegram and/or their own webhooks:
* bias changes: looked at every 15 minutes (9:30, 9:45, ...), so the label isn't flipping every
  5 minutes; the first of the day and every change of label after it;
* new signals: every 5-minute check, each signal that wasn't in the previous count.
"""

import csv
import io
import json
import logging
from datetime import date, datetime, timedelta

import httpx

from app import brokers, config, fractals, notify, oi, webhooks
from app.kite import Candle, KiteAuthError, KiteError, instruments
from app.market import IST, MarketSettings, in_scan_window, load_settings as market_settings
from app.store import store

log = logging.getLogger("bias")

FRACTAL_TF = "30m"
TRIGGER_TFS = ["5m", "15m"]  # the candles sweeps are judged on
SESSIONS = fractals.TIMEFRAMES[FRACTAL_TF][1]
TRIGGER_CHOICES = ["reject", "confirm", "fail"]
DEFAULTS = {"triggers": ["confirm", "fail"], "min_candles": fractals.DEFAULT_MIN_BETWEEN, "trigger_tf": "5m"}
EVERY = 5  # minutes between counts
BIAS_EVERY = 15  # minutes between looks at the bias label
TRIGGER_NAMES = {"reject": "Sweep", "confirm": "Sweep holds", "fail": "Break fails"}
SETTLE = timedelta(seconds=10)  # let Kite finish the candle that just closed
NIFTY50_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"
# NSE's list as of October 2026, used when the live one can't be had.
NIFTY50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BSE", "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV",
    "BEL", "BHARTIARTL", "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH", "HDFCBANK",
    "HDFCLIFE", "HINDALCO", "HINDUNILVR", "ICICIBANK", "ITC", "INFY", "INDIGO", "JSWSTEEL", "JIOFIN", "KOTAKBANK", "LT",
    "M&M", "MARUTI", "MAXHEALTH", "NTPC", "NESTLEIND", "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SHRIRAMFIN", "SBIN",
    "SUNPHARMA", "TCS", "TATACONSUM", "TMPV", "TATASTEEL", "TECHM", "TITAN", "TRENT", "ULTRACEMCO",
]


def load_settings() -> dict:
    return {**DEFAULTS, **(store.get("settings", "bias") or {})}


def save_settings(triggers: list[str], min_candles: int, trigger_tf: str = "5m") -> None:
    store.update("settings", "bias", {"triggers": triggers, "min_candles": min_candles, "trigger_tf": trigger_tf})


def stocks(today: date) -> list[str]:
    """Today's Nifty 50, from NSE once a day; the last list fetched (or the built-in one) if NSE won't answer."""
    saved = store.get("settings", "nifty50") or {}
    if saved.get("date") == today.isoformat():
        return saved["symbols"]
    try:
        r = httpx.get(NIFTY50_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        symbols = [row["Symbol"].strip() for row in csv.DictReader(io.StringIO(r.text)) if row.get("Symbol")]
        if len(symbols) < 45:
            raise ValueError(f"only {len(symbols)} symbols")
    except (httpx.HTTPError, ValueError, KeyError) as e:
        log.warning("Nifty 50 list from NSE failed (%s); using the last one", e)
        return saved.get("symbols") or NIFTY50
    store.put("settings", "nifty50", {"date": today.isoformat(), "symbols": symbols})
    return symbols


# ---- the signals of one stock ------------------------------------------------------

def _end(c: Candle, minutes: int, s: MarketSettings) -> datetime:
    return min(c.start + timedelta(minutes=minutes), s.close_at(c.start.astimezone(IST)))


def to_30m(candles: list[Candle], s: MarketSettings) -> list[Candle]:
    """Group 5- or 15-minute candles into the 30-minute ones Kite would give (from the open: 9:15, 9:45, ...)."""
    out: list[Candle] = []
    key = None
    for c in candles:
        start = c.start.astimezone(IST)
        open_at = s.open_at(start)
        k = (start.date(), int((start - open_at).total_seconds() // 1800))
        if k == key:
            last = out[-1]
            out[-1] = Candle(last.start, last.open, max(last.high, c.high), min(last.low, c.low), c.close)
        else:
            out.append(Candle(open_at + timedelta(minutes=30 * k[1]), c.open, c.high, c.low, c.close))
            key = k
    return out


def signals(symbol: str, candles: list[Candle], day: date, s: MarketSettings, cfg: dict, now: datetime) -> list[dict]:
    """Today's signals for one stock from completed trigger candles (oldest first, several sessions),
    each with how it has gone since."""
    m = fractals.minutes(cfg.get("trigger_tf", "15m"))
    candles = [c for c in candles if _end(c, m, s) <= now - SETTLE]
    history = [c for c in to_30m(candles, s) if _end(c, 30, s) <= now - SETTLE]
    found = fractals.find(history, lambda c: _end(c, 30, s))
    closes = [_end(c, 30, s) for c in history]
    hits, _ = fractals.replay(found, candles, closes, int(cfg["min_candles"]))
    out, seen = [], set()
    for hit in hits:
        if hit.trigger not in cfg["triggers"] or hit.candle.start.astimezone(IST).date() != day or hit.fractal.key in seen:
            continue
        seen.add(hit.fractal.key)  # one signal per fractal, from the first way it qualified
        o = fractals.outcome(hit, candles)
        status = {"stop": "stopped", "target": "target"}.get(o.result, "held")
        out.append({
            "key": f"{symbol}:{hit.key}", "symbol": symbol, "signal": hit.signal, "trigger": hit.trigger,
            "side": hit.fractal.side, "flipped": hit.fractal.flipped, "level": hit.fractal.level,
            "at": _end(hit.candle, m, s).isoformat(), "price": hit.price, "stop": o.stop,
            "target": o.target, "status": status,
            # when the stop or target was traded: the close of that candle, like "at"
            "until": _end(o.stopped if status == "stopped" else o.reached, m, s).isoformat() if status != "held" else None,
        })
    return out


def leaning(sig: dict) -> str:
    """Which way a signal pushes the reading: a buy that holds is bullish, one that is stopped bearish."""
    holds = sig["status"] != "stopped"
    return "bull" if (sig["signal"] == "buy") == holds else "bear"


def read(sigs: list[dict]) -> dict:
    count = {f"{side}_{state}": 0 for side in ("buy", "sell") for state in ("held", "target", "stopped")}
    for g in sigs:
        count[f"{g['signal']}_{g['status']}"] += 1
    bull = sum(1 for g in sigs if leaning(g) == "bull")
    bear = len(sigs) - bull
    reasons = []
    if len(sigs) < 3:
        score, label = None, "Neutral"
        reasons.append("Too few fractal signals across the Nifty 50 so far to read anything into.")
    else:
        score = (bull - bear) / len(sigs)
        label = ("Bullish" if score >= 0.35 else "Mildly bullish" if score >= 0.12 else
                 "Bearish" if score <= -0.35 else "Mildly bearish" if score <= -0.12 else "Neutral")
    buys = count["buy_held"] + count["buy_target"] + count["buy_stopped"]
    sells = count["sell_held"] + count["sell_target"] + count["sell_stopped"]
    if buys:
        reasons.append(f"{buys} potential buy{'s' if buys != 1 else ''} from fractal lows: {count['buy_held'] + count['buy_target']} holding"
                       f"{' (' + str(count['buy_target']) + ' at target)' if count['buy_target'] else ''}, {count['buy_stopped']} stopped out.")
    if sells:
        reasons.append(f"{sells} potential sell{'s' if sells != 1 else ''} from fractal highs: {count['sell_held'] + count['sell_target']} holding"
                       f"{' (' + str(count['sell_target']) + ' at target)' if count['sell_target'] else ''}, {count['sell_stopped']} stopped out.")
    if sigs:
        reasons.append(f"{bull} lean bullish (buys holding, sells stopped) against {bear} bearish.")
    return {"label": label, "tone": "up" if "ullish" in label else "down" if "earish" in label else "flat",
            "score": score, "bull": bull, "bear": bear, "buys": buys, "sells": sells, "count": count,
            "stocks": len({g["symbol"] for g in sigs}), "reasons": reasons}


# ---- snapshots -----------------------------------------------------------------------

_past: dict[tuple, list[Candle]] = {}  # (token, candle size, day) -> candles before that day


def _candles(client, token: int, tf: str, day: date) -> list[Candle]:
    """Earlier sessions are fetched once a day; each count then asks Kite only for today's candles."""
    key = (token, tf, day)
    if key not in _past:
        for k in [k for k in _past if k[2] != day]:
            del _past[k]
        start = day - timedelta(days=int(SESSIONS * 1.5) + 7)
        _past[key] = [c for c in client.candles_range(token, tf, start, day - timedelta(days=1))
                      if c.start.astimezone(IST).date() < day]
    today = [c for c in client.candles_range(token, tf, day, day) if c.start.astimezone(IST).date() == day]
    return _past[key] + today


def capture(client, now: datetime, slot_id: str | None = None) -> dict:
    """Recount the day across the Nifty 50 and save it. Raises KiteAuthError if the session is dead."""
    s, cfg = market_settings(), load_settings()
    known = instruments()
    day = now.astimezone(IST).date()
    sigs, missing, failed = [], [], []
    for symbol in stocks(day):
        inst = known.get(symbol)
        if not inst:
            missing.append(symbol)
            continue
        try:
            candles = _candles(client, inst.token, cfg["trigger_tf"], day)
        except KiteAuthError:
            raise
        except KiteError as e:
            log.warning("bias: %s candles failed: %s", symbol, e)
            failed.append(symbol)
            continue
        sigs += signals(symbol, fractals.last_sessions(candles, SESSIONS), day, s, cfg, now)
    sigs.sort(key=lambda g: (g["at"], g["symbol"]))
    r = read(sigs)
    snap = {
        "id": slot_id or f"{day.isoformat()}T{now.astimezone(IST):%H:%M}", "date": day.isoformat(), "at": now.isoformat(),
        "label": r["label"], "score": r["score"], "bull": r["bull"], "bear": r["bear"],
        "signals": sigs, "missing": missing, "failed": failed, "settings": cfg,
    }
    store.put("bias_snapshots", snap["id"], snap)
    summary = store.get("bias_days", snap["date"]) or {}
    slots = sorted(set(summary.get("slots", [])) | {snap["id"]})
    # Each count's totals live in the day's summary too, so the page and the chart needn't read every count.
    points = {**summary.get("points", {}), snap["id"]: {"bull": r["bull"], "bear": r["bear"], "label": r["label"], "at": snap["at"]}}
    store.update("bias_days", snap["date"], {"date": snap["date"], "count": len(slots), "slots": slots, "last_at": snap["at"],
                                             "points": points})
    return snap


def slot(now: datetime, s: MarketSettings) -> str | None:
    """The latest 5-minute candle close that has settled, as a snapshot id: 2026-10-06T09:20."""
    now = now.astimezone(IST)
    at = now - SETTLE
    open_at, close_at = s.open_at(now), s.close_at(now)
    if at < open_at + timedelta(minutes=EVERY):
        return None
    k = int((min(at, close_at) - open_at).total_seconds() // (EVERY * 60))
    return f"{now.date().isoformat()}T{min(open_at + timedelta(minutes=EVERY * k), close_at):%H:%M}"


def bias_mark(slot_id: str, s: MarketSettings) -> bool:
    """Is this count one of the 15-minute marks (9:30, 9:45, ... and the close) where the bias label is looked at?"""
    at = datetime.strptime(slot_id[11:16], "%H:%M")
    open_m, close_m = s.open.hour * 60 + s.open.minute, s.close.hour * 60 + s.close.minute
    m = at.hour * 60 + at.minute
    return m > open_m and ((m - open_m) % BIAS_EVERY == 0 or m == close_m)


_done: set[str] = set()


def _client():
    for u in store.list("users"):
        if u.get("active", True) and (u.get("role") == "superadmin" or "bias" in u.get("modules", [])):
            doc = brokers.load(u["username"])
            if doc.get("status") == "connected":
                try:
                    return brokers.client_for(u["username"], doc)
                except KiteError:
                    continue
    return None


def capture_if_due(now: datetime) -> dict | None:
    """Run by the scheduler every minute: take the snapshot for the 5-minute candle that just closed."""
    s = market_settings()
    if not config.BIAS_CAPTURE or not in_scan_window(s, now):
        return None
    slot_id = slot(now, s)
    if not slot_id or slot_id in _done or store.get("bias_snapshots", slot_id):
        if slot_id:
            _done.add(slot_id)
        return None
    client = _client()
    if not client:
        return None
    try:
        snap = capture(client, now, slot_id)
    except KiteError as e:
        log.warning("bias snapshot %s failed: %s", slot_id, e)
        return None
    _done.add(slot_id)
    log.info("bias %s: %s (%d signals)", slot_id, snap["label"], len(snap["signals"]))
    notify_all(snap)
    return snap


# ---- reading back ------------------------------------------------------------------------

def day_snapshots(day: str) -> list[dict]:
    return sorted(store.list("bias_snapshots", date=day), key=lambda s: s["id"])


def series(day_doc: dict) -> list[dict]:
    """One chart point per count of a day, from the day's summary."""
    out = []
    for sid, p in sorted((day_doc.get("points") or {}).items()):
        hh, mm = int(sid[11:13]), int(sid[14:16])
        at = datetime.fromisoformat(day_doc["date"]).replace(hour=hh, minute=mm, tzinfo=IST)
        out.append({"time": int(at.timestamp()) + 19800, "slot": sid, "bull": p["bull"], "bear": p["bear"], "label": p["label"]})
    return out


def slot_time(slot_id: str) -> str:
    return datetime.strptime(slot_id[11:16], "%H:%M").strftime("%-I:%M %p")


def can_see(username: str) -> bool:
    user = store.get("users", username) or {}
    return user.get("active", True) and (user.get("role") == "superadmin" or "bias" in user.get("modules", []))


def sentiment(username: str, now: datetime) -> dict | None:
    """The latest count today, for other messages to carry. None before the first count, or if the
    user can't see the Fractal bias tab."""
    if not can_see(username):
        return None
    now = now.astimezone(IST)
    day = store.get("bias_days", now.date().isoformat()) or {}
    done = [sid for sid, p in sorted((day.get("points") or {}).items()) if datetime.fromisoformat(p["at"]) <= now]
    snap = store.get("bias_snapshots", done[-1]) if done else None
    if not snap:
        return None
    r = read(snap["signals"])
    return {"label": r["label"], "tone": r["tone"], "bull": r["bull"], "bear": r["bear"], "buys": r["buys"], "sells": r["sells"],
            "stocks": r["stocks"], "count": r["count"], "as_of": slot_time(snap["id"]), "day": snap["date"], "slot": snap["id"]}


def sentiment_line(reading: dict | None) -> str:
    """'Fractal bias: Mildly bullish · 10 leaning bullish, 6 bearish (11:30 AM)'."""
    if not reading:
        return ""
    return f"Fractal bias: {reading['label']} · {reading['bull']} leaning bullish, {reading['bear']} bearish ({reading['as_of']})"


def previous(snap: dict) -> dict | None:
    """The count before this one on the same day."""
    slots = sorted((store.get("bias_days", snap["date"]) or {}).get("slots", []))
    earlier = [sid for sid in slots if sid < snap["id"]]
    return store.get("bias_snapshots", earlier[-1]) if earlier else None


def new_signals(snap: dict, before: dict | None) -> list[dict]:
    known = {g["key"] for g in (before or {}).get("signals", [])}
    return [g for g in snap["signals"] if g["key"] not in known]


# ---- notifications ---------------------------------------------------------------------------

def prefs(username: str) -> dict:
    """What someone wants to hear about and where. Older records only had telegram (bias changes on Telegram)."""
    doc = store.get("bias_alerts", username) or {}
    if "changes" not in doc and doc.get("telegram"):
        doc = {**doc, "changes": True}
    return {"changes": bool(doc.get("changes")), "signals": bool(doc.get("signals")), "telegram": bool(doc.get("telegram")),
            "webhooks": doc.get("webhooks", []), "webhook_payload": doc.get("webhook_payload", "")}


def save_prefs(username: str, p: dict) -> None:
    store.put("bias_alerts", username, p)


def wants_telegram(username: str) -> bool:
    p = prefs(username)
    return p["telegram"] and (p["changes"] or p["signals"])


def set_telegram(username: str, on: bool) -> None:
    """Bias changes on Telegram, on or off (what the old switch did)."""
    save_prefs(username, {**prefs(username), "telegram": on, "changes": on or prefs(username)["changes"]})


def add_line(body: str, line: str) -> str:
    """Put the other reading just above the link at the end of a message."""
    if not line:
        return body
    head, _, link = body.rpartition("\n")
    return f"{head}\n{line}\n{link}"


def message(snap: dict, was: str | None) -> tuple[str, str]:
    r = read(snap["signals"])
    at = slot_time(snap["id"])
    subject = f"🧭 Fractal bias turned {r['label']} (was {was}) · {at}" if was else f"🧭 Fractal bias at {at}: {r['label']}"
    lines = r["reasons"][:]
    fresh = [g for g in snap["signals"] if g["at"][11:16] == snap["id"][11:16]]
    if fresh:
        lines.append("Latest: " + ", ".join(f"{g['symbol']} {g['signal']}" for g in fresh[:8]) + (" …" if len(fresh) > 8 else ""))
    lines.append(f"{config.BASE_URL}/bias?day={snap['date']}&at={snap['id']}")
    return subject, "\n".join(lines)


def describe(g: dict) -> str:
    """'TCS potential buy: sweep holds of the 30 min fractal low 3,512.40 at 3,515.00, stop 3,508.10, target 3,540.00'"""
    target = f", target {g['target']:,.2f}" if g.get("target") is not None else ""
    return (f"{g['symbol']} potential {g['signal']}: {TRIGGER_NAMES[g['trigger']].lower()}"
            f"{' (gap flip)' if g.get('flipped') else ''} of the 30 min fractal {g['side']} {g['level']:,.2f}"
            f" at {g['price']:,.2f}, stop {g['stop']:,.2f}{target}")


def signals_message(snap: dict, fresh: list[dict]) -> tuple[str, str]:
    at = slot_time(snap["id"])
    subject = (f"🧭 {fresh[0]['symbol']} potential {fresh[0]['signal']} · {at}" if len(fresh) == 1
               else f"🧭 {len(fresh)} new fractal signals · {at}")
    r = read(snap["signals"])
    lines = [describe(g) for g in fresh[:15]] + ([f"… and {len(fresh) - 15} more"] if len(fresh) > 15 else [])
    lines.append(f"Bias now: {r['label']} ({r['bull']} leaning bullish, {r['bear']} bearish)")
    lines.append(f"{config.BASE_URL}/bias?day={snap['date']}&at={snap['id']}")
    return subject, "\n".join(lines)


def _signal_data(g: dict) -> dict:
    return {k: g.get(k) for k in ("symbol", "signal", "trigger", "side", "flipped", "level", "price", "stop", "target", "at")}


def hook_body(event: str, snap: dict, p: dict, **more) -> dict:
    r = read(snap["signals"])
    return {"event": event, "time": snap["at"], "slot": snap["id"], **more,
            "bias": {"label": r["label"], "bull": r["bull"], "bear": r["bear"], "score": r["score"]},
            "link": f"{config.BASE_URL}/bias?day={snap['date']}&at={snap['id']}",
            "payload": json.loads(p["webhook_payload"]) if p.get("webhook_payload") else None}


def _deliver(username: str, p: dict, subject: str, body: str, hook: dict) -> dict:
    out = {}
    if p["telegram"]:
        out["telegram"] = notify.send(username, ["telegram"], subject, body).get("telegram")
    if p["webhooks"]:
        out["webhook"] = webhooks.send(p["webhooks"], hook)
    return out


def notify_all(snap: dict, s: MarketSettings | None = None) -> dict:
    """After a count: new signals to those who want them; at the 15-minute marks, a change of bias label too."""
    s = s or market_settings()
    people = [(u["username"], prefs(u["username"])) for u in store.list("users") if can_see(u["username"])]
    people = [(u, p) for u, p in people if (p["telegram"] or p["webhooks"]) and (p["changes"] or p["signals"])]
    results: dict[str, dict] = {}
    fresh = new_signals(snap, previous(snap))
    if fresh:
        subject, body = signals_message(snap, fresh)
        for u, p in people:
            if p["signals"]:
                hook = hook_body("fractal_bias_signals", snap, p, signals=[_signal_data(g) for g in fresh])
                results.setdefault(u, {})["signals"] = _deliver(u, p, subject, body, hook)
    if bias_mark(snap["id"], s):
        day = store.get("bias_days", snap["date"]) or {}
        was = day.get("announced")
        if snap["label"] != was:
            store.update("bias_days", snap["date"], {"date": snap["date"], "announced": snap["label"]})
            subject, body = message(snap, was)
            at = datetime.fromisoformat(snap["at"])
            for u, p in people:
                if p["changes"]:
                    hook = hook_body("fractal_bias_change", snap, p, label=snap["label"], was=was)
                    results.setdefault(u, {})["change"] = _deliver(
                        u, p, subject, add_line(body, oi.sentiment_line(oi.sentiment(u, at))), hook)
    if results:
        log.info("bias %s notified: %s", snap["id"], results)
    return results


def announce(snap: dict) -> dict | None:
    """Kept for callers that only want the bias-change part, e.g. a count taken by hand."""
    return notify_all(snap)
