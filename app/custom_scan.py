"""Custom scanners: a person's own scans (app/scan_expr.py) over a list of stocks, run by hand ("Scan now") or live.

Scan now judges every stock on the latest completed candle, in the background like the MA "Scan now" (the slower
lane of Kite requests, one run per person, kept in memory until the next run or a restart). Given a date range, it
judges every candle in it instead and lists each time a stock passed.

A live scanner is judged at each close of its candle size, as MA scanners are (same window, same daily read 15
minutes after the close). It reports the stocks that pass now and didn't at the last close, so a stock that keeps
passing is reported once, and again only after it has dropped out and come back. Each stock is one Kite request per
close; daily candles of earlier days, when a scan uses daily or weekly values, are read once a day.

A scanner set to run once a day (`run_at`, "HH:MM") is judged instead on the candle that had closed by that time, and
sends every stock that passes, whether or not it passed the day before.
"""

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app import brokers, config, kite, ma_backtest, notify, scan_expr, universe, webhooks
from app.kite import Candle, KiteAuthError, KiteError, instruments
from app.ma_backtest import candle_end, label as tf_label
from app.ma_scanner import AFTER_CLOSE, LIVE_LIMIT, SETTLE, latest_close, limit_problem  # noqa: F401 (used by the routes)
from app.market import IST, MarketSettings, in_scan_window, is_trading_day, load_settings, now_ist
from app.scan_expr import Stock
from app.scanner import _ensure_session, session_notice
from app.store import new_id, store

log = logging.getLogger("custom_scan")

COL = "custom_scanners"
TIMEFRAMES = ["1m", "3m", "5m", "10m", "15m", "30m", "1h", "4h", "1d"]
MAX_SCANNERS = 5
MAX_LINES = 25  # stocks listed in one message
MAX_SHOWN = 3  # values shown per stock in a message
FEED = "scan_match_days"  # {user}:{date} -> {"user", "date", "items": [...]}
FEED_MAX = 2000
# A date range can span up to this many calendar days, by candle size, so a scan judges at most a few thousand candles a
# stock; and it's held to the backtests' request budget.
RANGE_DAYS = {"1m": 7, "3m": 15, "5m": 30, "10m": 45, "15m": 60, "30m": 120, "1h": 250, "4h": 365, "1d": 1500}
MAX_RANGE_ROWS = 5000


def describe(sc: dict) -> str:
    """'Nifty 100 · 15 min candles'"""
    return f"{universe.describe(sc['spec'])} · {tf_label(sc['tf']).lower()} candles"


# ---- candles ------------------------------------------------------------------------------------

_daily: dict[tuple, list[Candle]] = {}  # (token, day, count) -> completed daily candles before that day
_past: dict[tuple, list[Candle]] = {}  # (token, tf, day, count) -> intraday candles before that day (live only)


def _keep_today(cache: dict, day: date, pos: int) -> None:
    for k in [k for k in cache if k[pos] != day]:
        del cache[k]


def _start(tf: str, day: date, count: int) -> date:
    return max(ma_backtest.warmup_start(tf, day, None, count), day - timedelta(days=ma_backtest.max_days(tf) - 1))


def read(client, token: int, sc: dict, s: MarketSettings, now: datetime, upto: datetime, lane=None, live=False) -> Stock:
    """A stock's candles for this scan, up to the candle that closed at `upto`."""
    tf, day = sc["tf"], upto.astimezone(IST).date()
    need = scan_expr.needs(sc["logic"], tf)
    if live and tf != "1d":  # earlier sessions once a day, then only today's
        key = (token, tf, day, need["main"])
        if key not in _past:
            _keep_today(_past, day, 2)
            earlier = ma_backtest.fetch(client, token, tf, _start(tf, day, need["main"]), day - timedelta(days=1), s, now)
            _past[key] = [c for c in earlier if c.start.astimezone(IST).date() < day][-need["main"]:]
        main = _past[key] + [c for c in ma_backtest.fetch(client, token, tf, day, day, s, now) if c.start.astimezone(IST).date() == day]
    else:
        main = ma_backtest.fetch(client, token, tf, _start(tf, day, need["main"]), day, s, now, lane)
    main = [c for c in main if candle_end(c, tf, s) <= upto]
    daily: list[Candle] = []
    if need["daily"] and tf != "1d":
        key = (token, day, need["daily"])
        if key not in _daily:
            _keep_today(_daily, day, 1)
            if lane:
                lane.wait()
            got = client.candles_range(token, "1d", _start("1d", day, need["daily"]), day - timedelta(days=1))
            _daily[key] = [c for c in got if c.start.astimezone(IST).date() < day]
        daily = _daily[key]
    return Stock(tf, main, daily, s.open)


def read_range(client, token: int, sc: dict, s: MarketSettings, now: datetime, first: date, last: date, lane=None) -> Stock:
    """A stock's candles for judging every candle from `first` to `last`, with enough before them to warm up."""
    tf = sc["tf"]
    need = scan_expr.needs(sc["logic"], tf)
    main = ma_backtest.fetch(client, token, tf, ma_backtest.warmup_start(tf, first, None, need["main"]), last, s, now, lane)
    daily: list[Candle] = []
    if need["daily"] and tf != "1d":  # each day reads only the days before it (Stock.candles)
        daily = ma_backtest.fetch(client, token, "1d", ma_backtest.warmup_start("1d", first, None, need["daily"]), last, s, now, lane)
    return Stock(tf, main, daily, s.open)


def range_requests(sc: dict, n_stocks: int, first: date, last: date) -> int:
    tf, need = sc["tf"], scan_expr.needs(sc["logic"], sc["tf"])
    per = len(ma_backtest.chunks(tf, ma_backtest.warmup_start(tf, first, None, need["main"]), last))
    if need["daily"] and tf != "1d":
        per += len(ma_backtest.chunks("1d", ma_backtest.warmup_start("1d", first, None, need["daily"]), last))
    return n_stocks * per


def range_problem(sc: dict, n_stocks: int, first: date, last: date, today: date) -> str | None:
    if first > last:
        return "The start date is after the end date."
    if last > today:
        return "The end date can't be in the future."
    if (last - first).days + 1 > (cap := RANGE_DAYS[sc["tf"]]):
        return f"With {tf_label(sc['tf']).lower()} candles a range can be up to {cap} days. Pick fewer days or a longer candle."
    if (n := range_requests(sc, n_stocks, first, last)) > ma_backtest.MAX_REQUESTS:
        return (f"That's about {n} requests to Kite, more than the {ma_backtest.MAX_REQUESTS} one scan may use. "
                "Pick fewer days, a shorter list or a longer candle.")
    return None


def row(symbol: str, sc: dict, stock: Stock, j: int | None = None) -> dict:
    """What's shown and sent for a stock that passed on candle j (the last if not given): its close, the change on the
    candle, and the scan's values."""
    j = len(stock.main) - 1 if j is None else j
    c = stock.main[j]
    prev = stock.main[j - 1].close if j > 0 else None
    return {"symbol": symbol, "close": c.close, "change": (c.close - prev) / prev * 100 if prev else None,
            "at": c.start.isoformat(),
            "values": [[k, v] for k, v in scan_expr.readings(sc["logic"], stock, j) if k != "Close"]}


def hits(symbol: str, sc: dict, stock: Stock, first: date, last: date, first_only: bool) -> list[dict]:
    """Each candle from `first` to `last` the stock passed on; with `first_only`, only its first of each day."""
    out, seen = [], set()
    for j, c in enumerate(stock.main):
        day = _session_day(c)
        if first <= day <= last and day not in seen and scan_expr.passes(sc["logic"], stock, j):
            out.append(row(symbol, sc, stock, j))
            if first_only:
                seen.add(day)
        stock.forget()
    return out


# ---- Scan now -----------------------------------------------------------------------------------

@dataclass
class Screen:
    id: str
    user: str
    sc: dict  # the scan as it was when started (saved or not)
    symbols: list[str]
    upto: datetime
    started: datetime
    done: int = 0
    status: str = "running"  # running | done | cancelled | failed
    error: str = ""
    finished: datetime | None = None
    rows: list[dict] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    no_data: list[str] = field(default_factory=list)
    stop: threading.Event = field(default_factory=threading.Event)
    stocks: dict = field(default_factory=dict)  # symbol -> Stock, of the ones that passed, for their charts
    first: date | None = None  # a date range: every candle from `first` to `last` is judged
    last: date | None = None
    first_only: bool = True  # in a range, only a stock's first match of each day
    capped: bool = False  # a range found more than MAX_RANGE_ROWS matches; the rest aren't listed

    @property
    def total(self) -> int:
        return len(self.symbols)


_screens: dict[str, Screen] = {}
_lock = threading.Lock()


def current(username: str) -> Screen | None:
    return _screens.get(username)


def start(username: str, sc: dict, first: date | None = None, last: date | None = None, first_only: bool = True) -> Screen:
    """Scan now: on the latest completed candle, or given `first` and `last`, on every candle of those days."""
    s, now = load_settings(), now_ist()
    upto = latest_close(sc["tf"], s, now)
    if not upto:
        raise ValueError("There's no completed candle to scan yet.")
    symbols = universe.resolve(sc["spec"], now.date())
    if first and last:
        if problem := range_problem(sc, len(symbols), first, last, now.date()):
            raise ValueError(problem)
        upto = min(upto, s.close_at(datetime.combine(last, datetime.min.time(), IST)))
    with _lock:
        if (old := _screens.get(username)) and old.status == "running":
            raise ValueError("A scan is already running. Wait for it or cancel it first.")
        screen = Screen(new_id(), username, sc, symbols, upto, now, first=first if last else None,
                        last=last if first else None, first_only=first_only)
        _screens[username] = screen
    threading.Thread(target=_work, args=(screen,), name=f"custom-screen-{username}", daemon=True).start()
    return screen


def cancel(username: str) -> None:
    if sc := _screens.get(username):
        sc.stop.set()


def _session_day(c: Candle) -> date:
    return c.start.astimezone(IST).date()


def _work(screen: Screen) -> None:
    sc, s = screen.sc, load_settings()
    try:
        known = instruments()
        client = brokers.client_for(screen.user)
        for symbol in screen.symbols:
            if screen.stop.is_set():
                screen.status = "cancelled"
                break
            inst = known.get(symbol)
            if not inst:
                screen.missing.append(symbol)
            else:
                try:
                    if screen.first:
                        stock = read_range(client, inst.token, sc, s, now_ist(), screen.first, screen.last, kite.BACKTESTS)
                    else:
                        stock = read(client, inst.token, sc, s, now_ist(), screen.upto, kite.BACKTESTS)
                except KiteAuthError:
                    raise
                except KiteError as e:
                    log.warning("custom screen: %s candles failed: %s", symbol, e)
                    screen.failed.append(symbol)
                    stock = None
                if stock is not None and screen.first:
                    if not any(screen.first <= _session_day(c) <= screen.last for c in stock.main):
                        screen.no_data.append(symbol)
                    elif found := hits(symbol, sc, stock, screen.first, screen.last, screen.first_only):
                        room = MAX_RANGE_ROWS - len(screen.rows)
                        screen.capped |= len(found) > room
                        screen.rows += found[:max(0, room)]
                        screen.stocks[symbol] = stock
                elif stock is not None:
                    if not stock.main or _session_day(stock.main[-1]) != screen.upto.astimezone(IST).date():
                        screen.no_data.append(symbol)
                    elif scan_expr.passes(sc["logic"], stock):
                        screen.rows.append(row(symbol, sc, stock))
                        screen.stocks[symbol] = stock
            screen.done += 1
        else:
            screen.status = "done"
    except KiteAuthError:
        screen.status, screen.error = "failed", "Kite isn't connected or the session has expired. Log in on the Broker page and scan again."
    except KiteError as e:
        screen.status, screen.error = "failed", str(e)
    except Exception as e:
        log.exception("custom screen %s failed", screen.id)
        screen.status, screen.error = "failed", f"The scan stopped: {e}"
    screen.finished = now_ist()
    log.info("custom screen %s for %s: %s, %d/%d stocks, %d found", screen.id, screen.user, screen.status, screen.done,
             screen.total, len(screen.rows))


def chart(screen: Screen, symbol: str, at: str = "") -> dict:
    """A stock that passed: its last 150 candles, or given `at` (a candle's start) the candles around it with it marked,
    and the scan's price-scale indicators on its own candles drawn over them."""
    stock = screen.stocks.get(symbol)
    if not stock:
        raise KiteError(f"{symbol} isn't among this scan's results")
    daily = screen.sc["tf"] == "1d"
    candles = stock.main
    k = next((i for i, c in enumerate(candles) if c.start.isoformat() == at), None) if at else None
    first, end = (max(0, k - 150), min(len(candles), k + 60)) if k is not None else (max(0, len(candles) - 150), len(candles))

    def t(c: Candle):
        return c.start.astimezone(IST).date().isoformat() if daily else int(c.start.timestamp()) + 19800

    lines, seen = [], set()
    for c in scan_expr.conditions(screen.sc["logic"]):
        for v in (c["lhs"], c["rhs"]):
            ind = scan_expr.INDICATORS.get(v.get("ind", ""))
            if v["kind"] != "ind" or v["tf"] or not ind.price_scale or v["params"].get("src") == "volume":
                continue
            name = scan_expr.describe_value({**v, "ago": 0, "pct": 100})
            if name in seen:
                continue
            seen.add(name)
            values = ind.compute(candles, v["params"])[v["out"]]
            lines.append({"name": name, "points": [{"time": t(cd), "value": round(x, 4)}
                                                   for cd, x in zip(candles[first:end], values[first:end]) if x is not None]})
    return {"symbol": symbol, "daily": daily, "lines": lines, "mark": t(candles[k]) if k is not None else None,
            "candles": [{"time": t(c), "open": c.open, "high": c.high, "low": c.low, "close": c.close} for c in candles[first:end]]}


def csv_rows(screen: Screen) -> list[list]:
    """A range scan's matches, oldest first, for download."""
    labels = scan_expr.labels(screen.sc["logic"])
    out = [["Time", "Stock", "Close", "Change %"] + [x for x in labels if x != "Close"]]
    for r in sorted(screen.rows, key=lambda r: (r["at"], r["symbol"])):
        values = dict(r["values"])
        out.append([datetime.fromisoformat(r["at"]).astimezone(IST).strftime("%Y-%m-%d %H:%M"), r["symbol"], r["close"],
                    "" if r["change"] is None else round(r["change"], 2)] + [values.get(x, "") for x in labels if x != "Close"])
    return out


# ---- live ---------------------------------------------------------------------------------------

def message(sc: dict, items: list[dict], now: datetime, upto: datetime | None = None) -> tuple[str, str]:
    """The message for new matches, or for a once-a-day scanner (given `upto`), everything that matched by then."""
    if upto is not None:
        when = upto.astimezone(IST).strftime("%-I:%M %p")
        subject = (f"🔎 {sc['name']}: {len(items)} stock{'s' if len(items) != 1 else ''} match (as of {when})" if items
                   else f"🔎 {sc['name']}: no stock matches (as of {when})")
    elif len(items) == 1:
        subject = f"🔎 {items[0]['symbol']} matched {sc['name']}"
    else:
        subject = f"🔎 {sc['name']}: {len(items)} new matches"

    def line(i: dict) -> str:
        change = f" ({i['change']:+.2f}%)" if i.get("change") is not None else ""
        shown = [f"{k} {scan_expr._g(v)}" for k, v in i["values"][:MAX_SHOWN] if v is not None]
        return f"{i['symbol']} ₹{i['close']:,.2f}{change}" + "".join(f" · {x}" for x in shown)

    lines = [f"{describe(sc)}", f"Scan: {scan_expr.describe(sc['logic'])}", ""] + [line(i) for i in items[:MAX_LINES]]
    if len(items) > MAX_LINES:
        lines.append(f"… and {len(items) - MAX_LINES} more")
    lines += ["", f"Time: {now.astimezone(IST):%d %b, %-I:%M %p} IST", f"{config.BASE_URL}/scans"]
    return subject, "\n".join(lines)


def hook_body(sc: dict, items: list[dict], now: datetime, test: bool = False) -> dict:
    return {"event": "scan_matches", "test": test, "scanner_id": sc["id"], "scanner": sc["name"], "timeframe": sc["tf"],
            "scan": scan_expr.describe(sc["logic"]), "time": now.isoformat(),
            "matches": [{"symbol": i["symbol"], "close": i["close"], "change": i.get("change"), "candle": i.get("at"),
                         "values": {k: v for k, v in i["values"]}} for i in items],
            "payload": json.loads(sc["webhook_payload"]) if sc.get("webhook_payload") else None}


_feed_lock = threading.Lock()


def record(sc: dict, items: list[dict], now: datetime) -> None:
    day = now.astimezone(IST).date().isoformat()
    doc_id = f"{sc['user']}:{day}"
    new = [{"at": now.isoformat(), "scanner_id": sc["id"], "scanner": sc["name"], "tf": sc["tf"], **i} for i in items]
    with _feed_lock:
        doc = store.get(FEED, doc_id) or {"user": sc["user"], "date": day, "items": []}
        store.put(FEED, doc_id, {**doc, "items": (doc["items"] + new)[-FEED_MAX:]})


def feed(username: str, day: str) -> list[dict]:
    return sorted((store.get(FEED, f"{username}:{day}") or {}).get("items", []), key=lambda i: i["at"], reverse=True)


def send(sc: dict, items: list[dict], now: datetime, upto: datetime | None = None) -> dict:
    """Send new matches; a once-a-day scanner's list (given `upto`) goes out even when it's empty."""
    if not items and upto is None:
        return {}
    record(sc, items, now)
    subject, body = message(sc, items, now, upto)
    results = notify.send(sc["user"], sc.get("channels", []), subject, body)
    if hooks := webhooks.targets(sc["user"], sc.get("hook_ids")):
        results["webhook"] = "sending"
    store.put("events", event_id := new_id(), {
        "user": sc["user"], "alert_id": sc["id"], "key": f"scan:{sc['id']}", "symbol": items[0]["symbol"] if items else "",
        "summary": subject.split(" ", 1)[1], "price": items[0]["close"] if items else None, "at": now.isoformat(),
        "delivery": results})
    webhooks.dispatch(hooks, hook_body(sc, items, now), event_id)
    log.info("scan %s for %s: %d new matches, %s", sc["id"], sc["user"], len(items), results)
    return results


_locks: dict[str, threading.Lock] = {}


def lock(scanner_id: str) -> threading.Lock:
    return _locks.setdefault(scanner_id, threading.Lock())


def evaluate(sc: dict, client, s: MarketSettings, now: datetime, upto: datetime, every: bool = False) -> dict:
    """Judge every stock on the candle that closed at `upto`; report the ones newly passing, or with `every` (a
    once-a-day scanner) all of them. Returns the scanner as saved."""
    known = instruments()
    before = set() if every else set(sc.get("matched") or [])
    passing, found, missing, failed = [], [], [], []
    day = upto.astimezone(IST).date()
    for symbol in universe.resolve(sc["spec"], now.date()):
        inst = known.get(symbol)
        if not inst:
            missing.append(symbol)
            continue
        try:  # once a day, in the slower lane, so scanners judging every candle keep their room
            stock = read(client, inst.token, sc, s, now, upto, kite.BACKTESTS) if every else read(client, inst.token, sc, s, now, upto, live=True)
        except KiteAuthError:
            raise
        except KiteError as e:
            log.warning("scan %s: %s candles failed: %s", sc["id"], symbol, e)
            failed.append(symbol)
            if symbol in before:
                passing.append(symbol)  # unknown this time: not dropped, so not reported again when it's read next
            continue
        # Only a stock with a candle that closed at `upto` is judged: one that hasn't traded since keeps its standing.
        if not stock.main or candle_end(stock.main[-1], sc["tf"], s) != upto or _session_day(stock.main[-1]) != day:
            if symbol in before:
                passing.append(symbol)
            continue
        if scan_expr.passes(sc["logic"], stock):
            passing.append(symbol)
            if symbol not in before:
                found.append(row(symbol, sc, stock))
    with lock(sc["id"]):
        latest = store.get(COL, sc["id"]) or {}
        if latest and latest.get("armed_at") != sc.get("armed_at"):
            return latest  # edited or resumed while the candles were being read
        changes = {"matched": passing, "evaluated_at": upto.isoformat(), "checked_at": now.isoformat(),
                   "missing": missing, "failed": failed, "error": ""}
        if every:
            changes["ran_on"] = now.astimezone(IST).date().isoformat()
        store.update(COL, sc["id"], changes)
    sc = {**sc, **changes}
    send(sc, found, now, upto if every else None)
    return sc


def _active() -> dict[str, list[dict]]:
    by_user: dict[str, list[dict]] = {}
    for sc in store.list(COL, status="active"):
        by_user.setdefault(sc["user"], []).append(sc)
    return by_user


def run_candles(now: datetime | None = None) -> None:
    """Scheduler job: judge every scanner whose candle has closed since it was last judged."""
    s, now = load_settings(), now or now_ist()
    if not (in_scan_window(s, now) or (is_trading_day(now) and s.close_at(now) <= now < s.close_at(now) + AFTER_CLOSE)):
        return
    for username, scanners in _active().items():
        due = [(sc, upto) for sc in scanners if not sc.get("run_at")
               and (upto := latest_close(sc["tf"], s, now)) and upto.isoformat() > (sc.get("evaluated_at") or "")]
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
                log.exception("scan %s failed", sc["id"])
                store.update(COL, sc["id"], {"error": str(e)[:200]})



# ---- once a day ---------------------------------------------------------------------------------

def run_time(sc: dict, day: datetime) -> datetime:
    """When a once-a-day scanner runs on `day`."""
    h, m = map(int, sc["run_at"].split(":"))
    return day.astimezone(IST).replace(hour=h, minute=m, second=0, microsecond=0)


def due_daily(sc: dict, now: datetime) -> bool:
    """Its time has come today and it hasn't run yet. One saved or resumed after today's time first runs tomorrow."""
    at = run_time(sc, now)
    return (bool(sc.get("run_at")) and now >= at and sc.get("ran_on") != now.astimezone(IST).date().isoformat()
            and (sc.get("armed_at") or "") <= at.isoformat())


def run_daily(now: datetime | None = None) -> None:
    """Scheduler job: run each once-a-day scanner whose time has come, on the candle that had closed by then. One that
    runs late (the server was asleep) reads the same candles it would have on time."""
    s, now = load_settings(), now or now_ist()
    if not is_trading_day(now):
        return
    for username, scanners in _active().items():
        due = [sc for sc in scanners if due_daily(sc, now)]
        if not due:
            continue
        client = _ensure_session(username, len(scanners), now)
        if not client:
            continue
        for sc in due:
            upto = latest_close(sc["tf"], s, run_time(sc, now) + SETTLE + timedelta(seconds=1))
            try:
                if not upto:
                    raise KiteError("no completed candle to scan")
                evaluate(sc, client, s, now, upto, every=True)
            except KiteAuthError as e:
                store.update(COL, sc["id"], {"error": "Kite session expired"})
                session_notice(username, "expired", str(e), len(scanners), now)
                break
            except Exception as e:
                log.exception("daily scan %s failed", sc["id"])
                store.update(COL, sc["id"], {"error": str(e)[:200], "ran_on": now.date().isoformat()})
