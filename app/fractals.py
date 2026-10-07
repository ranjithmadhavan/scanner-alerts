"""Fractal levels and what happens when price comes back to them.

A fractal is three completed candles (the rule used in algo-nisha):
* fractal high — the middle candle's high is >= the highs on either side;
* fractal low  — the middle candle's low is <= the lows on either side.
It exists once the third candle has closed and stays *unmitigated* until a later candle
trades beyond it. That candle decides what is reported:

* touch  — price traded beyond the level;
* reject  — the candle that took the level closed back on the near side (a sweep);
* confirm — a sweep, and the very next candle also closes on the near side (a sweep that holds:
            neither candle closes beyond the level);
* fail    — it closed beyond the level, and the next two candles both closed back (a failed break).

A fractal high starts out as resistance (taking it reads as a potential sell) and a fractal
low as support (a potential buy). A candle that *opens* beyond the level, a gap, reports
nothing but flips the level's role: a fractal low that price has gapped below is now overhead
and acts as resistance, so price coming back up to it is a potential sell; a fractal high
gapped above becomes support. The target of a signal is the nearest unmitigated level playing
the opposite role on the far side of price.

A fractal taken too soon is dropped silently: an alert can ask for a minimum number of candles
(of the fractal's own timeframe) between the fractal and the candle that takes it.

The candles that decide a sweep or a failed break don't have to be the ones the fractal was
found on: fractals on 30-minute candles can be judged on 5-minute closes (the "trigger candle").
So the work is split: find() reads fractals off one set of candles, replay() runs any candles
past them.

Everything here is pure: it works on lists of completed candles, oldest first.
"""

from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, field, replace
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
    "confirm": "Swept, and the next candle also closes back",
    "fail": "Closes beyond, next two candles close back",
}
SIDES = {"both": "Highs and lows", "high": "Fractal highs only", "low": "Fractal lows only"}
# Candles of the fractal timeframe that must sit between a fractal and the candle that takes it.
MIN_BETWEEN_CHOICES = [0, 2, 3, 4, 5, 6, 8, 10]
DEFAULT_MIN_BETWEEN = 5
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
    index: int = field(default=-1, compare=False)  # position of the middle candle among the candles it was found on
    flipped: bool = False  # price gapped through it, so it now plays the opposite role

    @property
    def key(self) -> str:
        return f"{self.side}:{self.at.isoformat()}" + (":flipped" if self.flipped else "")

    @property
    def role(self) -> str:
        """"resistance": price is under it and taking it means trading above. "support": the mirror."""
        return "resistance" if (self.side == "high") != self.flipped else "support"

    def flip(self) -> "Fractal":
        return replace(self, flipped=not self.flipped)

    def traded_beyond(self, c: Candle) -> bool:
        return c.high > self.level if self.role == "resistance" else c.low < self.level

    def is_beyond(self, price: float) -> bool:
        return price > self.level if self.role == "resistance" else price < self.level


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
        return "sell" if self.fractal.role == "resistance" else "buy"


def target_for(signal: str, price: float, unmitigated: list[Fractal]) -> Fractal | None:
    """Where a trade would be aiming: the nearest unmitigated support below price for a sell,
    the nearest unmitigated resistance above price for a buy."""
    if signal == "sell":
        below = [f for f in unmitigated if f.role == "support" and f.level < price]
        return max(below, key=lambda f: f.level, default=None)
    above = [f for f in unmitigated if f.role == "resistance" and f.level > price]
    return min(above, key=lambda f: f.level, default=None)


def find(candles: list[Candle], end: Callable[[Candle], datetime]) -> list[tuple[datetime, Fractal]]:
    """Every fractal in `candles` with the moment it came into being: the close of its third candle
    (`end` gives a candle's closing time). In that order."""
    found = []
    for i, (before, middle, after) in enumerate(zip(candles, candles[1:], candles[2:]), start=1):
        if middle.high >= before.high and middle.high >= after.high:
            found.append((end(after), Fractal("high", middle.high, middle.start, i)))
        if middle.low <= before.low and middle.low <= after.low:
            found.append((end(after), Fractal("low", middle.low, middle.start, i)))
    return found


def candles_between(f: Fractal, when: datetime, closes: list[datetime]) -> int:
    """How many candles of the fractal's timeframe sit between its middle candle and the one in which
    `when` falls. `closes` are the closing times of the candles the fractal was found on. The third
    candle of the fractal is one of them, so the soonest a fractal can be taken is with 1 between."""
    return bisect_right(closes, when) - f.index - 1


def replay(found: list[tuple[datetime, Fractal]], candles: list[Candle], closes: list[datetime] | None = None,
           min_between: int = 0) -> tuple[list[Hit], list[Fractal]]:
    """Run completed candles (of any size, oldest first) past the fractals. A fractal is in play for
    candles that start at or after it came into being. With `closes` (see candles_between) and
    `min_between`, a fractal taken sooner than that many candles after it formed is retired silently.
    Returns every hit in order, and the fractals still unmitigated at the end."""
    hits: list[Hit] = []
    active: list[Fractal] = []
    broke: list[Fractal] = []  # closed beyond on the previous candle
    returning: list[Fractal] = []  # broke two candles ago and closed back on the previous one; this candle decides
    swept: list[Fractal] = []  # swept by the previous candle; this candle decides if the sweep holds
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
        # A failed break needs two closes back in a row after the close beyond.
        new = [Hit(f, "fail", c, j, c.close) for f in returning if not f.is_beyond(c.close)]
        new += [Hit(f, "confirm", c, j, c.close) for f in swept if not f.is_beyond(c.close)]
        returning = [f for f in broke if not f.is_beyond(c.close)]
        broke, swept, still = [], [], []
        for f in active:
            if f.is_beyond(c.open):
                f = f.flip()  # gapped through: nothing to report, but the level now plays the other role
            if not f.traded_beyond(c):
                still.append(f)
            elif min_between and closes is not None and candles_between(f, c.start, closes) < min_between:
                pass  # taken too soon after it formed to count
            else:
                new.append(Hit(f, "touch", c, j, f.level))
                if f.is_beyond(c.close):
                    broke.append(f)
                else:
                    new.append(Hit(f, "reject", c, j, c.close))
                    swept.append(f)
        active = still
        for hit in new:
            hit.target = target_for(hit.signal, hit.price, active)
        hits.extend(new)
    bring_in(None)
    return hits, active


def walk(candles: list[Candle], min_between: int = 0) -> tuple[list[Hit], list[Fractal]]:
    """find() and replay() on the same candles: a fractal is in play from the candle after its third."""
    def end(c: Candle) -> datetime:
        return c.start + timedelta(microseconds=1)
    return replay(find(candles, end), candles, [end(c) for c in candles], min_between)


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
    of the candle that took the fractal, including the candle before it for a sweep that held and the
    two candles before it for a failed break. Whichever
    of stop and target is traded first decides the result; if one candle trades both, the stop counts,
    because candles don't say which came first. The search for the target carries on past a stop."""
    sell = hit.signal == "sell"
    span = {"fail": 3, "confirm": 2}.get(hit.trigger, 1)  # candles that made up the signal
    made = candles[max(0, hit.index - span + 1):hit.index + 1] if hit.index >= 0 else [hit.candle]
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


def at_day_extreme(hit: Hit, candles: list[Candle]) -> bool:
    """Is the fractal behind this hit a day's high or low? A fractal high counts when it is the high of the
    session it formed in, as that session stood until the candles that made this signal (so for an earlier
    day, that day's high; for today, the high so far); a fractal low, the low. `candles` must reach back to
    the fractal's session; a live touch (index -1) brings its own candle, after them."""
    span = {"fail": 3, "confirm": 2}.get(hit.trigger, 1)
    cutoff = candles[max(0, hit.index - span + 1)].start if hit.index >= 0 else hit.candle.start
    day = hit.fractal.at.date()
    session = [c for c in candles if c.start.date() == day and c.start < cutoff]
    if not session:
        return False
    if hit.fractal.side == "high":
        return hit.fractal.level >= max(c.high for c in session)
    return hit.fractal.level <= min(c.low for c in session)


def last_sessions(candles: list[Candle], sessions: int) -> list[Candle]:
    """Keep only the most recent `sessions` trading days of candles."""
    days = sorted({c.start.date() for c in candles})[-sessions:]
    return [c for c in candles if c.start.date() >= days[0]] if days else []
