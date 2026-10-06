"""Fractal bias: what the Nifty 50 stocks' fractal sweeps say about the market, through the day.

Every 15 minutes, for each Nifty 50 stock, fractals are found on 30-minute candles (10 sessions,
as fractal alerts do) and today's 15-minute candles are run past them:

* a fractal low swept (or broken and failed) is a potential buy, a fractal high a potential sell;
  which of sweep / sweep holds / break fails counts is a setting (sweep holds and break fails by default);
* each signal is then followed: it *holds* while price stays on the right side of its stop (the
  extreme of the candles that made it), reaches its *target* (the next fractal the other way), or is
  *stopped* when price trades through the stop and carries on.

Buys that hold and sells that are stopped out lean bullish; sells that hold and buys that are stopped
out lean bearish. The reading is (bullish - bearish) / (bullish + bearish) over everything since the
open, so it is cumulative: each snapshot recounts the day, and signals stopped out since the last
one change sides. Snapshots are saved (bias_snapshots, with a summary per day in bias_days) so any
moment can be looked at again, and the reading goes out on Telegram at the first snapshot and
whenever its label changes, to whoever asked for it.
"""

import csv
import io
import logging
from datetime import date, datetime, timedelta

import httpx

from app import brokers, config, fractals, notify, oi
from app.kite import Candle, KiteAuthError, KiteError, instruments
from app.market import IST, MarketSettings, in_scan_window, load_settings as market_settings
from app.store import store

log = logging.getLogger("bias")

FRACTAL_TF = "30m"
TRIGGER_TF = "15m"
SESSIONS = fractals.TIMEFRAMES[FRACTAL_TF][1]
TRIGGER_CHOICES = ["reject", "confirm", "fail"]
DEFAULTS = {"triggers": ["confirm", "fail"], "min_candles": fractals.DEFAULT_MIN_BETWEEN}
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


def save_settings(triggers: list[str], min_candles: int) -> None:
    store.update("settings", "bias", {"triggers": triggers, "min_candles": min_candles})


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
    """Pair 15-minute candles into the 30-minute ones Kite would give (from the open: 9:15, 9:45, ...)."""
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
    """Today's signals for one stock from completed 15-minute candles (oldest first, several sessions),
    each with how it has gone since."""
    candles = [c for c in candles if _end(c, 15, s) <= now - SETTLE]
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
            "at": _end(hit.candle, 15, s).isoformat(), "price": hit.price, "stop": o.stop,
            "target": o.target, "status": status,
            # when the stop or target was traded: the close of that candle, like "at"
            "until": _end(o.stopped if status == "stopped" else o.reached, 15, s).isoformat() if status != "held" else None,
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

def capture(client, now: datetime, slot_id: str | None = None) -> dict:
    """Recount the day across the Nifty 50 and save it. Raises KiteAuthError if the session is dead."""
    s, cfg = market_settings(), load_settings()
    known = instruments()
    day = now.astimezone(IST).date()
    start = day - timedelta(days=int(SESSIONS * 1.5) + 7)
    sigs, missing, failed = [], [], []
    for symbol in stocks(day):
        inst = known.get(symbol)
        if not inst:
            missing.append(symbol)
            continue
        try:
            candles = client.candles_range(inst.token, TRIGGER_TF, start, day)
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
    store.update("bias_days", snap["date"], {"date": snap["date"], "count": len(slots), "slots": slots, "last_at": snap["at"]})
    return snap


def slot(now: datetime, s: MarketSettings) -> str | None:
    """The latest 15-minute candle close that has settled, as a snapshot id: 2026-10-06T09:30."""
    now = now.astimezone(IST)
    at = now - SETTLE
    open_at, close_at = s.open_at(now), s.close_at(now)
    if at < open_at + timedelta(minutes=15):
        return None
    k = int((min(at, close_at) - open_at).total_seconds() // 900)
    return f"{now.date().isoformat()}T{min(open_at + timedelta(minutes=15 * k), close_at):%H:%M}"


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
    """Run by the scheduler every minute: take the snapshot for the 15-minute candle that just closed."""
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
    announce(snap)
    return snap


# ---- reading back ------------------------------------------------------------------------

def day_snapshots(day: str) -> list[dict]:
    return sorted(store.list("bias_snapshots", date=day), key=lambda s: s["id"])


def series(snaps: list[dict]) -> list[dict]:
    out = []
    for snap in snaps:
        hh, mm = int(snap["id"][11:13]), int(snap["id"][14:16])
        at = datetime.fromisoformat(snap["date"]).replace(hour=hh, minute=mm, tzinfo=IST)
        out.append({"time": int(at.timestamp()) + 19800, "slot": snap["id"], "bull": snap["bull"], "bear": snap["bear"],
                    "label": snap["label"]})
    return out


def slot_time(slot_id: str) -> str:
    return datetime.strptime(slot_id[11:16], "%H:%M").strftime("%-I:%M %p")


# ---- Telegram ------------------------------------------------------------------------------

def wants_telegram(username: str) -> bool:
    return bool((store.get("bias_alerts", username) or {}).get("telegram"))


def set_telegram(username: str, on: bool) -> None:
    store.put("bias_alerts", username, {"telegram": on})


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


def sentiment(username: str, now: datetime) -> dict | None:
    """The latest count today, for other messages to carry. None before the first count, or if the
    user can't see the Fractal bias tab."""
    user = store.get("users", username) or {}
    if not (user.get("role") == "superadmin" or "bias" in user.get("modules", [])):
        return None
    now = now.astimezone(IST)
    snaps = [s for s in day_snapshots(now.date().isoformat()) if datetime.fromisoformat(s["at"]) <= now]
    if not snaps:
        return None
    r = read(snaps[-1]["signals"])
    return {"label": r["label"], "tone": r["tone"], "bull": r["bull"], "bear": r["bear"], "buys": r["buys"], "sells": r["sells"],
            "stocks": r["stocks"], "count": r["count"],
            "as_of": slot_time(snaps[-1]["id"]), "day": snaps[-1]["date"], "slot": snaps[-1]["id"]}


def sentiment_line(reading: dict | None) -> str:
    """'Fractal bias: Mildly bullish · 10 leaning bullish, 6 bearish (11:30 AM)'."""
    if not reading:
        return ""
    return f"Fractal bias: {reading['label']} · {reading['bull']} leaning bullish, {reading['bear']} bearish ({reading['as_of']})"


def announce(snap: dict) -> dict | None:
    """Telegram the reading at the day's first snapshot and whenever its label changes. Each message also
    carries the Nifty OI reading at that moment, for those who can see it."""
    day = store.get("bias_days", snap["date"]) or {}
    was = day.get("announced")
    if snap["label"] == was:
        return None
    store.update("bias_days", snap["date"], {"date": snap["date"], "announced": snap["label"]})
    subject, body = message(snap, was)
    users = [u["username"] for u in store.list("users")
             if u.get("active", True) and (u.get("role") == "superadmin" or "bias" in u.get("modules", []))
             and wants_telegram(u["username"])]
    at = datetime.fromisoformat(snap["at"])
    results = {u: notify.send(u, ["telegram"], subject, add_line(body, oi.sentiment_line(oi.sentiment(u, at)))).get("telegram")
               for u in users}
    log.info("bias %s announced %s: %s", snap["id"], snap["label"], results)
    return results
