"""Lists of stocks a scanner or backtest can run on: Nifty 50, 100, 200 and 500, and the F&O stocks.

The Nifty lists come from NSE's index files once a day (the last list fetched, or the built-in copy in
app/data/indices.json, if NSE won't answer). F&O stocks are read off Kite's NFO instrument list: every
stock that has futures, so it needs no NSE call at all.
"""

import csv
import io
import json
import logging
from datetime import date
from pathlib import Path

import httpx

from app.kite import instruments
from app.store import store

log = logging.getLogger("universe")

UNIVERSES = {
    "nifty50": "Nifty 50",
    "nifty100": "Nifty 100",
    "nifty200": "Nifty 200",
    "nifty500": "Nifty 500",
    "fno": "F&O stocks",
}
NSE_URL = "https://nsearchives.nseindia.com/content/indices/ind_{key}list.csv"
# NSE's Nifty 50 as of October 2026, used when the live one can't be had.
NIFTY50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BSE", "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV",
    "BEL", "BHARTIARTL", "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH", "HDFCBANK",
    "HDFCLIFE", "HINDALCO", "HINDUNILVR", "ICICIBANK", "ITC", "INFY", "INDIGO", "JSWSTEEL", "JIOFIN", "KOTAKBANK", "LT",
    "M&M", "MARUTI", "MAXHEALTH", "NTPC", "NESTLEIND", "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SHRIRAMFIN", "SBIN",
    "SUNPHARMA", "TCS", "TATACONSUM", "TMPV", "TATASTEEL", "TECHM", "TITAN", "TRENT", "ULTRACEMCO",
]
_BUILT_IN = json.loads((Path(__file__).parent / "data" / "indices.json").read_text())


def built_in(key: str) -> list[str]:
    return NIFTY50 if key == "nifty50" else _BUILT_IN[key]


def _from_nse(key: str) -> list[str]:
    r = httpx.get(NSE_URL.format(key=key), timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    symbols = [row["Symbol"].strip() for row in csv.DictReader(io.StringIO(r.text)) if row.get("Symbol")]
    symbols = [s for s in symbols if not s.startswith("DUMMY")]  # NSE's placeholder for a demerger in progress
    if len(symbols) < len(built_in(key)) * 0.9:
        raise ValueError(f"only {len(symbols)} symbols")
    return symbols


def fno_stocks() -> list[str]:
    """NSE stocks that have futures on NFO, A-Z."""
    known = instruments()
    names = {i.underlying for i in known.values() if i.exchange == "NFO" and i.kind == "FUT"}
    return sorted(n for n in names if (inst := known.get(n)) and not inst.is_index)


def symbols(key: str, today: date) -> list[str]:
    """Today's members of a list, NSE symbols as Kite names them."""
    if key == "fno":
        return fno_stocks()
    if key not in UNIVERSES:
        raise KeyError(key)
    saved = store.get("settings", key) or {}
    if saved.get("date") == today.isoformat():
        return saved["symbols"]
    try:
        found = _from_nse(key)
    except (httpx.HTTPError, ValueError, KeyError) as e:
        log.warning("%s list from NSE failed (%s); using the last one", UNIVERSES[key], e)
        return saved.get("symbols") or built_in(key)
    store.put("settings", key, {"date": today.isoformat(), "symbols": found})
    return found


def parse_symbols(text: str) -> list[str]:
    """A typed list: symbols separated by commas or new lines (not spaces: index names such as NIFTY 50 have
    them), each once, in the order given."""
    out: list[str] = []
    for s in text.replace("\n", ",").upper().split(","):
        s = " ".join(s.split()).removeprefix("NSE:")
        if not s:
            continue
        if s not in out:
            out.append(s)
    return out


def describe(spec: dict) -> str:
    """'Nifty 200', 'INFY' or 'INFY, TCS and 3 more'."""
    if spec.get("list"):
        return UNIVERSES[spec["list"]]
    picked = spec.get("symbols") or []
    if len(picked) <= 3:
        return ", ".join(picked)
    return f"{', '.join(picked[:2])} and {len(picked) - 2} more"


def resolve(spec: dict, today: date) -> list[str]:
    """The symbols a scanner or backtest runs on: {"list": "nifty200"} or {"symbols": ["INFY", "TCS"]}."""
    return symbols(spec["list"], today) if spec.get("list") else list(spec.get("symbols") or [])
