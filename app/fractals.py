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

Everything here is pure: it works on a list of completed candles, oldest first.
"""

from dataclasses import dataclass
from datetime import datetime

from app.kite import Candle

# timeframe -> (label, trading sessions of history searched for fractals)
TIMEFRAMES: dict[str, tuple[str, int]] = {
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


def walk(candles: list[Candle]) -> tuple[list[Hit], list[Fractal]]:
    """Replay completed candles. Returns every hit in order, and the fractals still unmitigated at the end."""
    hits: list[Hit] = []
    active: list[Fractal] = []
    broke: list[Fractal] = []  # closed beyond on the previous candle; this candle decides if the break fails
    for j, c in enumerate(candles):
        found = [Hit(f, "fail", c, j, c.close) for f in broke if not f.is_beyond(c.close)]
        broke, still = [], []
        for f in active:
            if not f.traded_beyond(c):
                still.append(f)
            elif f.is_beyond(c.open):
                pass  # gapped through: mitigated without a word
            else:
                found.append(Hit(f, "touch", c, j, f.level))
                if f.is_beyond(c.close):
                    broke.append(f)
                else:
                    found.append(Hit(f, "reject", c, j, c.close))
        active = still
        for hit in found:
            hit.target = target_for(hit.fractal.side, hit.price, active)
        hits.extend(found)
        if j >= 2:  # this candle completes a fractal around the one before it
            before, middle = candles[j - 2], candles[j - 1]
            new = []
            if middle.high >= before.high and middle.high >= c.high:
                new.append(Fractal("high", middle.high, middle.start))
            if middle.low <= before.low and middle.low <= c.low:
                new.append(Fractal("low", middle.low, middle.start))
            # A flat run of equal highs is one level, not several.
            active.extend(f for f in new if not any((a.side, a.level) == (f.side, f.level) for a in active))
    return hits, active


def target_reached(hit: Hit, candles: list[Candle]) -> Candle | None:
    """For backtests: the first later candle that traded to the hit's target, if any did."""
    if not hit.target:
        return None
    for c in candles[hit.index + 1:]:
        if (c.low <= hit.target.level) if hit.signal == "sell" else (c.high >= hit.target.level):
            return c
    return None


def last_sessions(candles: list[Candle], sessions: int) -> list[Candle]:
    """Keep only the most recent `sessions` trading days of candles."""
    days = sorted({c.start.date() for c in candles})[-sessions:]
    return [c for c in candles if c.start.date() >= days[0]] if days else []
