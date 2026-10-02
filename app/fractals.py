"""Fractal levels and what happens when price comes back to them.

A fractal is three completed candles (the rule used in algo-nisha):
* fractal high — the middle candle's high is >= the highs on either side;
* fractal low  — the middle candle's low is <= the lows on either side.
It exists once the third candle has closed and stays *unmitigated* until a later candle
trades beyond it. That candle decides what is reported:

* touch  — price traded beyond the level;
* reject — the candle that took the level closed back on the near side (a sweep);
* fail   — it closed beyond the level, and the very next candle closed back (a failed break).

A candle that *opens* beyond the level (a gap) mitigates it silently: nothing is reported.
Taking a fractal high reads as a potential sell, a fractal low as a potential buy, and the
target is the nearest unmitigated fractal on the other side of price.

The candles that decide a sweep or a failed break don't have to be the ones the fractal was
found on: fractals on 30-minute candles can be judged on 5-minute closes (the "trigger candle").
So the work is split: find() reads fractals off one set of candles, replay() runs any candles
past them.

Everything here is pure: it works on lists of completed candles, oldest first.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.kite import TIMEFRAMES as CANDLES
from app.kite import Candle

# timeframe -> (label, trading sessions of history searched for fractals)
TIMEFRAMES: dict[str, tuple[str, int]] = {
    "3m": ("3 min", 10),
    "5m": ("5 min", 10),
    "10m": ("10 min", 10),
    "15m": ("15 min", 10),
    "30m": ("30 min", 10),
    "1h": ("1 hour", 20),
    "1d": ("Daily", 120),
}
DEFAULT_TIMEFRAME = "30m"
TRIGGERS = {
    "touch": "Price trades through it",
    "reject": "Swept, candle closes back",
    "fail": "Closes beyond, next candle closes back",
}
SIDES = {"both": "Highs and lows", "high": "Fractal highs only", "low": "Fractal lows only"}
SESSION_MINUTES = 375  # what a daily candle counts as when comparing timeframes
# Kite serves this many days of each candle size per request; a backtest on small trigger candles is capped by it.
MAX_DAYS = {"1m": 60, "3m": 100, "5m": 100, "10m": 100, "15m": 200, "30m": 200, "1h": 400, "1d": 2000}


def minutes(tf: str) -> int:
    return CANDLES[tf][1] or SESSION_MINUTES


def label(tf: str) -> str:
    return TIMEFRAMES[tf][0] if tf in TIMEFRAMES else tf.replace("m", " min")


def trigger_choices(tf: str) -> list[str]:
    """Candle sizes a sweep or failed break can be judged on for fractals found on `tf`: shorter
    ones that fit into it a whole number of times (any intraday size for daily fractals)."""
    return [c for c in CANDLES if c != "1d" and minutes(c) < minutes(tf) and (tf == "1d" or minutes(tf) % minutes(c) == 0)]


@dataclass(frozen=True)
class Fractal:
    side: str  # "high" | "low"
    level: float
    at: datetime  # start of the middle candle

    @property
    def key(self) -> str:
        return f"{self.side}:{self.at.isoformat()}"

    def traded_beyond(self, c: Candle) -> bool:
        return c.high > self.level if self.side == "high" else c.low < self.level

    def is_beyond(self, price: float) -> bool:
        return price > self.level if self.side == "high" else price < self.level


@dataclass
class Hit:
    fractal: Fractal
    trigger: str  # key of TRIGGERS
    candle: Candle  # the candle that produced it
    index: int  # its position in the candle list
    price: float  # the level for a touch, the candle's close otherwise
    target: Fractal | None = None

    @property
    def key(self) -> str:
        return f"{self.fractal.key}:{self.trigger}"

    @property
    def signal(self) -> str:
        return "sell" if self.fractal.side == "high" else "buy"


def target_for(side: str, price: float, unmitigated: list[Fractal]) -> Fractal | None:
    """Where a trade off a `side` fractal would be aiming: the nearest unmitigated fractal low below
    price after a fractal high is taken (sell), the nearest fractal high above after a low (buy)."""
    if side == "high":
        below = [f for f in unmitigated if f.side == "low" and f.level < price]
        return max(below, key=lambda f: f.level, default=None)
    above = [f for f in unmitigated if f.side == "high" and f.level > price]
    return min(above, key=lambda f: f.level, default=None)


def find(candles: list[Candle], end: Callable[[Candle], datetime]) -> list[tuple[datetime, Fractal]]:
    """Every fractal in `candles` with the moment it came into being: the close of its third candle
    (`end` gives a candle's closing time). In that order."""
    found = []
    for before, middle, after in zip(candles, candles[1:], candles[2:]):
        if middle.high >= before.high and middle.high >= after.high:
            found.append((end(after), Fractal("high", middle.high, middle.start)))
        if middle.low <= before.low and middle.low <= after.low:
            found.append((end(after), Fractal("low", middle.low, middle.start)))
    return found


def replay(found: list[tuple[datetime, Fractal]], candles: list[Candle]) -> tuple[list[Hit], list[Fractal]]:
    """Run completed candles (of any size, oldest first) past the fractals. A fractal is in play for
    candles that start at or after it came into being. Returns every hit in order, and the fractals
    still unmitigated at the end."""
    hits: list[Hit] = []
    active: list[Fractal] = []
    broke: list[Fractal] = []  # closed beyond on the previous candle; this candle decides if the break fails
    pending = 0

    def bring_in(upto: datetime | None) -> None:
        nonlocal pending
        while pending < len(found) and (upto is None or found[pending][0] <= upto):
            f = found[pending][1]
            pending += 1
            if not any((a.side, a.level) == (f.side, f.level) for a in active):  # a flat run of equal highs is one level
                active.append(f)

    for j, c in enumerate(candles):
        bring_in(c.start)
        new = [Hit(f, "fail", c, j, c.close) for f in broke if not f.is_beyond(c.close)]
        broke, still = [], []
        for f in active:
            if not f.traded_beyond(c):
                still.append(f)
            elif f.is_beyond(c.open):
                pass  # gapped through: mitigated without a word
            else:
                new.append(Hit(f, "touch", c, j, f.level))
                if f.is_beyond(c.close):
                    broke.append(f)
                else:
                    new.append(Hit(f, "reject", c, j, c.close))
        active = still
        for hit in new:
            hit.target = target_for(hit.fractal.side, hit.price, active)
        hits.extend(new)
    bring_in(None)
    return hits, active


def walk(candles: list[Candle]) -> tuple[list[Hit], list[Fractal]]:
    """find() and replay() on the same candles: a fractal is in play from the candle after its third."""
    return replay(find(candles, lambda c: c.start + timedelta(microseconds=1)), candles)


@dataclass
class Outcome:
    """How a hit would have gone as a trade, for backtests."""
    entry: float  # the fractal level for a touch, the deciding candle's close otherwise
    target: float | None
    stop: float  # the extreme price made while the fractal was being taken; beyond it the idea has failed
    stopped: Candle | None  # first later candle that traded beyond the stop
    reached: Candle | None  # first later candle that traded to the target
    result: str  # "target" | "stop" | "open" | "none" (no target to aim at, stop not hit)

    @property
    def reached_after_stop(self) -> bool:
        return self.result == "stop" and self.reached is not None

    @property
    def points(self) -> float | None:
        """Points made (entry to target) or lost (entry to stop). None while the trade is open, or when
        there was no target and so no trade to take."""
        if self.target is None or self.result not in ("target", "stop"):
            return None
        return abs(self.entry - self.target) if self.result == "target" else -abs(self.stop - self.entry)


def outcome(hit: Hit, candles: list[Candle]) -> Outcome:
    """Follow a hit through the candles after it. The stop is the high (for a sell) or low (for a buy)
    of the candle that took the fractal, including the candle before it for a failed break. Whichever
    of stop and target is traded first decides the result; if one candle trades both, the stop counts,
    because candles don't say which came first. The search for the target carries on past a stop."""
    sell = hit.signal == "sell"
    made = [hit.candle] + ([candles[hit.index - 1]] if hit.trigger == "fail" and hit.index > 0 else [])
    stop = max(c.high for c in made) if sell else min(c.low for c in made)
    target = hit.target.level if hit.target else None
    stopped = reached = None
    for c in candles[hit.index + 1:]:
        if stopped is None and (c.high > stop if sell else c.low < stop):
            stopped = c
        if reached is None and target is not None and (c.low <= target if sell else c.high >= target):
            reached = c
        if stopped and (reached or target is None):
            break
    if stopped and (reached is None or stopped.start <= reached.start):
        result = "stop"
    elif reached:
        result = "target"
    else:
        result = "open" if hit.target else "none"
    return Outcome(hit.price, target, stop, stopped, reached, result)


def last_sessions(candles: list[Candle], sessions: int) -> list[Candle]:
    """Keep only the most recent `sessions` trading days of candles."""
    days = sorted({c.start.date() for c in candles})[-sessions:]
    return [c for c in candles if c.start.date() >= days[0]] if days else []
