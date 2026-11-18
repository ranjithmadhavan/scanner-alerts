"""Indicators for custom scanners, computed the way TradingView draws them.

Each takes completed candles (oldest first) and returns lists the same length, with None where the indicator has
no value yet. INDICATORS describes each one for the scan builder: its inputs, its outputs, whether it lives on the
price scale (so a chart can draw it over the candles), and how many candles it needs to settle.
"""

import math
from dataclasses import dataclass, field

from app.adx import indicators as adx_indicators, rma
from app.kite import Candle
from app.market import IST

Series = list[float | None]

SOURCES = {"close": "Close", "open": "Open", "high": "High", "low": "Low", "hl2": "(H+L)/2", "hlc3": "(H+L+C)/3",
           "volume": "Volume"}


def source(candles: list[Candle], src: str) -> list[float]:
    if src == "hl2":
        return [(c.high + c.low) / 2 for c in candles]
    if src == "hlc3":
        return [(c.high + c.low + c.close) / 3 for c in candles]
    return [getattr(c, src) for c in candles]


def _start(values: Series) -> int:
    return next((i for i, v in enumerate(values) if v is not None), len(values))


def sma(values: Series, n: int) -> Series:
    """Leading Nones are skipped (so it can smooth another indicator)."""
    out: Series = [None] * len(values)
    s = _start(values)
    total = 0.0
    for i in range(s, len(values)):
        total += values[i]
        if i - s >= n:
            total -= values[i - n]
        if i - s >= n - 1:
            out[i] = total / n
    return out


def ema(values: Series, n: int) -> Series:
    """Seeded with the SMA of the first n values, as TradingView does. Leading Nones are skipped."""
    out: Series = [None] * len(values)
    s = _start(values)
    if len(values) - s < n:
        return out
    k = 2 / (n + 1)
    prev = sum(values[s:s + n]) / n
    out[s + n - 1] = prev
    for i in range(s + n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def wma(values: Series, n: int) -> Series:
    out: Series = [None] * len(values)
    s, weight = _start(values), n * (n + 1) / 2
    for i in range(s + n - 1, len(values)):
        out[i] = sum(values[i - n + 1 + j] * (j + 1) for j in range(n)) / weight
    return out


def rsi(values: Series, n: int) -> Series:
    gains: Series = [None] + [max(b - a, 0.0) for a, b in zip(values, values[1:])]
    losses: Series = [None] + [max(a - b, 0.0) for a, b in zip(values, values[1:])]
    up, down = rma(gains, n), rma(losses, n)
    return [None if u is None else 100.0 if d == 0 else 100 - 100 / (1 + u / d) for u, d in zip(up, down)]


def macd(values: Series, fast: int, slow: int, signal: int) -> dict[str, Series]:
    f, s = ema(values, fast), ema(values, slow)
    line: Series = [a - b if a is not None and b is not None else None for a, b in zip(f, s)]
    sig = ema(line, signal)
    return {"line": line, "signal": sig, "hist": [a - b if a is not None and b is not None else None for a, b in zip(line, sig)]}


def stoch(candles: list[Candle], n: int, smooth: int, d: int) -> dict[str, Series]:
    raw: Series = [None] * len(candles)
    for i in range(n - 1, len(candles)):
        window = candles[i - n + 1:i + 1]
        hi, lo = max(c.high for c in window), min(c.low for c in window)
        raw[i] = 100 * (candles[i].close - lo) / (hi - lo) if hi > lo else 50.0
    k = sma(raw, smooth)
    return {"k": k, "d": sma(k, d)}


def true_range(candles: list[Candle]) -> list[float]:
    return [c.high - c.low if i == 0 else
            max(c.high - c.low, abs(c.high - candles[i - 1].close), abs(c.low - candles[i - 1].close))
            for i, c in enumerate(candles)]


def atr(candles: list[Candle], n: int) -> Series:
    return rma(true_range(candles), n)


def supertrend(candles: list[Candle], n: int, mult: float) -> dict[str, Series]:
    """TradingView's ta.supertrend: the line, and direction 1 (up: line below price) or -1 (down)."""
    a = atr(candles, n)
    line: Series = [None] * len(candles)
    trend: Series = [None] * len(candles)
    upper = lower = None
    for i, c in enumerate(candles):
        if a[i] is None:
            continue
        mid = (c.high + c.low) / 2
        up, lo = mid + mult * a[i], mid - mult * a[i]
        prev_close = candles[i - 1].close
        if lower is not None and not (lo > lower or prev_close < lower):
            lo = lower
        if upper is not None and not (up < upper or prev_close > upper):
            up = upper
        if trend[i - 1] is None:
            d = 1
        elif trend[i - 1] == -1:
            d = 1 if c.close > upper else -1
        else:
            d = -1 if c.close < lower else 1
        trend[i], line[i] = d, lo if d == 1 else up
        upper, lower = up, lo
    return {"line": line, "dir": trend}


def bollinger(values: Series, n: int, mult: float) -> dict[str, Series]:
    mid = sma(values, n)
    upper: Series = [None] * len(values)
    lower: Series = [None] * len(values)
    for i, m in enumerate(mid):
        if m is not None:
            sd = math.sqrt(sum((v - m) ** 2 for v in values[i - n + 1:i + 1]) / n)  # population, as TradingView
            upper[i], lower[i] = m + mult * sd, m - mult * sd
    return {"upper": upper, "mid": mid, "lower": lower}


def highest(values: Series, n: int) -> Series:
    return [max(values[i - n + 1:i + 1]) if i >= n - 1 else None for i in range(len(values))]


def lowest(values: Series, n: int) -> Series:
    return [min(values[i - n + 1:i + 1]) if i >= n - 1 else None for i in range(len(values))]


def vwap(candles: list[Candle]) -> Series:
    """Volume-weighted (H+L+C)/3, starting afresh each day. None without volume (an index)."""
    out: Series = []
    day, pv, vol = None, 0.0, 0.0
    for c in candles:
        d = c.start.astimezone(IST).date()
        if d != day:
            day, pv, vol = d, 0.0, 0.0
        pv += (c.high + c.low + c.close) / 3 * c.volume
        vol += c.volume
        out.append(pv / vol if vol else None)
    return out


# ---- the registry the scan builder works from ----------------------------------------------------

@dataclass(frozen=True)
class Param:
    key: str
    label: str
    default: float | str
    low: float = 1
    high: float = 500
    whole: bool = True  # an integer (a period) rather than a decimal (a multiplier)


SRC = Param("src", "Of", "close")


@dataclass(frozen=True)
class Ind:
    key: str
    label: str  # short, as in "EMA(20)"
    name: str  # in the picker
    params: tuple[Param, ...]
    outputs: dict[str, str] = field(default_factory=dict)  # output key -> label; empty: a single value
    price_scale: bool = False  # drawn over the candles, comparable with price

    def warmup(self, p: dict) -> int:
        """Candles needed before the value is settled (EMAs and Wilder averages take a few periods to forget the seed)."""
        n = [v for k, v in p.items() if k not in ("src", "mult")]
        slow = max(n or [1])
        smooth = 3 if self.key in ("ema", "rsi", "macd", "adx", "atr", "supertrend") else 1
        return int(slow * smooth + sum(n) + 2)

    def compute(self, candles: list[Candle], p: dict) -> dict[str, Series]:
        src = source(candles, p.get("src", "close"))
        k = self.key
        if k == "sma":
            return {"": sma(src, p["n"])}
        if k == "ema":
            return {"": ema(src, p["n"])}
        if k == "wma":
            return {"": wma(src, p["n"])}
        if k == "rsi":
            return {"": rsi(src, p["n"])}
        if k == "macd":
            return macd(src, p["fast"], p["slow"], p["signal"])
        if k == "stoch":
            return stoch(candles, p["n"], p["smooth"], p["d"])
        if k == "supertrend":
            return supertrend(candles, p["n"], p["mult"])
        if k == "adx":
            got = adx_indicators(candles, p["n"], p["n"])
            return {"adx": got["adx"], "pdi": got["pdi"], "mdi": got["mdi"]}
        if k == "atr":
            return {"": atr(candles, p["n"])}
        if k == "bb":
            return bollinger(src, p["n"], p["mult"])
        if k == "highest":
            return {"": highest(src, p["n"])}
        if k == "lowest":
            return {"": lowest(src, p["n"])}
        if k == "vwap":
            return {"": vwap(candles)}
        raise KeyError(k)


N = lambda d: Param("n", "Period", d)  # noqa: E731
INDICATORS: dict[str, Ind] = {i.key: i for i in [
    Ind("sma", "SMA", "SMA (simple moving average)", (N(20), SRC), price_scale=True),
    Ind("ema", "EMA", "EMA (exponential moving average)", (N(20), SRC), price_scale=True),
    Ind("wma", "WMA", "WMA (weighted moving average)", (N(20), SRC), price_scale=True),
    Ind("rsi", "RSI", "RSI", (N(14), SRC)),
    Ind("macd", "MACD", "MACD", (Param("fast", "Fast", 12), Param("slow", "Slow", 26), Param("signal", "Signal", 9), SRC),
        {"line": "line", "signal": "signal", "hist": "histogram"}),
    Ind("stoch", "Stoch", "Stochastic", (N(14), Param("smooth", "%K smoothing", 3), Param("d", "%D", 3)),
        {"k": "%K", "d": "%D"}),
    Ind("supertrend", "Supertrend", "Supertrend", (N(10), Param("mult", "Multiplier", 3.0, 0.1, 20, False)),
        {"line": "line", "dir": "direction (1 up, -1 down)"}, price_scale=True),
    Ind("adx", "ADX", "ADX / DMI", (N(14),), {"adx": "ADX", "pdi": "+DI", "mdi": "-DI"}),
    Ind("atr", "ATR", "ATR (average true range)", (N(14),)),
    Ind("bb", "BB", "Bollinger bands", (N(20), Param("mult", "Std dev", 2.0, 0.1, 10, False), SRC),
        {"upper": "upper", "mid": "middle", "lower": "lower"}, price_scale=True),
    Ind("highest", "Highest", "Highest of N candles", (N(20), Param("src", "Of", "high")), price_scale=True),
    Ind("lowest", "Lowest", "Lowest of N candles", (N(20), Param("src", "Of", "low")), price_scale=True),
    Ind("vwap", "VWAP", "VWAP (intraday)", (), price_scale=True),
]}
