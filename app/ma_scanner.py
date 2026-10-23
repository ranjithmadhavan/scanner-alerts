"""Live MA crossover scanners: each person's saved scanners, run on their own Kite session.

A scanner watches a list of stocks (or one) on one candle size for the setups in app.crossover, and sends:
* *setup*: a trigger candle has just closed, with the level to enter at, its SL and target;
* *entry*: price traded beyond that level. On a gap (a session opening beyond a level set on an earlier
  day), only if the scanner asks for gap entries.

Two scheduler jobs do the work, so neither holds the other up:
* at each candle close of the scanner's timeframe (daily: just after the close), every stock's candles are
  read again (earlier sessions once a day, then only today's) and the setups worked out. That is one Kite
  request per stock, which is why the list size is capped by timeframe (LIVE_LIMIT);
* every tick, one quote request covers every stock waiting on a level, so an entry is reported within a
  tick of happening rather than at the next candle close. Anything a quote missed (a quick spike) is
  still caught at the candle close, from the candles.

The setups waiting on a level are saved on the scanner (`state`), so a restart picks up where it was.
"""

import json
import logging
import threading
from collections import defaultdict
from datetime import datetime, timedelta

from app import config, crossover, ma_backtest, notify, universe, webhooks
from app.crossover import Rules
from app.kite import Candle, KiteAuthError, KiteError, instruments
from app.market import IST, MarketSettings, in_scan_window, is_market_open, is_trading_day, load_settings, now_ist
from app.ma_backtest import candle_end, last_boundary
from app.scanner import _ensure_session, session_notice
from app.store import new_id, store

log = logging.getLogger("ma.scanner")

COL = "ma_scanners"
TIMEFRAMES = ["5m", "10m", "15m", "30m", "1h", "4h", "1d"]
# Stocks a scanner may watch on each candle size: Kite allows ~3 candle requests a second, and each stock
# is one request per candle close.
LIVE_LIMIT = {"5m": 100, "10m": 200, "15m": 500, "30m": 500, "1h": None, "4h": None, "1d": None}
MAX_SCANNERS = 5
MAX_LINES = 25  # stocks listed in one message
KEEP_FIRED = 3000
SETTLE = timedelta(seconds=10)  # let Kite finish the candle that just closed
# Daily candles are read 15 minutes after the close (3:45 PM), not at it: at 3:30 Kite's daily candle closes on
# the last traded price, and it takes NSE's official close (a weighted average of the last half hour) a few
# minutes later. A candle that only just closed red, or only just reached the MA, could otherwise be judged on
# the wrong close.
DAILY_READ_AFTER = timedelta(minutes=15)
# The candle job keeps running this long after the close, for the daily read; missed altogether (the app down
# then), the day is read at the next open instead.
AFTER_CLOSE = timedelta(minutes=45)
# Candles read, in multiples of what the averages need: with one entry per crossover, the crossover must be among
# them (a trend older than this gives no setups). Still one request per stock.
LOOKBACK = 5
# The candle job and the quote job both save a scanner's state; each holds its scanner's lock to read, merge and save.
_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)


def limit_problem(n: int, tf: str) -> str | None:
    cap = LIVE_LIMIT[tf]
    if cap is not None and n > cap:
        return (f"{ma_backtest.label(tf)} candles can be scanned live on up to {cap} stocks (this list has {n}). "
                f"Pick a longer candle or a shorter list.")
    return None


def rules_of(sc: dict) -> Rules:
    return Rules(**sc["rules"])


def exits_of(sc: dict) -> crossover.Exits:
    """How a scanner follows a first entry, to know if it was stopped out (for a second chance): its SL and target."""
    rr = float(sc.get("rr") or 0)
    return crossover.Exits(stop=sc.get("stop", "candle"), rr=rr, book=100 if rr else 0,
                           fill="open" if sc.get("gap_alerts") else "skip")


def latest_close(tf: str, s: MarketSettings, now: datetime) -> datetime | None:
    """When the most recent candle of this size that has settled closed: today's, or the last session's close.
    A daily candle counts as settled DAILY_READ_AFTER after the close."""
    if is_trading_day(now) and (b := last_boundary(tf, s, now - (DAILY_READ_AFTER if tf == "1d" else SETTLE))):
        return b
    day = now - timedelta(days=1)
    for _ in range(10):
        if is_trading_day(day):
            return s.close_at(day)
        day -= timedelta(days=1)
    return None


# ---- candles -------------------------------------------------------------------------------

_past: dict[tuple, list[Candle]] = {}  # (token, candle size, day, warm-up) -> candles before that day


def candles_for(client, token: int, tf: str, rules: Rules, s: MarketSettings, now: datetime) -> list[Candle]:
    """Completed candles, enough of them for the averages and to see the last crossover. Intraday, earlier sessions
    are fetched once a day."""
    day = now.astimezone(IST).date()
    count = rules.warmup() * LOOKBACK
    start = max(ma_backtest.warmup_start(tf, day, rules, count), day - timedelta(days=ma_backtest.max_days(tf) - 1))
    if tf == "1d":
        return ma_backtest.fetch(client, token, tf, start, day, s, now)
    key = (token, tf, day, count)
    if key not in _past:
        for k in [k for k in _past if k[2] != day]:
            del _past[k]
        earlier = ma_backtest.fetch(client, token, tf, start, day - timedelta(days=1), s, now)
        _past[key] = [c for c in earlier if c.start.astimezone(IST).date() < day][-count:]
    today = ma_backtest.fetch(client, token, tf, day, day, s, now)
    return _past[key] + [c for c in today if c.start.astimezone(IST).date() == day]


# ---- what to say ----------------------------------------------------------------------------

def plan(sc: dict, side: str, entry: float, candle_stop: float, slow_now: float | None) -> tuple[float | None, float | None]:
    """(SL, target) for a trade entered at `entry`, as the scanner's settings have them."""
    stop = candle_stop if sc.get("stop", "candle") == "candle" else slow_now
    rr = float(sc.get("rr") or 0)
    if stop is None or (entry - stop) * (1 if side == "buy" else -1) <= 0:
        return stop, None
    return stop, (entry + rr * (entry - stop) if rr else None)  # entry - stop is negative for a sell


def _line(sc: dict, item: dict) -> str:
    above = "above" if item["side"] == "buy" else "below"
    head = f"{item['symbol']} {item['side']}"
    if item.get("price") is None:
        what = f"{head} {above} ₹{item['level']:,.2f}"
    elif item.get("gap"):
        what = f"{head}: opened at ₹{item['price']:,.2f}, {above} ₹{item['level']:,.2f} (gap)"
    else:
        what = f"{head}: broke ₹{item['level']:,.2f}, now ₹{item['price']:,.2f}"
    stop = item.get("stop")
    sl = "" if stop is None else (f" · SL ₹{stop:,.2f}" if sc.get("stop", "candle") == "candle" else f" · SL slow MA (₹{stop:,.2f} now)")
    target = f" · target ₹{item['target']:,.2f} (1:{float(sc['rr']):g})" if item.get("target") is not None else ""
    return what + sl + target


def describe(sc: dict) -> str:
    """'Nifty 200 · 9 EMA / 21 EMA on 15 min candles'"""
    return f"{universe.describe(sc['spec'])} · {rules_of(sc).label} on {ma_backtest.label(sc['tf']).lower()} candles"


def message(sc: dict, kind: str, items: list[dict], now: datetime) -> tuple[str, str]:
    first = items[0]
    if kind == "setup":
        subject = (f"📈 {first['symbol']}: {first['side']} {'above' if first['side'] == 'buy' else 'below'} {first['level']:,.2f}"
                   if len(items) == 1 else f"📈 {len(items)} new MA setups · {sc['name']}")
        intro = "Trigger candle closed. Enter on a trade beyond the level:"
    else:
        subject = (f"🚀 {first['symbol']} {first['side']} triggered at {first['level']:,.2f}{' (gap)' if first.get('gap') else ''}"
                   if len(items) == 1 else f"🚀 {len(items)} MA entries triggered · {sc['name']}")
        intro = "Entry level traded:"
    lines = [f"{sc['name']}: {describe(sc)}", intro] + [_line(sc, i) for i in items[:MAX_LINES]]
    if len(items) > MAX_LINES:
        lines.append(f"… and {len(items) - MAX_LINES} more")
    lines += [f"Time: {now.astimezone(IST):%d %b, %-I:%M %p} IST", f"{config.BASE_URL}/ma"]
    return subject, "\n".join(lines)


def hook_body(sc: dict, kind: str, items: list[dict], now: datetime, test: bool = False) -> dict:
    return {"event": "ma_setups" if kind == "setup" else "ma_entries", "test": test, "scanner_id": sc["id"],
            "scanner": sc["name"], "timeframe": sc["tf"], "averages": rules_of(sc).label, "time": now.isoformat(),
            "signals": [{k: i.get(k) for k in ("symbol", "side", "level", "stop", "target", "price", "gap", "trigger_at")}
                        for i in items],
            "payload": json.loads(sc["webhook_payload"]) if sc.get("webhook_payload") else None}


# ---- the signals feed: what the scanners found, per person per day ---------------------------
FEED = "ma_signal_days"  # {user}:{date} -> {"user", "date", "items": [...]}
FEED_DAYS = 7
FEED_MAX = 2000  # items kept a day
_feed_lock = threading.Lock()


def record(sc: dict, kind: str, items: list[dict], now: datetime) -> None:
    """Add what was just sent to the person's feed for today (one document a day, so reading it is cheap)."""
    day = now.astimezone(IST).date().isoformat()
    doc_id = f"{sc['user']}:{day}"
    new = [{"at": now.isoformat(), "kind": kind, "scanner_id": sc["id"], "scanner": sc["name"], "tf": sc["tf"],
            **{k: i.get(k) for k in ("symbol", "side", "level", "stop", "target", "price", "gap", "trigger_at")}}
           for i in items]
    with _feed_lock:
        doc = store.get(FEED, doc_id) or {"user": sc["user"], "date": day, "items": []}
        store.put(FEED, doc_id, {**doc, "items": (doc["items"] + new)[-FEED_MAX:]})


def feed(username: str, day: str) -> list[dict]:
    """One day's signals, newest first."""
    return sorted((store.get(FEED, f"{username}:{day}") or {}).get("items", []), key=lambda i: i["at"], reverse=True)


def feed_days(username: str, today) -> list[str]:
    """The days the feed can show: the last FEED_DAYS calendar days with anything in them, today first."""
    days = [(today - timedelta(days=k)).isoformat() for k in range(FEED_DAYS)]
    return [d for d in days if d == days[0] or store.get(FEED, f"{username}:{d}")]


def send(sc: dict, kind: str, items: list[dict], now: datetime) -> dict:
    if not items:
        return {}
    record(sc, kind, items, now)
    subject, body = message(sc, kind, items, now)
    results = notify.send(sc["user"], sc.get("channels", []), subject, body)
    if sc.get("webhooks"):
        results["webhook"] = webhooks.send(sc["webhooks"], hook_body(sc, kind, items, now))
    store.put("events", new_id(), {
        "user": sc["user"], "alert_id": sc["id"], "key": f"ma:{sc['id']}", "symbol": items[0]["symbol"],
        "summary": subject.split(" ", 1)[1], "price": items[0].get("price") or items[0]["level"], "level": items[0]["level"],
        "signal": items[0]["side"], "at": now.isoformat(), "delivery": results,
    })
    log.info("ma %s %s for %s: %d stocks, %s", sc["id"], kind, sc["user"], len(items), results)
    return results


# ---- the two jobs ---------------------------------------------------------------------------

def _cutoff(sc: dict) -> datetime:
    """Only setups and entries on candles that closed after this are reported."""
    times = [datetime.fromisoformat(sc["armed_at"])] + ([datetime.fromisoformat(sc["evaluated_at"])] if sc.get("evaluated_at") else [])
    return max(times)


def evaluate(sc: dict, client, s: MarketSettings, now: datetime, upto: datetime) -> dict:
    """Read every stock's candles up to the candle that closed at `upto`, report new setups and any entry the
    quotes missed, and save what is now waiting. Returns the scanner as saved."""
    rules, tf = rules_of(sc), sc["tf"]
    cutoff = _cutoff(sc)
    seen = set(sc.get("fired", []))
    new_keys: list[str] = []
    known = instruments()
    state, setups, entries, missing, failed = {}, [], [], [], []
    for symbol in universe.resolve(sc["spec"], now.date()):
        inst = known.get(symbol)
        if not inst:
            missing.append(symbol)
            continue
        try:
            candles = [c for c in candles_for(client, inst.token, tf, rules, s, now) if candle_end(c, tf, s) <= upto]
        except KiteAuthError:
            raise
        except KiteError as e:
            log.warning("ma %s: %s candles failed: %s", sc["id"], symbol, e)
            failed.append(symbol)
            if symbol in sc.get("state", {}):
                state[symbol] = sc["state"][symbol]  # keep watching what we had
            continue
        if not candles:
            continue
        found, pending, fast, slow = crossover.scan(candles, rules, exits_of(sc))
        for e in found:
            key = f"{symbol}:{candles[e.setup.trigger].start.isoformat()}:entry"
            if candle_end(candles[e.index], tf, s) <= cutoff or key in seen:
                continue
            seen.add(key)
            new_keys.append(key)
            if e.gap and not sc.get("gap_alerts"):
                continue
            price = candles[e.index].open if e.gap else e.setup.level
            stop, target = plan(sc, e.side, price, e.setup.stop, slow[e.index - 1] if e.index else None)
            entries.append({"key": key, "symbol": symbol, "side": e.side, "level": e.setup.level, "stop": stop,
                            "target": target, "price": price, "gap": e.gap,
                            "trigger_at": candles[e.setup.trigger].start.isoformat()})
        if p := pending:
            trigger = candles[p.trigger]
            item = {"side": p.side, "level": p.level, "candle_stop": p.stop, "slow": slow[p.trigger],
                    "trigger_at": trigger.start.isoformat(), "token": inst.token}
            state[symbol] = item
            key = f"{symbol}:{item['trigger_at']}:setup"
            if candle_end(trigger, tf, s) > cutoff and key not in seen:
                seen.add(key)
                new_keys.append(key)
                stop, target = plan(sc, p.side, p.level, p.stop, slow[p.trigger])
                setups.append({"symbol": symbol, "side": p.side, "level": p.level, "stop": stop, "target": target,
                               "trigger_at": item["trigger_at"]})
    with _locks[sc["id"]]:
        # The quote job may have reported entries while the candles were being read: don't report them twice,
        # and don't put their levels back on watch.
        fired = (store.get(COL, sc["id"]) or {}).get("fired", [])
        done = set(fired)
        entries = [e for e in entries if e["key"] not in done]
        state = {sym: it for sym, it in state.items() if f"{sym}:{it['trigger_at']}:entry" not in done}
        changes = {"state": state, "evaluated_at": upto.isoformat(), "checked_at": now.isoformat(),
                   "fired": (fired + [k for k in new_keys if k not in done])[-KEEP_FIRED:],
                   "missing": missing, "failed": failed, "error": ""}
        store.update(COL, sc["id"], changes)
    sc = {**sc, **changes}
    send(sc, "entry", entries, now)
    send(sc, "setup", setups, now)
    return sc


def check_quotes(sc: dict, client, s: MarketSettings, now: datetime) -> dict:
    """One quote request for every stock waiting on a level; report those that have traded beyond it."""
    with _locks[sc["id"]]:
        sc = store.get(COL, sc["id"]) or sc
        waiting = sc.get("state") or {}
        if not waiting:
            return sc
        got = client.quote([f"NSE:{sym}" for sym in waiting])
        sc, entries = _quoted(sc, got, now)
    send(sc, "entry", entries, now)
    return sc


def _quoted(sc: dict, got: dict, now: datetime) -> tuple[dict, list[dict]]:
    waiting = sc["state"]
    today = now.astimezone(IST).date()
    fired, state, entries = list(sc.get("fired", [])), dict(waiting), []
    for symbol, item in waiting.items():
        q = got.get(f"NSE:{symbol}") or {}
        ltp, day_open = q.get("last_price"), (q.get("ohlc") or {}).get("open")
        if not ltp:
            continue
        buy = item["side"] == "buy"
        if not (ltp > item["level"] if buy else ltp < item["level"]):
            continue
        earlier_day = datetime.fromisoformat(item["trigger_at"]).astimezone(IST).date() < today
        gap = bool(earlier_day and day_open and (day_open > item["level"] if buy else day_open < item["level"]))
        del state[symbol]
        key = f"{symbol}:{item['trigger_at']}:entry"
        if key in fired:
            continue
        fired.append(key)
        if gap and not sc.get("gap_alerts"):
            continue
        price = day_open if gap else ltp
        stop, target = plan(sc, item["side"], day_open if gap else item["level"], item["candle_stop"], item.get("slow"))
        entries.append({"symbol": symbol, "side": item["side"], "level": item["level"], "stop": stop, "target": target,
                        "price": price, "gap": gap, "trigger_at": item["trigger_at"]})
    changes = {"state": state, "fired": fired[-KEEP_FIRED:], "quoted_at": now.isoformat()}
    store.update(COL, sc["id"], changes)
    return {**sc, **changes}, entries


def _active() -> dict[str, list[dict]]:
    by_user: dict[str, list[dict]] = {}
    for sc in store.list(COL, status="active"):
        by_user.setdefault(sc["user"], []).append(sc)
    return by_user


def run_candles(now: datetime | None = None) -> None:
    """Scheduler job: evaluate every scanner whose candle has closed since it was last evaluated."""
    s, now = load_settings(), now or now_ist()
    if not (in_scan_window(s, now) or (is_trading_day(now) and s.close_at(now) <= now < s.close_at(now) + AFTER_CLOSE)):
        return
    for username, scanners in _active().items():
        due = [(sc, upto) for sc in scanners
               if (upto := latest_close(sc["tf"], s, now)) and upto.isoformat() > (sc.get("evaluated_at") or "")]
        if not due:
            continue
        client = _ensure_session(username, len(scanners), now)
        if not client:
            continue
        for sc, upto in due:
            try:
                evaluate(sc, client, s, now, upto)
            except KiteAuthError as e:
                store.update(COL, sc["id"], {"error": "Kite session expired"})
                session_notice(username, "expired", str(e), len(scanners), now)
                break
            except Exception as e:
                log.exception("ma scanner %s failed", sc["id"])
                store.update(COL, sc["id"], {"error": str(e)[:200]})


def run_quotes(now: datetime | None = None) -> None:
    """Scheduler job: look for entries on every scanner's waiting levels while the market is open."""
    s, now = load_settings(), now or now_ist()
    if not is_market_open(s, now):
        return
    for username, scanners in _active().items():
        waiting = [sc for sc in scanners if sc.get("state")]
        if not waiting:
            continue
        client = _ensure_session(username, len(scanners), now)
        if not client:
            continue
        for sc in waiting:
            try:
                check_quotes(sc, client, s, now)
            except KiteAuthError:
                break  # the candle job tells the user
            except KiteError as e:
                log.warning("ma %s quotes failed: %s", sc["id"], e)
            except Exception:
                log.exception("ma scanner %s quotes failed", sc["id"])
