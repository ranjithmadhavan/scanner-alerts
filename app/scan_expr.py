"""Custom scans: what one says, in words, and whether a stock passes it. Pure: candles in, answers out.

A scan's `logic` is {"op": "all"|"any", "items": [...]}, each item a condition or a group (one level deep) of
conditions with its own all/any. A condition compares two values: {"lhs": value, "cmp": ">", "rhs": value}, where
"crosses_above" / "crosses_below" also look at the candle before. A value is:

* {"kind": "price", "field": "close", "tf": "", "ago": 0, "pct": 100}: a candle's open/high/low/close/volume;
* {"kind": "ind", "ind": "ema", "params": {"n": 20, "src": "close"}, "out": "", "tf": "", "ago": 0, "pct": 100};
* {"kind": "num", "value": 60}.

`ago` counts candles back (1: the previous one). `tf` "" is the scan's own candle size; a bigger intraday size ("15m",
"1h", … a whole multiple of the scan's) is built from the scan's own candles; "1d" and "1w" are daily and weekly
candles. On a bigger size, ago 0 is the candle still forming (built from the scan's candles so far), so in a 5-minute
scan "previous daily high" is yesterday's high, "daily high" is today's so far and "15 min close" is the latest 5-minute
close. `pct` scales a value ("102% of").
"""

import copy
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from app.kite import Candle
from app.market import IST
from app.ta import INDICATORS, SOURCES

PRICE_FIELDS = {"close": "Close", "open": "Open", "high": "High", "low": "Low", "volume": "Volume"}
INTRADAY = {"1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30, "1h": 60, "4h": 240}  # minutes
TFS = {"": "Scan's candle", "1m": "1 min", "3m": "3 min", "5m": "5 min", "10m": "10 min", "15m": "15 min", "30m": "30 min", "1h": "1 hour",
       "4h": "4 hour", "1d": "Daily", "1w": "Weekly"}
CMPS = {">": ">", ">=": "≥", "<": "<", "<=": "≤", "=": "=", "crosses_above": "crosses above", "crosses_below": "crosses below"}
CROSSES = ("crosses_above", "crosses_below")
MAX_CONDITIONS = 20
MAX_GROUPS = 5
MAX_AGO = 50
MAX_JSON = 20_000


# ---- checking what a form sent ------------------------------------------------------------------

def _number(raw, what: str, low: float, high: float, whole: bool) -> float | int:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number.") from None
    if not math.isfinite(v) or not low <= v <= high:
        raise ValueError(f"{what} must be between {low:g} and {high:g}.")
    if whole:
        if v != int(v):
            raise ValueError(f"{what} must be a whole number.")
        return int(v)
    return v


def _value(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("A condition is missing a value.")
    kind = raw.get("kind")
    if kind == "num":
        return {"kind": "num", "value": _number(raw.get("value"), "A number", -1e9, 1e9, False)}
    tf = raw.get("tf") or ""
    if tf not in TFS:
        raise ValueError("Pick a candle size for each value.")
    common = {"tf": tf, "ago": _number(raw.get("ago", 0), "Candles ago", 0, MAX_AGO, True),
              "pct": _number(raw.get("pct", 100), "The % of a value", 0.01, 10_000, False)}
    if kind == "price":
        if raw.get("field") not in PRICE_FIELDS:
            raise ValueError("Pick open, high, low, close or volume.")
        return {"kind": "price", "field": raw["field"], **common}
    if kind == "ind":
        ind = INDICATORS.get(raw.get("ind"))
        if not ind:
            raise ValueError("Pick an indicator.")
        given = raw.get("params") or {}
        params = {}
        for p in ind.params:
            v = given.get(p.key, p.default)
            if p.key == "src":
                if v not in SOURCES:
                    raise ValueError(f"Pick what {ind.label} is worked out from.")
                params["src"] = v
            else:
                params[p.key] = _number(v, f"{ind.label}'s {p.label.lower()}", p.low, p.high, p.whole)
        if ind.key == "macd" and params["fast"] >= params["slow"]:
            raise ValueError("MACD's fast period must be shorter than its slow one.")
        out = raw.get("out") or (next(iter(ind.outputs)) if ind.outputs else "")
        if (out not in ind.outputs) if ind.outputs else out:
            raise ValueError(f"Pick which {ind.label} line to use.")
        return {"kind": "ind", "ind": ind.key, "params": params, "out": out, **common}
    raise ValueError("Pick a price, an indicator or a number for each value.")


def _condition(raw) -> dict:
    if not isinstance(raw, dict) or raw.get("cmp") not in CMPS:
        raise ValueError("Pick a comparison (>, <, crosses above …) for each condition.")
    cond = {"lhs": _value(raw.get("lhs")), "cmp": raw["cmp"], "rhs": _value(raw.get("rhs"))}
    if cond["lhs"]["kind"] == "num" and cond["rhs"]["kind"] == "num":
        raise ValueError("A condition needs a price or an indicator on at least one side.")
    return cond


def _op(raw) -> str:
    if raw not in ("all", "any"):
        raise ValueError("Choose all or any.")
    return raw


def validate(raw: str | dict) -> dict:
    """A scan's logic from the builder, checked and filled in. Raises ValueError with something to show."""
    if isinstance(raw, str):
        if len(raw) > MAX_JSON:
            raise ValueError("That scan is too big.")
        try:
            raw = json.loads(raw)
        except ValueError:
            raise ValueError("The scan couldn't be read. Reload the page and try again.") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        raise ValueError("The scan couldn't be read. Reload the page and try again.")
    items, groups, count = [], 0, 0
    for item in raw["items"]:
        if isinstance(item, dict) and "items" in item:
            groups += 1
            if not isinstance(item["items"], list):
                raise ValueError("The scan couldn't be read. Reload the page and try again.")
            inner = [_condition(c) for c in item["items"]]
            count += len(inner)
            if inner:
                items.append({"op": _op(item.get("op")), "items": inner})
        else:
            items.append(_condition(item))
            count += 1
    if not count:
        raise ValueError("Add at least one condition.")
    if count > MAX_CONDITIONS:
        raise ValueError(f"A scan can have up to {MAX_CONDITIONS} conditions.")
    if groups > MAX_GROUPS:
        raise ValueError(f"A scan can have up to {MAX_GROUPS} groups.")
    return {"op": _op(raw.get("op")), "items": items}


def fits(vtf: str, tf: str) -> bool:
    """Can a value on `vtf` candles be read in a scan of `tf` candles? Bigger intraday sizes must be whole multiples."""
    if vtf in ("", "1d", "1w") or vtf == tf:
        return True
    if vtf not in INTRADAY or tf not in INTRADAY:
        return False
    return INTRADAY[vtf] > INTRADAY[tf] and INTRADAY[vtf] % INTRADAY[tf] == 0


def fit(logic: dict, tf: str) -> dict:
    """The logic for a scan of `tf` candles: values on the scan's own size become "". Raises ValueError if one can't be read."""
    out = copy.deepcopy(logic)
    for c in conditions(out):
        for v in (c["lhs"], c["rhs"]):
            if v["kind"] == "num":
                continue
            if not fits(v["tf"], tf):
                scan = TFS.get(tf, tf).lower()
                raise ValueError(f"{TFS[v['tf']]} candles can't be read in a {scan} scan. Pick {scan} or a bigger size that's a "
                                 f"whole number of {scan} candles, or make the scan's candle size smaller.")
            if v["tf"] == tf:
                v["tf"] = ""
    return out


def conditions(logic: dict) -> list[dict]:
    return [c for item in logic["items"] for c in (item["items"] if "items" in item else [item])]


# ---- in words -----------------------------------------------------------------------------------

def _g(v: float) -> str:
    return f"{v:,.2f}".rstrip("0").rstrip(".") if abs(v) < 1e6 else f"{v:,.0f}"


def describe_value(v: dict) -> str:
    """'Close', 'Prev daily high', '3 candles ago EMA(20)', '102% of Weekly high', 'MACD(12,26,9) signal'."""
    if v["kind"] == "num":
        return _g(v["value"])
    if v["kind"] == "price":
        what = PRICE_FIELDS[v["field"]]
    else:
        ind = INDICATORS[v["ind"]]
        shown = [_g(v["params"][p.key]) if p.key != "src" else SOURCES[v["params"]["src"]].lower()
                 for p in ind.params if p.key != "src" or v["params"]["src"] != p.default]
        what = f"{ind.label}({','.join(shown)})" if shown else ind.label
        if v["out"]:
            what += f" {ind.outputs[v['out']]}"
    words = (["Prev"] if v["ago"] == 1 else [f"{v['ago']} candles ago"] if v["ago"] else []) + \
        ([TFS[v["tf"]]] if v["tf"] else []) + [what]
    if v["pct"] != 100:
        words.insert(0, f"{_g(v['pct'])}% of")
    # Only the first word is capitalised: "Prev daily high", "102% of prev high", but "EMA(20)" keeps its capitals.
    words = [w if i == 0 or (w == what and v["kind"] == "ind") else w.lower() for i, w in enumerate(words)]
    return " ".join(words)


def describe_condition(c: dict) -> str:
    return f"{describe_value(c['lhs'])} {CMPS[c['cmp']]} {describe_value(c['rhs'])}"


def describe(logic: dict) -> str:
    def join(items: list[str], op: str) -> str:
        return f" {'and' if op == 'all' else 'or'} ".join(items)
    parts = [f"({join([describe_condition(c) for c in item['items']], item['op'])})" if "items" in item and len(item["items"]) > 1
             else describe_condition(item["items"][0] if "items" in item else item) for item in logic["items"]]
    return join(parts, logic["op"])


# ---- how many candles a scan needs --------------------------------------------------------------

def _values(logic: dict):
    for c in conditions(logic):
        for side in ("lhs", "rhs"):
            yield c[side], c["cmp"] in CROSSES


def needs(logic: dict, tf: str) -> dict[str, int]:
    """Candles to read: {"main": of the scan's size, "daily": completed daily candles before today (0: none)}."""
    main, daily = 2, 0
    for v, crosses in _values(logic):
        if v["kind"] == "num":
            continue
        n = (INDICATORS[v["ind"]].warmup(v["params"]) if v["kind"] == "ind" else 1) + v["ago"] + 1 + crosses
        vtf = v["tf"] if v["tf"] != tf else ""
        if vtf in INTRADAY and tf in INTRADAY:  # built from the scan's own candles, plus the one forming
            ratio = INTRADAY[vtf] // INTRADAY[tf]
            main = max(main, n * ratio + ratio)
            continue
        if vtf == "1w":
            n = n * 5 + 5
        if vtf == "":
            main = max(main, n)
        elif tf == "1d":  # weekly in a daily scan: built from the scan's own daily candles
            main = max(main, n)
        else:
            daily = max(daily, n)
    return {"main": main, "daily": daily}


def uses_daily(logic: dict) -> bool:
    return any(v["kind"] != "num" and v["tf"] in ("1d", "1w") for v, _ in _values(logic))


# ---- evaluating ---------------------------------------------------------------------------------

def _day(c: Candle) -> date:
    return c.start.astimezone(IST).date()


def merge(candles: list[Candle]) -> Candle:
    return Candle(candles[0].start, candles[0].open, max(c.high for c in candles), min(c.low for c in candles),
                  candles[-1].close, sum(c.volume for c in candles))


def group(candles: list[Candle], m: int, open_at: time) -> list[Candle]:
    """Smaller candles put together into ones of `m` minutes, counted from each day's open (as Kite's are)."""
    out: list[Candle] = []
    key = None
    for c in candles:
        start = c.start.astimezone(IST)
        day_open = datetime.combine(start.date(), open_at, IST)
        n = int((start - day_open).total_seconds() // (m * 60))
        if (start.date(), n) == key:
            last = out[-1]
            out[-1] = Candle(last.start, last.open, max(last.high, c.high), min(last.low, c.low), c.close, last.volume + c.volume)
        else:
            out.append(Candle(day_open + timedelta(minutes=m * n), c.open, c.high, c.low, c.close, c.volume))
            key = (start.date(), n)
    return out


def weekly(daily: list[Candle]) -> list[Candle]:
    """Daily candles put together by week (Monday to Friday)."""
    out: list[list[Candle]] = []
    key = None
    for c in daily:
        d = _day(c)
        k = d - timedelta(days=d.weekday())
        if k != key:
            out.append([])
            key = k
        out[-1].append(c)
    return [merge(w) for w in out]


@dataclass
class Stock:
    """One stock's candles: the scan's own (`main`), and for an intraday scan, completed daily candles of earlier days."""
    tf: str
    main: list[Candle]
    daily: list[Candle] = field(default_factory=list)
    open_at: time = time(9, 15)  # the session's open, where bigger intraday candles start
    _cache: dict = field(default_factory=dict)

    def own(self, tf: str) -> bool:
        return not tf or tf == self.tf

    def candles(self, tf: str, j: int) -> list[Candle]:
        """The candles a value on `tf` is read from when judging main candle j: the last one is the current one."""
        if self.own(tf):
            return self.main[:j + 1]
        if tf in INTRADAY:
            return group(self.main[:j + 1], INTRADAY[tf], self.open_at)
        if self.tf == "1d":
            days = self.main[:j + 1]
        else:
            today = _day(self.main[j])
            so_far = [c for c in self.main[:j + 1] if _day(c) == today]
            days = [c for c in self.daily if _day(c) < today] + [merge(so_far)]
        return days if tf == "1d" else weekly(days)

    def value(self, v: dict, j: int) -> float | None:
        if v["kind"] == "num":
            return v["value"]
        own = self.own(v["tf"])
        if v["kind"] == "price":
            got = self._read(v["tf"], j)
            k = len(got) - 1 - v["ago"]
            x = getattr(got[k], v["field"]) if k >= 0 else None
        else:
            # Indicators are worked out once over all the scan's candles (they only look back); on a bigger timeframe,
            # once per judged candle, since its forming candle changes.
            key = (v["ind"], json.dumps(v["params"], sort_keys=True), v["tf"] if not own else "", None if own else j)
            if key not in self._cache:
                got = self._read(v["tf"], len(self.main) - 1 if own else j)
                self._cache[key] = (INDICATORS[v["ind"]].compute(got, v["params"]), len(got))
            series, length = self._cache[key]
            k = (j if own else length - 1) - v["ago"]
            x = series[v["out"]][k] if k >= 0 else None
        return None if x is None else x * v["pct"] / 100

    def _read(self, tf: str, j: int) -> list[Candle]:
        key = ("candles", tf, j)
        if key not in self._cache:
            self._cache[key] = self.candles(tf, j)
        return self._cache[key]


def _compare(a: float, cmp: str, b: float) -> bool:
    if cmp == ">":
        return a > b
    if cmp == ">=":
        return a >= b or math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    if cmp == "<":
        return a < b
    if cmp == "<=":
        return a <= b or math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)


def check(c: dict, stock: Stock, j: int) -> bool:
    a, b = stock.value(c["lhs"], j), stock.value(c["rhs"], j)
    if a is None or b is None:
        return False
    if c["cmp"] not in CROSSES:
        return _compare(a, c["cmp"], b)
    if j < 1:
        return False
    pa, pb = stock.value(c["lhs"], j - 1), stock.value(c["rhs"], j - 1)
    if pa is None or pb is None:
        return False
    return (a > b and pa <= pb) if c["cmp"] == "crosses_above" else (a < b and pa >= pb)


def passes(logic: dict, stock: Stock, j: int | None = None) -> bool:
    """Does the stock pass the scan on main candle j (the last one if not given)?"""
    j = len(stock.main) - 1 if j is None else j
    if j < 0:
        return False

    def group(op: str, results) -> bool:
        return all(results) if op == "all" else any(results)
    return group(logic["op"], (group(i["op"], (check(c, stock, j) for c in i["items"])) if "items" in i else check(i, stock, j)
                               for i in logic["items"]))


def labels(logic: dict) -> list[str]:
    """Each value the scan looks at, in words, once each. Numbers are left out."""
    out: list[str] = []
    for c in conditions(logic):
        for v in (c["lhs"], c["rhs"]):
            if v["kind"] != "num" and (label := describe_value(v)) not in out:
                out.append(label)
    return out


def readings(logic: dict, stock: Stock, j: int | None = None) -> list[tuple[str, float | None]]:
    """Each value the scan looks at, as it stood on candle j, for results and messages. Numbers are left out."""
    j = len(stock.main) - 1 if j is None else j
    by_label = {describe_value(v): v for c in conditions(logic) for v in (c["lhs"], c["rhs"]) if v["kind"] != "num"}
    return [(label, stock.value(by_label[label], j)) for label in labels(logic)]
