"""MA crossover backtests over one stock, a list, or a whole index, run in the background.

A run fetches each stock's candles from Kite (with enough earlier candles for the moving averages to settle),
replays app.crossover over them and keeps only the trades, so even the Nifty 500 fits in a small instance.
Kite allows about 3 candle requests a second, so a long list takes minutes: the page polls the run's progress.
One run per person at a time; the latest finished run is kept in memory (a restart loses it) for the
page, the per-trade charts and the CSV download.
"""

import csv
import io
import logging
import math
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app import brokers, crossover, fractals, universe
from app.crossover import Exits, Rules
from app.kite import Candle, KiteAuthError, KiteError, instruments
from app.market import IST, MarketSettings, load_settings, now_ist
from app.scanner import SETTLE, candle_end
from app.store import new_id

log = logging.getLogger("ma.backtest")

TIMEFRAMES = ["3m", "5m", "10m", "15m", "30m", "1h", "1d"]
MAX_REQUESTS = 1500  # ~9 minutes of Kite requests
MAX_SHOWN = 500  # trades listed on the page (the CSV has all of them)
CHART_SIDE = 400  # candles drawn either side of a trade


def chunks(tf: str, start: date, end: date) -> list[tuple[date, date]]:
    """Date ranges no longer than Kite serves in one request for this candle size."""
    step = fractals.MAX_DAYS[tf]
    out, at = [], start
    while at <= end:
        out.append((at, min(end, at + timedelta(days=step - 1))))
        at += timedelta(days=step)
    return out


def warmup_start(tf: str, start: date, rules: Rules) -> date:
    """How far before `start` to fetch so the slow average has settled by then."""
    per_day = 1 if tf == "1d" else max(1, fractals.SESSION_MINUTES // fractals.minutes(tf))
    trading_days = math.ceil(rules.warmup() / per_day)
    return start - timedelta(days=math.ceil(trading_days * 7 / 5) + 7)


def fetch(client, token: int, tf: str, start: date, end: date, s: MarketSettings, now: datetime) -> list[Candle]:
    out: list[Candle] = []
    for a, b in chunks(tf, start, end):
        out += client.candles_range(token, tf, a, b)
    return [c for c in out if candle_end(c, tf, s) <= now - SETTLE]  # completed candles only


def requests_needed(n_stocks: int, tf: str, start: date, end: date, rules: Rules) -> int:
    return n_stocks * len(chunks(tf, warmup_start(tf, start, rules), end))


@dataclass
class Params:
    spec: dict  # {"list": key} or {"symbols": [...]}
    tf: str
    start: date
    end: date
    rules: Rules
    exits: Exits
    risk: float | None = None  # ₹ risked per trade, for money figures

    @property
    def minutes(self) -> int:
        return 0 if self.tf == "1d" else fractals.minutes(self.tf)


@dataclass
class Run:
    id: str
    user: str
    params: Params
    symbols: list[str]
    started: datetime
    done: int = 0
    status: str = "running"  # running | done | cancelled | failed
    error: str = ""
    finished: datetime | None = None
    trades: list[dict] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # not on Kite's NSE list
    failed: list[str] = field(default_factory=list)  # Kite wouldn't give candles
    no_data: list[str] = field(default_factory=list)  # too few candles for the averages
    stop: threading.Event = field(default_factory=threading.Event)
    chart_cache: dict = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.symbols)


_runs: dict[str, Run] = {}
_lock = threading.Lock()


def current(username: str) -> Run | None:
    return _runs.get(username)


def start(username: str, p: Params) -> Run:
    """Start a run in the background. Raises ValueError if one is already running for this person."""
    symbols = universe.resolve(p.spec, now_ist().date())
    with _lock:
        if (r := _runs.get(username)) and r.status == "running":
            raise ValueError("A backtest is already running. Wait for it or cancel it first.")
        run = Run(new_id(), username, p, symbols, now_ist())
        _runs[username] = run
    threading.Thread(target=_work, args=(run,), name=f"ma-backtest-{username}", daemon=True).start()
    return run


def cancel(username: str) -> None:
    if r := _runs.get(username):
        r.stop.set()


def _record(symbol: str, t: crossover.Trade, candles: list[Candle]) -> dict:
    e = t.entry
    def at(i: int) -> str:
        return candles[i].start.isoformat()  # every time is the start of its candle, so it lines up on the chart
    return {
        "symbol": symbol, "side": e.side, "status": t.status, "note": t.note, "gap": e.gap,
        "trigger_at": candles[e.setup.trigger].start.isoformat(), "level": e.setup.level,
        "entry_at": candles[e.index].start.isoformat(), "price": t.price, "stop": t.stop, "risk": t.risk, "target": t.target,
        "exits": [{"part": f.part, "price": f.price, "at": at(f.index), "reason": f.reason} for f in t.fills],
        "exit_at": at(t.exit_index) if t.exit_index is not None else None, "exit_price": t.exit_price,
        "reason": t.reason, "r": t.r, "pct": t.pct,
    }


def _work(run: Run) -> None:
    p, s = run.params, load_settings()
    known = instruments()
    try:
        client = brokers.client_for(run.user)
        for symbol in run.symbols:
            if run.stop.is_set():
                run.status = "cancelled"
                break
            inst = known.get(symbol)
            if not inst:
                run.missing.append(symbol)
                run.done += 1
                continue
            try:
                candles = fetch(client, inst.token, p.tf, warmup_start(p.tf, p.start, p.rules), p.end, s, now_ist())
            except KiteAuthError:
                raise
            except KiteError as e:
                log.warning("ma backtest: %s candles failed: %s", symbol, e)
                run.failed.append(symbol)
                run.done += 1
                continue
            first = next((i for i, c in enumerate(candles) if c.start.astimezone(IST).date() >= p.start), len(candles))
            if first >= len(candles) or len(candles) < p.rules.warmup() // 2:
                run.no_data.append(symbol)
            else:
                got, _ = crossover.trades(candles, p.rules, p.exits, p.minutes, first)
                run.trades += [_record(symbol, t, candles) for t in got]
            run.done += 1
        else:
            run.status = "done"
    except KiteAuthError:
        run.status, run.error = "failed", "Kite isn't connected or the session has expired. Log in on the Broker page and run it again."
    except Exception as e:
        log.exception("ma backtest %s failed", run.id)
        run.status, run.error = "failed", f"The backtest stopped: {e}"
    run.finished = now_ist()
    log.info("ma backtest %s for %s: %s, %d/%d stocks, %d trades", run.id, run.user, run.status, run.done, run.total, len(run.trades))


# ---- results -----------------------------------------------------------------------------

def closed(run: Run) -> list[dict]:
    """Closed trades in the order they closed."""
    return sorted((t for t in run.trades if t["status"] == "closed"), key=lambda t: (t["exit_at"], t["entry_at"]))


def report(run: Run) -> dict:
    done = closed(run)
    as_trades = [type("T", (), {"r": t["r"], "pct": t["pct"]})() for t in done]
    totals = crossover.stats(as_trades)
    running, curve = 0.0, []
    for t in done:
        running += t["r"]
        curve.append({"time": t["exit_at"], "r": round(running, 3)})
    per: dict[str, dict] = {}
    for t in run.trades:
        row = per.setdefault(t["symbol"], {"symbol": t["symbol"], "trades": 0, "wins": 0, "r": 0.0, "open": 0, "skipped": 0})
        if t["status"] == "closed":
            row["trades"] += 1
            row["wins"] += t["r"] > 0
            row["r"] += t["r"]
        else:
            row[t["status"]] += 1
    stocks = sorted(per.values(), key=lambda r: -r["r"])
    by_reason: dict[str, int] = {}
    for t in done:
        by_reason[t["reason"]] = by_reason.get(t["reason"], 0) + 1
    return {
        "stats": totals, "curve": curve, "stocks": stocks, "reasons": sorted(by_reason.items(), key=lambda x: -x[1]),
        "open": sum(1 for t in run.trades if t["status"] == "open"),
        "skipped": sum(1 for t in run.trades if t["status"] == "skipped"),
        "gaps": sum(1 for t in run.trades if t["gap"]),
        "money": run.params.risk,
        "shown": sorted(run.trades, key=lambda t: t["entry_at"], reverse=True)[:MAX_SHOWN],
    }


CSV_FIELDS = ["symbol", "side", "status", "note", "gap", "trigger_at", "level", "entry_at", "price", "stop", "risk", "target",
              "exit_at", "exit_price", "reason", "r", "pct"]


def to_csv(run: Run) -> str:
    out = io.StringIO()
    w = csv.DictWriter(out, CSV_FIELDS + (["rupees"] if run.params.risk else []), extrasaction="ignore")
    w.writeheader()
    for t in sorted(run.trades, key=lambda t: t["entry_at"]):
        row = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in t.items()}
        if run.params.risk and t["r"] is not None:
            row["rupees"] = round(t["r"] * run.params.risk, 2)
        w.writerow(row)
    return out.getvalue()


def chart(run: Run, symbol: str) -> dict:
    """Candles of one stock over the run, with both averages and every trade on it. Cached per run."""
    if symbol in run.chart_cache:
        return run.chart_cache[symbol]
    p, s = run.params, load_settings()
    inst = instruments().get(symbol)
    if not inst:
        raise KiteError(f"{symbol} isn't on Kite's NSE list")
    candles = fetch(brokers.client_for(run.user), inst.token, p.tf, warmup_start(p.tf, p.start, p.rules), p.end, s, now_ist())
    fast, slow = p.rules.averages(candles)
    first = next((i for i, c in enumerate(candles) if c.start.astimezone(IST).date() >= p.start), 0)
    daily = p.tf == "1d"

    def t(c: Candle):
        return c.start.astimezone(IST).date().isoformat() if daily else int(c.start.timestamp()) + 19800

    data = {
        "daily": daily,
        "candles": [{"time": t(c), "open": c.open, "high": c.high, "low": c.low, "close": c.close} for c in candles[first:]],
        "fast": [{"time": t(c), "value": round(v, 4)} for c, v in zip(candles[first:], fast[first:]) if v is not None],
        "slow": [{"time": t(c), "value": round(v, 4)} for c, v in zip(candles[first:], slow[first:]) if v is not None],
        "fast_label": f"{p.rules.fast_len} {crossover.MA_KINDS[p.rules.fast_kind]}",
        "slow_label": f"{p.rules.slow_len} {crossover.MA_KINDS[p.rules.slow_kind]}",
    }
    run.chart_cache.clear()  # one stock at a time is plenty
    run.chart_cache[symbol] = data
    return data


def chart_time(run: Run, iso: str):
    """A trade's time as the chart's time axis has it."""
    at = datetime.fromisoformat(iso).astimezone(IST)
    return at.date().isoformat() if run.params.tf == "1d" else int(at.timestamp()) + 19800
