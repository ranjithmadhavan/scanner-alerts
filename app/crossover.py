"""Moving-average crossover setups, and how they would have gone as trades.

Two moving averages, each an EMA or SMA of closes with its own period. When the fast one closes above the
slow one the trend is up and buys are looked for; below, sells. Until the opposite crossover:

* a pullback candle is a red candle in an uptrend (close below open), a green one in a downtrend. With
  `touch`, it must also reach the chosen moving average: its low at or below it for a buy, its high at
  or above it for a sell. The crossover candle itself is never the pullback;
* the latest pullback candle is the *trigger candle*: each new one replaces the one before;
* a later candle trading beyond it (above its high for a buy, below its low for a sell) is the entry.
  When that candle opens beyond the level, the entry came on a *gap*;
* with `once` (the default), a crossover gives one entry: after it, nothing more is looked for until the
  next crossover, however that trade ends; with `second` as well, one more if the first was stopped out. Without it, the next pullback candle in the same trend sets up
  the next entry. The opposite crossover cancels a setup that hasn't been entered.

The trend is read from where the averages stand once both exist. With `once`, setups wait for a crossover
inside the candles, since there is no telling whether an earlier one had already given its entry; without
it, a crossover from before the candles start still counts. Everything here is pure: it works on completed
candles, oldest first.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import time, timedelta

from app.kite import Candle

MA_KINDS = {"ema": "EMA", "sma": "SMA"}
TOUCH = {"": "Any pullback candle", "fast": "Must touch the fast MA", "slow": "Must touch the slow MA"}
SIDES = {"both": "Buys and sells", "buy": "Buys only", "sell": "Sells only"}
STOPS = {"candle": "Trigger candle low / high", "slow_ma": "Slow MA (exit on a close beyond it)"}
TRAILS = {"": "No trail", "candle": "Previous candle's low / high", "fast_ma": "Fast MA (exit on a close beyond it)",
          "step": "Step: lift the stop 1R for every 1R gained"}
FILLS = {"open": "At the open", "level": "At the trigger level", "skip": "Skip the trade"}


def sma(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def ema(values: list[float], n: int) -> list[float | None]:
    """Seeded with the SMA of the first n closes, the usual way."""
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def average(kind: str, values: list[float], n: int) -> list[float | None]:
    return ema(values, n) if kind == "ema" else sma(values, n)


@dataclass(frozen=True)
class Rules:
    fast_kind: str = "ema"
    fast_len: int = 9
    slow_kind: str = "ema"
    slow_len: int = 21
    touch: str = ""  # "", "fast" or "slow": the moving average a pullback candle must reach
    sides: str = "both"
    once: bool = True  # one entry per crossover...
    second: bool = False  # ...or, with `once`, a second one if the first was stopped out (decided in trades(), not walk())

    @property
    def label(self) -> str:
        """'9 EMA / 21 SMA'"""
        return f"{self.fast_len} {MA_KINDS[self.fast_kind]} / {self.slow_len} {MA_KINDS[self.slow_kind]}"

    @property
    def summary(self) -> str:
        """'Must touch the fast MA · buys only · first entry per crossover, a second after a stop-out'"""
        entries = ("first entry per crossover" + (", a second after a stop-out" if self.second else "")) if self.once \
            else "re-entries in the same trend"
        return f"{TOUCH[self.touch]} · {SIDES[self.sides].lower()} · {entries}"

    def warmup(self) -> int:
        """Candles to read before the first one that matters, so an EMA has settled."""
        return max(self.fast_len, self.slow_len) * 3 + 10

    def averages(self, candles: list[Candle]) -> tuple[list[float | None], list[float | None]]:
        closes = [c.close for c in candles]
        return average(self.fast_kind, closes, self.fast_len), average(self.slow_kind, closes, self.slow_len)

    def prepare(self, candles: list[Candle]) -> "Prepared":
        fast, slow = self.averages(candles)
        return Prepared(walk(candles, self, fast, slow), fast, slow)

    def series(self, candles: list[Candle]) -> dict:
        """What a chart draws besides the candles: both averages over them."""
        fast, slow = self.averages(candles)
        return {"overlays": [{"label": f"{self.fast_len} {MA_KINDS[self.fast_kind]}", "color": "#E9A23B", "values": fast},
                             {"label": f"{self.slow_len} {MA_KINDS[self.slow_kind]}", "color": "#0F5E5C", "values": slow}],
                "panel": None}


@dataclass(frozen=True)
class Setup:
    side: str  # "buy" | "sell"
    trigger: int  # index of the trigger (pullback) candle
    level: float  # its high for a buy, its low for a sell: the entry is a trade beyond it
    stop: float  # its low for a buy, its high for a sell
    crossed: int | None  # index of the crossover candle; None when it came before the candles start


@dataclass(frozen=True)
class Entry:
    setup: Setup
    index: int  # the candle that traded beyond the level
    gap: bool  # that candle opened beyond it

    @property
    def side(self) -> str:
        return self.setup.side


@dataclass
class Walk:  # what a strategy's walk over the candles found (crossovers: when each trending phase began)
    entries: list[Entry] = field(default_factory=list)
    crossovers: list[tuple[int, str]] = field(default_factory=list)  # (candle index, "buy" | "sell")
    pending: Setup | None = None  # waiting for its entry after the last candle
    trend: str | None = None  # "buy" while fast is above slow, "sell" while below


def walk(candles: list[Candle], rules: Rules, fast: list | None = None, slow: list | None = None) -> Walk:
    if fast is None or slow is None:
        fast, slow = rules.averages(candles)
    touch_ma = fast if rules.touch == "fast" else slow if rules.touch == "slow" else None
    out = Walk()
    crossed: int | None = None
    setup: Setup | None = None
    used = False  # this crossover has given its entry
    for i, c in enumerate(candles):
        entered = False
        if setup and (c.high > setup.level if setup.side == "buy" else c.low < setup.level):
            gap = c.open > setup.level if setup.side == "buy" else c.open < setup.level
            out.entries.append(Entry(setup, i, gap))
            setup, entered, used = None, True, True
        f, s = fast[i], slow[i]
        if f is None or s is None or f == s:
            continue
        now = "buy" if f > s else "sell"
        if now != out.trend:
            if out.trend is not None:  # a real crossover, not the first reading
                crossed = i
                out.crossovers.append((i, now))
            out.trend, setup, used = now, None, False
            continue  # the crossover candle is never the pullback
        # With a second chance, walk() lists every entry of the crossover; trades() keeps the ones allowed.
        if entered or rules.sides not in ("both", now) or (rules.once and (crossed is None or (used and not rules.second))):
            continue
        if now == "buy" and c.close < c.open and (touch_ma is None or (touch_ma[i] is not None and c.low <= touch_ma[i])):
            setup = Setup("buy", i, c.high, c.low, crossed)
        elif now == "sell" and c.close > c.open and (touch_ma is None or (touch_ma[i] is not None and c.high >= touch_ma[i])):
            setup = Setup("sell", i, c.low, c.high, crossed)
    out.pending = setup
    return out


# ---- trades -------------------------------------------------------------------------------

@dataclass(frozen=True)
class Exits:
    stop: str = "candle"  # key of STOPS
    rr: float = 2.0  # target at this many times the risk
    book: int = 100  # % of the position booked at the target (0: no target)
    trail: str = ""  # key of TRAILS, for what is left after the target
    breakeven: bool = False  # move the stop to entry at 1R; a trail starts only then
    ma_exit: bool = False  # exit everything when a candle closes beyond the slow MA
    fill: str = "open"  # key of FILLS: where a gap entry fills
    square_off: time | None = None  # intraday: close what is left at the last candle before this time
    trend_exit: bool = False  # close what is left when the strategy's trend ends (ADX trend)
    expiry_exit: bool = False  # close what is left at the monthly expiry's close (ADX trend)


@dataclass(frozen=True)
class Fill:
    part: float  # share of the position, 0..1
    price: float
    index: int
    reason: str  # stop | breakeven | trail | target | slow_ma | ma_exit | fast_ma | square_off


EXIT_NAMES = {"stop": "SL", "breakeven": "Breakeven", "trail": "Trailing SL", "target": "Target", "slow_ma": "Closed beyond slow MA",
              "ma_exit": "Closed beyond slow MA", "fast_ma": "Closed beyond fast MA", "square_off": "Squared off",
              "trend_end": "Trend ended", "expiry": "Expiry close"}


@dataclass
class Trade:
    entry: Entry
    status: str  # "closed" | "open" | "skipped"
    price: float = 0.0  # entry price
    stop: float = 0.0  # initial stop (the slow MA's value at entry, for an MA stop)
    risk: float = 0.0  # 1R, in price
    target: float | None = None
    fills: list[Fill] = field(default_factory=list)
    note: str = ""  # why a trade was skipped

    @property
    def side(self) -> str:
        return self.entry.side

    @property
    def sign(self) -> int:
        return 1 if self.side == "buy" else -1

    @property
    def r(self) -> float | None:
        """Result in multiples of the risk, once closed."""
        if self.status != "closed":
            return None
        return sum(f.part * (f.price - self.price) * self.sign for f in self.fills) / self.risk

    @property
    def pct(self) -> float | None:
        if self.status != "closed":
            return None
        return sum(f.part * (f.price - self.price) * self.sign for f in self.fills) / self.price * 100

    @property
    def exit_index(self) -> int | None:
        return self.fills[-1].index if self.fills and self.status == "closed" else None

    @property
    def exit_price(self) -> float | None:
        """Average exit price over the parts."""
        if self.status != "closed":
            return None
        return sum(f.part * f.price for f in self.fills)

    @property
    def reason(self) -> str:
        return " + ".join(EXIT_NAMES[f.reason] for f in self.fills)


def _beyond(price: float, level: float, buy: bool) -> bool:
    """Is `price` on the losing side of a stop at `level`?"""
    return price < level if buy else price > level


def _tighten(stop: float | None, to: float, buy: bool) -> float:
    return to if stop is None else (max(stop, to) if buy else min(stop, to))


def _last_before(candles: list[Candle], j: int, at: time, minutes: int) -> bool | None:
    """Is candle j the last of its day that starts before `at`? None if the candles stop mid-day before it."""
    c = candles[j]
    if c.start.time() >= at:
        return False
    if j + 1 < len(candles):
        n = candles[j + 1]
        return n.start.date() != c.start.date() or n.start.time() >= at
    return True if (c.start + timedelta(minutes=minutes)).time() >= at else None


def simulate(entry: Entry, candles: list[Candle], fast: list, slow: list, x: Exits, minutes: int = 0,
             ends: Callable[[int, "Entry", Exits], str | None] | None = None) -> Trade:
    """Follow an entry through the candles. Prices are checked in this order on each candle: the stop (a gap
    through it fills at the open), then the target (a gap past it fills at the open), then the exits on a
    close. A candle that trades both the stop and the target counts as the stop. The entry candle is read the
    same cautious way: trading beyond the stop counts as stopped even though the low (for a buy) may have come
    before the entry, while the target counts only if the candle closed beyond it. (A candle-by-candle replay
    can't tell; on a random walk this keeps results a little under zero rather than well above it.) Entered at
    the open on a gap, the whole candle comes after the entry anyway. Stops and trails move at each close, for
    the next candle."""
    buy, k = entry.side == "buy", entry.index
    sign = 1 if buy else -1
    c0 = candles[k]
    if entry.gap and x.fill == "skip":
        return Trade(entry, "skipped", note="opened beyond the level (gap)")
    if x.square_off and minutes and c0.start.time() >= x.square_off:
        return Trade(entry, "skipped", note="after the square-off time")
    at_open = entry.gap and x.fill == "open"
    price = c0.open if at_open else entry.setup.level
    stop = entry.setup.stop if x.stop == "candle" else slow[k - 1] if k > 0 else None
    if stop is None or (price - stop) * sign <= 0:
        return Trade(entry, "skipped", note="entry is not above the slow MA" if buy else "entry is not below the slow MA")
    risk = (price - stop) * sign
    target = price + sign * x.rr * risk if x.book > 0 and x.rr > 0 else None
    t = Trade(entry, "open", price, stop, risk, target)
    price_stop: float | None = stop if x.stop == "candle" else None
    left, booked, armed, best = 1.0, False, not x.breakeven, price
    intraday = bool(x.square_off and minutes)

    def out(part: float, px: float, j: int, reason: str) -> None:
        nonlocal left
        t.fills.append(Fill(part, px, j, reason))
        left -= part

    for j in range(k, len(candles)):
        c = candles[j]
        whole = j > k or at_open  # the entire candle came after the entry
        if price_stop is not None and _beyond(c.low if buy else c.high, price_stop, buy):
            px = c.open if j > k and _beyond(c.open, price_stop, buy) else price_stop
            out(left, px, j, "stop" if price_stop == stop else "breakeven" if price_stop == price else "trail")
            break
        if target is not None and not booked and ((c.high >= target if buy else c.low <= target) if whole
                                                   else (c.close >= target if buy else c.close <= target)):
            px = (max(c.open, target) if buy else min(c.open, target)) if j > k else target
            out(left if x.book >= 100 else x.book / 100, px, j, "target")
            booked = True
            if left <= 1e-9:
                break
        reason = None
        if slow[j] is not None and _beyond(c.close, slow[j], buy) and (x.stop == "slow_ma" or x.ma_exit):
            reason = "slow_ma" if x.stop == "slow_ma" else "ma_exit"
        elif x.trail == "fast_ma" and armed and fast[j] is not None and _beyond(c.close, fast[j], buy):
            reason = "fast_ma"
        elif ends and (own := ends(j, entry, x)):  # the strategy's own exits on a close
            reason = own
        elif intraday:
            last = _last_before(candles, j, x.square_off, minutes)
            if last is None:
                break  # the day is still in progress
            reason = "square_off" if last else None
        if reason:
            out(left, c.close, j, reason)
            break
        # Move the stop for the next candle.
        best = max(best, c.high if whole else c.close) if buy else min(best, c.low if whole else c.close)
        gained = (best - price) * sign / risk
        if x.breakeven and gained >= 1:
            armed = True
            price_stop = _tighten(price_stop, price, buy)
        if armed and x.trail == "candle":
            price_stop = _tighten(price_stop, c.low if buy else c.high, buy)
        if x.trail == "step" and gained >= 1:
            price_stop = _tighten(price_stop, price + sign * (math.floor(gained + 1e-9) - 1) * risk, buy)
    t.status = "closed" if left <= 1e-9 else "open"
    return t


def stopped_out(t: Trade) -> bool:
    """Did the trade end at its SL (the trigger candle's, or the slow MA when that is the SL) with nothing booked,
    at a loss? With the slow MA as SL, a close beyond it after a good run is the trade's way out, not a stop-out:
    it doesn't earn a second entry, and neither does anything that ended in profit or at breakeven."""
    return t.status == "closed" and all(f.reason in ("stop", "slow_ma") for f in t.fills) and t.r < 0


@dataclass
class Prepared:
    """A strategy's reading of some candles: its walk, the two averages the MA exits read (all None for a strategy
    without them), and its own exits on a close, if any."""
    walk: Walk
    fast: list
    slow: list
    ends: Callable[[int, Entry, Exits], str | None] | None = None


def _second_chances(w: Walk, candles: list[Candle], fast: list, slow: list, x: Exits, minutes: int,
                    ends=None) -> list[Trade]:
    """One position at a time, from the first candle: a crossover's first entry, and a second only once the first
    has been stopped out. Never a third."""
    out: list[Trade] = []
    per: dict[int | None, list[Trade]] = {}
    free_from = 0
    for e in w.entries:
        before = per.get(e.setup.crossed, [])
        if e.index < free_from or len(before) >= 2 or (before and not stopped_out(before[-1])):
            continue
        t = simulate(e, candles, fast, slow, x, minutes, ends)
        out.append(t)
        per.setdefault(e.setup.crossed, []).append(t)
        if t.status == "open":
            break
        if t.status == "closed":
            free_from = t.exit_index + 1
    return out


def scan(candles: list[Candle], rules, x: Exits) -> tuple[list[Entry], Setup | None, list, list]:
    """For a live scanner: the entries to report and the setup still waiting for its entry, with both averages.
    With a second chance, the first entry is followed as a trade (with `x`) to know whether it was stopped out,
    and nothing is set up while it is still running. `rules` is any strategy's (MA crossover, ADX trend)."""
    p = rules.prepare(candles)
    w, fast, slow = p.walk, p.fast, p.slow
    if not (rules.once and rules.second):
        return w.entries, w.pending, fast, slow
    taken = _second_chances(w, candles, fast, slow, x, 0, p.ends)
    pending = w.pending
    if pending:
        before = [t for t in taken if t.entry.setup.crossed == pending.crossed]
        if (taken and taken[-1].status == "open") or len(before) >= 2 or (before and not stopped_out(before[0])):
            pending = None
    return [t.entry for t in taken], pending, fast, slow


def trades(candles: list[Candle], rules, x: Exits, minutes: int = 0, start: int = 0) -> tuple[list[Trade], Walk]:
    """Every entry from candle `start` on, one position at a time: an entry while a trade is still open is
    passed over, and an open trade at the end blocks everything after it. With a second chance after a stop-out,
    trades before `start` are followed too, since they decide what is allowed after it. `rules` is any strategy's."""
    p = rules.prepare(candles)
    w, fast, slow = p.walk, p.fast, p.slow
    if rules.once and rules.second:
        return [t for t in _second_chances(w, candles, fast, slow, x, minutes, p.ends) if t.entry.index >= start], w
    out: list[Trade] = []
    free_from = start
    for e in w.entries:
        if e.index < free_from:
            continue
        t = simulate(e, candles, fast, slow, x, minutes, p.ends)
        out.append(t)
        if t.status == "open":
            break
        if t.status == "closed":
            free_from = t.exit_index + 1
    return out, w


def stats(closed: list[Trade]) -> dict:
    """Totals for closed trades, taken in the order they closed (drawdown is in R on that running total)."""
    rs = [t.r for t in closed]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    running = peak = drawdown = 0.0
    for r in rs:
        running += r
        peak = max(peak, running)
        drawdown = max(drawdown, peak - running)
    return {
        "trades": len(rs), "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(rs) * 100 if rs else None,
        "total_r": sum(rs), "avg_r": sum(rs) / len(rs) if rs else None,
        "avg_win": sum(wins) / len(wins) if wins else None, "avg_loss": sum(losses) / len(losses) if losses else None,
        "profit_factor": sum(wins) / -sum(losses) if losses and sum(losses) < 0 else None,
        "max_dd": drawdown, "total_pct": sum(t.pct for t in closed),
    }
