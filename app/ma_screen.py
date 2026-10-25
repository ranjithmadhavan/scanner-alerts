"""MA crossover "Scan now": which stocks, at a day's close, have just triggered, have just formed a trigger candle,
or have one still waiting for its entry. Run by hand, in the background like a backtest (same slower lane of Kite
requests), one per person at a time, and kept in memory until the next run or a restart.

For each stock the candles up to the end of the chosen day are read (as many as a live scanner reads, so the
crossover behind a setup is in them) and app.crossover.scan() gives the entries and the setup still waiting:

* triggered: an entry on a candle of that day (on a gap too: the day opened beyond the level);
* formed: the setup waiting for its entry has its trigger candle on that day;
* ready: the setup waiting for its entry is from an earlier day.
"""

import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app import brokers, crossover, kite, ma_backtest, ma_scanner, universe
from app.crossover import Rules
from app.kite import Candle, KiteAuthError, KiteError, instruments
from app.market import IST, MarketSettings, is_trading_day, load_settings, now_ist
from app.store import new_id
from app.strategies import STRATEGIES

log = logging.getLogger("ma.screen")

GROUPS = {"triggered": "Triggered", "formed": "Trigger candle formed", "ready": "Ready, waiting for entry"}


def group_names(strategy: str) -> dict[str, str]:
    """The group headings in the strategy's own words (an ADX setup comes from a signal candle)."""
    word = STRATEGIES[strategy].setup_word
    return {**GROUPS, "formed": f"{word} formed"}


def last_session(s: MarketSettings, now: datetime) -> date:
    """The latest trading day whose daily candle has its official close: from 3:45 PM (as live scanners read it), today."""
    day = now.astimezone(IST)
    if not (is_trading_day(day) and day >= s.close_at(day) + ma_scanner.DAILY_READ_AFTER):
        day -= timedelta(days=1)
        while not is_trading_day(day):
            day -= timedelta(days=1)
    return day.date()


@dataclass
class Params:
    spec: dict
    tf: str
    day: date  # as of this day's close
    rules: Rules
    stop: str = "candle"  # for the SL and target shown
    rr: float = 2.0
    strategy: str = "ma"


@dataclass
class Screen:
    id: str
    user: str
    params: Params
    symbols: list[str]
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
    charts: dict = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.symbols)

    def group(self, key: str) -> list[dict]:
        rows = [r for r in self.rows if r["group"] == key]
        # Waiting setups nearest their level first; the rest A-Z.
        return sorted(rows, key=lambda r: (r["away"] if key == "ready" else 0, r["symbol"]))


_screens: dict[tuple[str, str], Screen] = {}  # (person, strategy) -> their latest scan
_lock = threading.Lock()


def current(username: str, strategy: str = "ma") -> Screen | None:
    return _screens.get((username, strategy))


def start(username: str, p: Params) -> Screen:
    symbols = universe.resolve(p.spec, now_ist().date())
    with _lock:
        if (sc := _screens.get((username, p.strategy))) and sc.status == "running":
            raise ValueError("A scan is already running. Wait for it or cancel it first.")
        screen = Screen(new_id(), username, p, symbols, now_ist())
        _screens[(username, p.strategy)] = screen
    threading.Thread(target=_work, args=(screen,), name=f"ma-screen-{username}", daemon=True).start()
    return screen


def cancel(username: str, strategy: str = "ma") -> None:
    if sc := _screens.get((username, strategy)):
        sc.stop.set()


def candles_until(client, token: int, p: Params, s: MarketSettings) -> list[Candle]:
    """Completed candles up to the end of the chosen day, as many as a live scanner reads."""
    count = p.rules.warmup() * ma_scanner.LOOKBACK
    start = max(ma_backtest.warmup_start(p.tf, p.day, p.rules, count), p.day - timedelta(days=ma_backtest.max_days(p.tf) - 1))
    got = ma_backtest.fetch(client, token, p.tf, start, p.day, s, now_ist(), kite.BACKTESTS)
    return [c for c in got if c.start.astimezone(IST).date() <= p.day]


def classify(symbol: str, candles: list[Candle], p: Params) -> dict | None:
    """Where this stock stands at the day's close, or None if it has nothing to show."""
    if not candles:
        return None
    sc = {"stop": p.stop, "rr": p.rr}
    exits = crossover.Exits(stop=p.stop, rr=p.rr, book=100 if p.rr else 0)
    entries, pending, fast, slow = crossover.scan(candles, p.rules, exits)

    def on_day(i: int) -> bool:
        return candles[i].start.astimezone(IST).date() == p.day

    last = candles[-1]
    base = {"symbol": symbol, "close": last.close, "fast": fast[-1], "slow": slow[-1]}
    hit = next((e for e in reversed(entries) if on_day(e.index)), None)
    if hit:
        price = candles[hit.index].open if hit.gap else hit.setup.level
        stop, target = ma_scanner.plan(sc, hit.side, price, hit.setup.stop, slow[hit.index - 1] if hit.index else None)
        return {**base, "group": "triggered", "side": hit.side, "level": hit.setup.level, "price": price, "gap": hit.gap,
                "stop": stop, "target": target, "trigger_at": candles[hit.setup.trigger].start.isoformat(),
                "at": candles[hit.index].start.isoformat(), "away": 0.0}
    if pending:
        stop, target = ma_scanner.plan(sc, pending.side, pending.level, pending.stop, slow[pending.trigger])
        away = (pending.level - last.close) / last.close * 100 * (1 if pending.side == "buy" else -1)
        return {**base, "group": "formed" if on_day(pending.trigger) else "ready", "side": pending.side,
                "level": pending.level, "price": None, "gap": False, "stop": stop, "target": target,
                "trigger_at": candles[pending.trigger].start.isoformat(), "at": None, "away": away}
    return None


def _work(screen: Screen) -> None:
    p, s = screen.params, load_settings()
    try:
        known = instruments()  # inside the try: if the lists can't be had, the run must still end, not hang
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
                    candles = candles_until(client, inst.token, p, s)
                except KiteAuthError:
                    raise
                except KiteError as e:
                    log.warning("ma screen: %s candles failed: %s", symbol, e)
                    screen.failed.append(symbol)
                    candles = None
                if candles is not None:
                    if not candles or candles[-1].start.astimezone(IST).date() != p.day:
                        screen.no_data.append(symbol)  # no candle that day (a holiday, or not traded)
                    elif row := classify(symbol, candles, p):
                        screen.rows.append(row)
            screen.done += 1
        else:
            screen.status = "done"
    except KiteAuthError:
        screen.status, screen.error = "failed", "Kite isn't connected or the session has expired. Log in on the Broker page and scan again."
    except KiteError as e:
        screen.status, screen.error = "failed", str(e)
    except Exception as e:
        log.exception("ma screen %s failed", screen.id)
        screen.status, screen.error = "failed", f"The scan stopped: {e}"
    screen.finished = now_ist()
    log.info("ma screen %s for %s: %s, %d/%d stocks, %d found", screen.id, screen.user, screen.status, screen.done,
             screen.total, len(screen.rows))


def chart(screen: Screen, symbol: str) -> dict:
    """One stock's last 150 candles up to the day, with the strategy's lines and its setup. Cached per scan (one stock)."""
    if symbol in screen.charts:
        return screen.charts[symbol]
    p, s = screen.params, load_settings()
    inst = instruments().get(symbol)
    if not inst:
        raise KiteError(f"{symbol} isn't on Kite's NSE list")
    candles = candles_until(brokers.client_for(screen.user), inst.token, p, s)
    daily = p.tf == "1d"

    def t(at: datetime):
        at = at.astimezone(IST)
        return at.date().isoformat() if daily else int(at.timestamp()) + 19800

    row = next((r for r in screen.rows if r["symbol"] == symbol), None)
    data = {**ma_backtest.chart_data(p.rules, candles, max(0, len(candles) - 150), daily), "symbol": symbol, "row": row,
            "trigger": t(datetime.fromisoformat(row["trigger_at"])) if row else None,
            "entry": t(datetime.fromisoformat(row["at"])) if row and row["at"] else None}
    screen.charts.clear()
    screen.charts[symbol] = data
    return data
