"""The indicator strategies the scanners, Scan now and backtests run. Each has its own page (and permission), but
they share the engine: app.crossover for setups, entries and trades, ma_backtest / ma_scanner / ma_screen for
running them over stocks. A strategy's rules object provides warmup(), label, sides/once/second, prepare() (its
walk over candles and its own exits) and series() (what its charts draw).
"""

from dataclasses import dataclass, fields

from app.adx import AdxRules
from app.crossover import Rules


@dataclass(frozen=True)
class Strategy:
    key: str  # also its module (permission) key and the start of its URLs
    name: str
    rules: type
    blurb: str
    setup_word: str  # what the candle that sets up an entry is called


STRATEGIES: dict[str, Strategy] = {
    "ma": Strategy("ma", "MA pullback", Rules,
                   "After a fast moving average crosses the slow one, wait for a pullback candle and enter when price "
                   "trades beyond it.", "Trigger candle"),
    "adx": Strategy("adx", "ADX trend", AdxRules,
                    "Trade only while ADX says the market is trending, in the direction the DMI says: enter beyond the "
                    "signal candle, SL one ATR past its other end.", "Signal candle"),
}


def rules_from(key: str, saved: dict):
    """A strategy's rules from what a scanner saved (fields it doesn't know are left out, missing ones default)."""
    cls = STRATEGIES[key].rules
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in saved.items() if k in known})
