"""ADX trend: trade only when ADX says the market is trending, in the direction the DMI says (as taught by Sahil
Rohmetra, Upsurge, "ADX + ATR strategy for positional options buying").

* Trending: ADX above `adx_min` (25). Not trending, no trade.
* Up: +DI above `di_min` (25) and -DI below `di_max` (20). Down: the mirror.
* A candle meeting all three is a *signal candle*. While the conditions keep holding, each newer candle becomes
  the signal candle. If they fail before an entry, the setup is dropped.
* Entry: a trade beyond the signal candle's high (up) or low (down). SL: the other extreme, one ATR further out.
* One trade per trending phase: after an entry, nothing more until the conditions have failed and been met
  again. A phase that is already under way when the candles start gives no setup (with `once`), since there is
  no telling whether it has had its entry.
* Its own exits on a close: the trend ending (ADX back under `adx_min`, or the DIs crossing against the trade),
  and the monthly expiry (closing what's left at that day's close).

ADX, DI and ATR are Wilder's, smoothed the way TradingView's DMI and ATR are (an SMA seed, then a running
average), so the numbers match its chart once enough candles have gone by.
"""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.crossover import SIDES, Entry, Exits, Prepared, Setup, Walk
from app.kite import Candle
from app.market import IST, is_trading_day

# NSE moved monthly F&O expiries from the last Thursday to the last Tuesday of the month from September 2025.
TUESDAY_EXPIRIES_FROM = date(2025, 9, 1)


def rma(values: list[float | None], n: int) -> list[float | None]:
    """Wilder's running average (TradingView's ta.rma): the mean of the first n values, then (prev*(n-1)+x)/n.
    Leading Nones are skipped."""
    out: list[float | None] = [None] * len(values)
    start = next((i for i, v in enumerate(values) if v is not None), len(values))
    if len(values) - start < n:
        return out
    prev = sum(values[start:start + n]) / n
    out[start + n - 1] = prev
    for i in range(start + n, len(values)):
        prev = (prev * (n - 1) + values[i]) / n
        out[i] = prev
    return out


def indicators(candles: list[Candle], adx_len: int, atr_len: int) -> dict[str, list[float | None]]:
    """+DI, -DI and ADX (period `adx_len`, ADX smoothed over the same), and ATR (period `atr_len`)."""
    tr: list[float | None] = []
    plus: list[float | None] = []
    minus: list[float | None] = []
    for i, c in enumerate(candles):
        if i == 0:
            tr.append(c.high - c.low)
            plus.append(None)
            minus.append(None)
            continue
        p = candles[i - 1]
        tr.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
        up, down = c.high - p.high, p.low - c.low
        plus.append(up if up > down and up > 0 else 0.0)
        minus.append(down if down > up and down > 0 else 0.0)
    tr_dm = [None] + tr[1:]  # the DI's true range starts with the first candle that has a previous one
    s_tr, s_plus, s_minus = rma(tr_dm, adx_len), rma(plus, adx_len), rma(minus, adx_len)
    pdi = [100 * a / t if a is not None and t else None for a, t in zip(s_plus, s_tr)]
    mdi = [100 * a / t if a is not None and t else None for a, t in zip(s_minus, s_tr)]
    dx = [100 * abs(a - b) / (a + b) if a is not None and b is not None and a + b else (0.0 if a is not None else None)
          for a, b in zip(pdi, mdi)]
    return {"pdi": pdi, "mdi": mdi, "adx": rma(dx, adx_len), "atr": rma(tr, atr_len)}


def monthly_expiry(year: int, month: int, is_trading_day=None) -> date:
    """The month's F&O expiry: its last Tuesday (last Thursday before September 2025), or the trading day before
    it when that's a holiday (`is_trading_day`, a date -> bool, if given)."""
    weekday = 1 if date(year, month, 1) >= TUESDAY_EXPIRIES_FROM else 3
    day = date(year, month, monthrange(year, month)[1])
    while day.weekday() != weekday:
        day -= timedelta(days=1)
    while is_trading_day and not is_trading_day(day):
        day -= timedelta(days=1)
    return day


@dataclass(frozen=True)
class AdxRules:
    adx_len: int = 14
    atr_len: int = 14
    adx_min: float = 25.0
    di_min: float = 25.0
    di_max: float = 20.0
    atr_mult: float = 1.0  # SL this many ATRs beyond the signal candle
    sides: str = "both"
    once: bool = True  # one trade per trending phase
    second: bool = False  # not offered for ADX trend; kept so the engine can treat every strategy alike

    @property
    def label(self) -> str:
        """'ADX 14 > 25 · DI 25 / 20'"""
        return f"ADX {self.adx_len} > {self.adx_min:g} · DI {self.di_min:g} / {self.di_max:g}"

    @property
    def summary(self) -> str:
        """'SL 1 ATR (14) past the signal candle · buys and sells · one trade per trending phase'"""
        return (f"SL {self.atr_mult:g} ATR ({self.atr_len}) past the signal candle · {SIDES[self.sides].lower()} · "
                + ("one trade per trending phase" if self.once else "every signal candle"))

    def warmup(self) -> int:
        """Candles before the first one that matters: Wilder's averages take a while to settle (ADX is an average of
        an average)."""
        return max(self.adx_len, self.atr_len) * 8 + 10

    def trending(self, ind: dict, i: int) -> str | None:
        """'buy' or 'sell' if candle i meets all three conditions, else None."""
        adx, pdi, mdi = ind["adx"][i], ind["pdi"][i], ind["mdi"][i]
        if adx is None or pdi is None or mdi is None or adx <= self.adx_min:
            return None
        if pdi > self.di_min and mdi < self.di_max:
            return "buy"
        if mdi > self.di_min and pdi < self.di_max:
            return "sell"
        return None

    def walk(self, candles: list[Candle], ind: dict) -> Walk:
        out = Walk()
        setup: Setup | None = None
        phase: int | None = None  # index of the candle that began the current trending phase
        seen_quiet = False  # a candle not meeting the conditions has been seen, so the next phase's start is real
        used = False  # this phase has given its entry
        for i, c in enumerate(candles):
            entered = False
            if setup and (c.high > setup.level if setup.side == "buy" else c.low < setup.level):
                gap = c.open > setup.level if setup.side == "buy" else c.open < setup.level
                out.entries.append(Entry(setup, i, gap))
                setup, entered, used = None, True, True
            now = self.trending(ind, i)
            if now != out.trend:
                if now and (seen_quiet or out.trend is not None):  # a phase whose start we saw (or a straight flip)
                    phase = i
                    out.crossovers.append((i, now))
                elif now:
                    phase = None  # already under way when the candles start
                out.trend, setup, used = now, None, False
            if now is None:
                if ind["adx"][i] is not None:
                    seen_quiet = True
                continue
            if entered or self.sides not in ("both", now) or (self.once and (phase is None or used)):
                continue
            atr = ind["atr"][i] or 0.0
            if now == "buy":
                setup = Setup("buy", i, c.high, c.low - self.atr_mult * atr, phase)
            else:
                setup = Setup("sell", i, c.low, c.high + self.atr_mult * atr, phase)
        out.pending = setup
        return out

    def prepare(self, candles: list[Candle]) -> Prepared:
        ind = indicators(candles, self.adx_len, self.atr_len)
        none = [None] * len(candles)

        def ends(j: int, entry: Entry, x: Exits) -> str | None:
            buy = entry.side == "buy"
            if x.trend_exit:
                adx, pdi, mdi = ind["adx"][j], ind["pdi"][j], ind["mdi"][j]
                if adx is not None and (adx < self.adx_min or (pdi < mdi if buy else pdi > mdi)):
                    return "trend_end"
            if x.expiry_exit:
                day = candles[j].start.date()
                entered = candles[entry.index].start.date()
                last_of_day = j + 1 == len(candles) or candles[j + 1].start.date() != day
                # The first monthly expiry after the day of entry (an entry on expiry day rolls to next month's).
                if day > entered and last_of_day and day == monthly_expiry(day.year, day.month, _trading_day):
                    return "expiry"
            return None

        return Prepared(self.walk(candles, ind), none, none, ends)

    def series(self, candles: list[Candle]) -> dict:
        """What a chart draws besides the candles: ADX and the two DIs in a panel underneath, with the thresholds."""
        ind = indicators(candles, self.adx_len, self.atr_len)
        return {"overlays": [], "panel": {
            "label": f"ADX / DMI {self.adx_len}",
            "lines": [{"label": "ADX", "color": "#16373A", "values": ind["adx"]},
                      {"label": "+DI", "color": "#2E8B57", "values": ind["pdi"]},
                      {"label": "−DI", "color": "#C2475A", "values": ind["mdi"]}],
            "levels": sorted({self.adx_min, self.di_min, self.di_max})}}


def _trading_day(day: date) -> bool:
    return is_trading_day(datetime(day.year, day.month, day.day, 12, tzinfo=IST))
