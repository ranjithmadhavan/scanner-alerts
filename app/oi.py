"""Nifty option open interest: snapshots through the day, and what they say about it.

At the open and then every `interval` minutes until the close, one Kite quote request fetches the
nearest-expiry Nifty options a few strikes either side of the at-the-money strike, plus the index.
Each snapshot is saved (collection oi_snapshots, one document per capture; oi_days keeps a summary
per day so the history list stays cheap), so any moment of any captured day can be looked at again.

Reading a snapshot against the day's first one:

* the change in call OI and put OI since the open, across the strikes in view. Option writers
  defend the strikes they sell, so put OI growing faster than call OI means support is being
  built under price (bullish); call OI growing faster means a ceiling is being built (bearish);
* PCR, put OI / call OI, for the same strikes;
* support and resistance: the strikes holding the most put OI and the most call OI.

The sentiment is the balance of the first: (put change - call change) / (|put change| + |call change|),
from -1 (only call writing) to +1 (only put writing). At the open there is no change yet, so PCR
stands in.
"""

import logging
from datetime import date, datetime, timedelta

from app import brokers, config
from app.kite import Instrument, KiteError, instruments
from app.market import IST, MarketSettings, in_scan_window
from app.store import store

log = logging.getLogger("oi")

UNDERLYING = "NIFTY"
SPOT_KEY = "NSE:NIFTY 50"
DEFAULTS = {"interval": 15, "strikes": 10}
INTERVALS = [15, 30]
STRIKE_CHOICES = [5, 8, 10, 12, 15]
LAKH = 100_000


def load_settings() -> dict:
    return {**DEFAULTS, **(store.get("settings", "oi") or {})}


def save_settings(interval: int, strikes: int) -> None:
    store.update("settings", "oi", {"interval": interval, "strikes": strikes})


# ---- the chain -----------------------------------------------------------------

def nearest_chain(today: date) -> tuple[str, list[Instrument]]:
    """(expiry, options) for the nearest Nifty expiry that hasn't passed."""
    options = [i for i in instruments().values()
               if i.exchange == "NFO" and i.underlying == UNDERLYING and i.kind in ("CE", "PE") and i.expiry >= today.isoformat()]
    if not options:
        raise KiteError("No Nifty options in Kite's instrument list")
    expiry = min(i.expiry for i in options)
    return expiry, [i for i in options if i.expiry == expiry]


def strikes_around(options: list[Instrument], spot: float, each_side: int) -> list[float]:
    """The at-the-money strike and `each_side` strikes above and below it."""
    strikes = sorted({i.strike for i in options})
    atm = min(range(len(strikes)), key=lambda k: abs(strikes[k] - spot))
    return strikes[max(0, atm - each_side):atm + each_side + 1]


def slot(now: datetime, s: MarketSettings, interval: int) -> str:
    """The capture slot `now` falls in: the open, then every `interval` minutes. e.g. 2026-10-06T09:45"""
    open_at = s.open_at(now)
    k = max(0, int((now - open_at).total_seconds() // (interval * 60)))
    at = min(open_at + timedelta(minutes=k * interval), s.close_at(now))
    return f"{now.date().isoformat()}T{at:%H:%M}"


def capture(client, now: datetime, slot_id: str | None = None) -> dict:
    """Fetch and save one snapshot. Raises KiteError."""
    cfg = load_settings()
    expiry, options = nearest_chain(now.date())
    spot = client.quote([SPOT_KEY])[SPOT_KEY]["last_price"]
    wanted = set(strikes_around(options, spot, int(cfg["strikes"])))
    picked = [o for o in options if o.strike in wanted]
    data = client.quote([f"NFO:{o.symbol}" for o in picked] + [SPOT_KEY])
    spot = data.get(SPOT_KEY, {}).get("last_price", spot)
    rows: dict[float, dict] = {}
    for o in picked:
        q = data.get(f"NFO:{o.symbol}")
        if not q:
            continue
        side = "ce" if o.kind == "CE" else "pe"
        row = rows.setdefault(o.strike, {"strike": o.strike, "ce_oi": 0, "pe_oi": 0, "ce_ltp": None, "pe_ltp": None})
        row[f"{side}_oi"] = int(q.get("oi") or 0)
        row[f"{side}_ltp"] = q.get("last_price")
    snap = {
        "id": slot_id or f"{now.date().isoformat()}T{now:%H:%M}",
        "date": now.date().isoformat(), "at": now.isoformat(), "expiry": expiry, "spot": spot,
        "rows": [rows[k] for k in sorted(rows)],
    }
    store.put("oi_snapshots", snap["id"], snap)
    day = store.get("oi_days", snap["date"]) or {}
    slots = sorted(set(day.get("slots", [])) | {snap["id"]})
    store.put("oi_days", snap["date"], {"date": snap["date"], "expiry": expiry, "count": len(slots), "slots": slots,
                                        "first_at": day.get("first_at") or snap["at"], "last_at": snap["at"]})
    return snap


# ---- capturing on schedule --------------------------------------------------------

_done: set[str] = set()  # slots captured by this process, to skip the store look-up


def _client_for_capture():
    """Any connected Kite session belonging to someone allowed to see OI (the data is the same for all)."""
    for u in store.list("users"):
        if u.get("active", True) and (u.get("role") == "superadmin" or "oi" in u.get("modules", [])):
            doc = brokers.load(u["username"])
            if doc.get("status") == "connected":
                try:
                    return brokers.client_for(u["username"], doc)
                except KiteError:
                    continue
    return None


def capture_if_due(now: datetime, s: MarketSettings) -> dict | None:
    """Called on every scanner tick: take this slot's snapshot if it hasn't been taken."""
    if not config.OI_CAPTURE or not in_scan_window(s, now):
        return None
    slot_id = slot(now, s, int(load_settings()["interval"]))
    if slot_id in _done or store.get("oi_snapshots", slot_id):
        _done.add(slot_id)
        return None
    client = _client_for_capture()
    if not client:
        return None
    try:
        snap = capture(client, now, slot_id)
    except KiteError as e:
        log.warning("OI capture %s failed: %s", slot_id, e)
        return None
    _done.add(slot_id)
    log.info("OI captured %s (%d strikes)", slot_id, len(snap["rows"]))
    return snap


# ---- reading it --------------------------------------------------------------------

def day_snapshots(day: str) -> list[dict]:
    return sorted(store.list("oi_snapshots", date=day), key=lambda s: s["at"])


def analyse(snap: dict, first: dict) -> dict:
    """What `snap` says, read against the day's first snapshot `first`."""
    base = {r["strike"]: r for r in first["rows"]}
    rows = []
    for r in snap["rows"]:
        b = base.get(r["strike"])
        rows.append({**r, "ce_chg": r["ce_oi"] - b["ce_oi"] if b else None, "pe_chg": r["pe_oi"] - b["pe_oi"] if b else None})
    ce_total = sum(r["ce_oi"] for r in rows)
    pe_total = sum(r["pe_oi"] for r in rows)
    ce_chg = sum(r["ce_chg"] for r in rows if r["ce_chg"] is not None)
    pe_chg = sum(r["pe_chg"] for r in rows if r["pe_chg"] is not None)
    pcr = pe_total / ce_total if ce_total else None
    moved = abs(ce_chg) + abs(pe_chg)
    support = max(rows, key=lambda r: r["pe_oi"], default=None)
    resistance = max(rows, key=lambda r: r["ce_oi"], default=None)
    spot_move = snap["spot"] - first["spot"]
    atm = min(rows, key=lambda r: abs(r["strike"] - snap["spot"]))["strike"] if rows else None

    reasons = []
    if snap["id"] == first["id"] or moved < 0.005 * (ce_total + pe_total):
        score = None  # nothing has changed enough to read; PCR alone
        label = "Mildly bullish" if pcr and pcr >= 1.3 else "Mildly bearish" if pcr and pcr <= 0.7 else "Neutral"
        reasons.append("Too early in the day for OI changes to say much; this reading goes by PCR alone."
                       if snap["id"] == first["id"] else "OI has hardly changed since the open; this reading goes by PCR alone.")
    else:
        score = (pe_chg - ce_chg) / moved
        label = ("Bullish" if score >= 0.35 else "Mildly bullish" if score >= 0.12 else
                 "Bearish" if score <= -0.35 else "Mildly bearish" if score <= -0.12 else "Neutral")
        lead, lag = ("Put", "call") if pe_chg >= ce_chg else ("Call", "put")
        reasons.append(f"{lead} OI has changed by {lakhs(max(pe_chg, ce_chg))} since the open against {lakhs(min(pe_chg, ce_chg))} "
                       f"for {lag}s: writers are {'building support under' if lead == 'Put' else 'capping'} price.")
    if pcr is not None:
        reasons.append(f"PCR {pcr:.2f} across these strikes"
                       + (" (more puts than calls open)" if pcr > 1 else " (more calls than puts open)" if pcr < 1 else "") + ".")
    if support and resistance:
        reasons.append(f"Most put OI at {support['strike']:,.0f} (support), most call OI at {resistance['strike']:,.0f} (resistance).")
    return {
        "label": label, "tone": "up" if "Bullish" in label or "bullish" in label else "down" if "earish" in label else "flat",
        "score": score, "pcr": pcr, "ce_total": ce_total, "pe_total": pe_total, "ce_chg": ce_chg, "pe_chg": pe_chg,
        "support": support["strike"] if support else None, "resistance": resistance["strike"] if resistance else None,
        "spot": snap["spot"], "spot_move": spot_move, "atm": atm, "rows": rows, "reasons": reasons,
    }


def series(snaps: list[dict]) -> list[dict]:
    """One point per snapshot of a day, for the chart: OI change since the open (lakhs) and the index."""
    if not snaps:
        return []
    first = snaps[0]
    out = []
    for snap in snaps:
        a = analyse(snap, first)
        at = datetime.fromisoformat(snap["at"]).astimezone(IST)
        out.append({"time": int(at.timestamp()) + 19800, "slot": snap["id"], "ce": round(a["ce_chg"] / LAKH, 2),
                    "pe": round(a["pe_chg"] / LAKH, 2), "spot": snap["spot"], "label": a["label"]})
    return out


def lakhs(n: float) -> str:
    """OI in lakhs, signed when it's a change: 12.4L, -3.1L."""
    return f"{n / LAKH:,.1f}L"
