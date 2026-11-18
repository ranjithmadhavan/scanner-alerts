"""Custom scanners page (/scans): build a scan, run it now over a list, and keep it as a live scanner that alerts."""

from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import brokers, custom_scan, notify, scan_expr, ta, universe, webhooks
from app.kite import KiteError
from app.ma_backtest import label as tf_label
from app.market import now_ist
from app.routes.indicators import UNIVERSE_DEFAULTS, read_universe, symbols as symbol_search
from app.security import require
from app.store import new_id, store
from app.web import fail, render, toast

router = APIRouter(prefix="/scans")
guard = require("scans")


def _p(field, tf="", ago=0, pct=100):
    return {"kind": "price", "field": field, "tf": tf, "ago": ago, "pct": pct}


def _i(ind, out="", tf="", ago=0, pct=100, **params):
    return {"kind": "ind", "ind": ind, "params": params, "out": out, "tf": tf, "ago": ago, "pct": pct}


def _n(v):
    return {"kind": "num", "value": v}


def _all(*conds):
    return {"op": "all", "items": [{"lhs": a, "cmp": c, "rhs": b} for a, c, b in conds]}


# Starting points for the builder: (name, candle size, logic).
STARTERS = [
    ("Breaks above the previous day's high", "15m", _all((_p("close"), "crosses_above", _p("high", tf="1d", ago=1)))),
    ("EMA 20 crosses above EMA 50", "1d", _all((_i("ema", n=20), "crosses_above", _i("ema", n=50)))),
    ("RSI crosses 60 on rising volume", "1d", _all((_i("rsi", n=14), "crosses_above", _n(60)),
                                                   (_p("volume"), ">", _i("sma", n=20, src="volume", pct=150)))),
    ("Supertrend turns up", "1h", _all((_i("supertrend", "dir", n=10, mult=3), ">", _n(0)),
                                       (_i("supertrend", "dir", ago=1, n=10, mult=3), "<", _n(0)))),
    ("Near the 52-week high", "1d", _all((_p("close"), ">=", _i("highest", tf="1w", pct=98, n=52, src="high")))),
    ("Inside day", "1d", _all((_p("high"), "<", _p("high", ago=1)), (_p("low"), ">", _p("low", ago=1)))),
    ("MACD crosses above its signal, above EMA 200", "1d", _all((_i("macd", "line", fast=12, slow=26, signal=9), "crosses_above",
                                                                 _i("macd", "signal", fast=12, slow=26, signal=9)),
                                                                (_p("close"), ">", _i("ema", n=200)))),
]
STARTERS.append(("Breaks out of yesterday's range past VWAP", "5m", {"op": "any", "items": [
    {"op": "all", "items": _all((_p("close"), ">", _p("high", tf="1d", ago=1)), (_p("close"), ">", _i("vwap")),
                                (_p("open"), "<", _p("high", tf="1d", ago=1)))["items"]},
    {"op": "all", "items": _all((_p("close"), "<", _p("low", tf="1d", ago=1)), (_p("close"), "<", _i("vwap")),
                                (_p("open"), ">", _p("low", tf="1d", ago=1)))["items"]}]}))
STARTERS.append(("15-min uptrend, 5-min breakout", "5m", _all((_p("close", tf="15m"), ">", _i("ema", tf="15m", n=20)),
                                                              (_p("close"), "crosses_above", _p("high", ago=1)))))
STARTERS = [(name, tf, scan_expr.fit(scan_expr.validate(logic), tf)) for name, tf, logic in STARTERS]
BLANK = _all((_p("close"), ">", _i("ema", n=20)))


def _registry() -> dict:
    """What the builder offers, for app.js."""
    return {
        "prices": scan_expr.PRICE_FIELDS, "tfs": scan_expr.TFS, "tfOrder": list(scan_expr.TFS), "intraday": scan_expr.INTRADAY, "cmps": scan_expr.CMPS, "sources": ta.SOURCES,
        "maxAgo": scan_expr.MAX_AGO,
        "indicators": {k: {"name": i.name, "label": i.label, "outputs": i.outputs,
                           "params": [{"key": p.key, "label": p.label, "default": p.default, "low": p.low, "high": p.high,
                                       "whole": p.whole} for p in i.params]} for k, i in ta.INDICATORS.items()},
        "starters": [{"name": n, "tf": tf, "logic": logic} for n, tf, logic in STARTERS],
    }


# ---- page ----------------------------------------------------------------------------------------

def _mine(username: str) -> list[dict]:
    return sorted(store.list(custom_scan.COL, user=username), key=lambda s: s["created_at"], reverse=True)


def _own(scanner_id: str, user: dict) -> dict | None:
    sc = store.get(custom_scan.COL, scanner_id)
    return sc if sc and sc["user"] == user["username"] else None


def _view(sc: dict, eps: list[dict]) -> dict:
    return {**sc, "summary": scan_expr.describe(sc["logic"]), "describe": custom_scan.describe(sc),
            "sends_to": [notify.CHANNELS[c] for c in sc.get("channels", [])] + [e["name"] for e in webhooks.pick(eps, sc.get("hook_ids"))]}


def _list_ctx(user: dict) -> dict:
    eps = webhooks.endpoints(user["username"])
    return {"scanners": [_view(s, eps) for s in _mine(user["username"])], "max_scanners": custom_scan.MAX_SCANNERS}


def _feed_ctx(username: str, day: str) -> dict:
    today = now_ist().date()
    days = [(today - timedelta(days=k)).isoformat() for k in range(7)]
    day = day if day in days else days[0]
    return {"feed": custom_scan.feed(username, day), "feed_day": day, "feed_days": days}


def _screen_ctx(screen: custom_scan.Screen | None) -> dict:
    if not screen:
        return {"screen": None}
    rows = sorted(screen.rows, key=lambda r: r["symbol"])
    return {"screen": screen, "screen_rows": rows, "screen_summary": scan_expr.describe(screen.sc["logic"]),
            "screen_spec": universe.describe(screen.sc["spec"]), "screen_tf": tf_label(screen.sc["tf"]),
            "screen_columns": [k for k, _ in rows[0]["values"]] if rows else []}


NEW = {"name": "", "tf": "5m", "logic": BLANK, "channels": None, "hook_ids": None, "webhook_payload": ""}


def _ctx(user: dict, edit_id: str = "") -> dict:
    username = user["username"]
    contacts = notify.load_contacts(username)
    editing = _own(edit_id, user) if edit_id else None
    form_sc = {**NEW, **editing} if editing else NEW
    spec = form_sc.get("spec") or {}
    setup = {**UNIVERSE_DEFAULTS, **({"universe_kind": "list" if spec.get("list") else "symbols",
                                      "universe_list": spec.get("list") or "nifty50",
                                      "symbols": ", ".join(spec.get("symbols") or [])} if spec else {})}
    return {
        **_list_ctx(user), **_feed_ctx(username, ""), **_screen_ctx(custom_scan.current(username)),
        "editing": editing, "form_sc": form_sc, "setup": setup, "universes": universe.UNIVERSES,
        "timeframes": custom_scan.TIMEFRAMES, "tf_label": tf_label, "live_limit": custom_scan.LIVE_LIMIT,
        "registry": _registry(), "base": "/scans",
        "channels": [{"key": k, "label": v, "ready": notify.sender_ready(k) and notify.recipient_ready(k, contacts)}
                     for k, v in notify.CHANNELS.items()],
        "broker_ok": brokers.load(username).get("status") == "connected",
    }


@router.get("")
def page(request: Request, user: dict = Depends(guard), edit: str = ""):
    return render(request, "scans.html", _ctx(user, edit))


@router.get("/symbols")
def symbols(q: str = "", user: dict = Depends(guard)):
    return symbol_search(q)


@router.post("/describe")
async def describe(request: Request, user: dict = Depends(guard)):
    """The scan in words as it's being built, or what's wrong with it."""
    form = await request.form()
    try:
        logic = scan_expr.validate(form.get("logic", ""))
        if (tf := form.get("tf", "")) in custom_scan.TIMEFRAMES:
            logic = scan_expr.fit(logic, tf)
        return JSONResponse({"text": scan_expr.describe(logic)})
    except ValueError as e:
        return JSONResponse({"error": str(e)})


# ---- reading the form ----------------------------------------------------------------------------

def _read_scan(form) -> dict:
    """{"spec", "tf", "logic", "name"} from the builder. Raises ValueError with something to show."""
    spec = read_universe(form)
    tf = form.get("tf", "")
    if tf not in custom_scan.TIMEFRAMES:
        raise ValueError("Pick a candle size.")
    logic = scan_expr.fit(scan_expr.validate(form.get("logic", "")), tf)
    name = (form.get("name") or "").strip()[:60] or f"{universe.describe(spec)} scan"
    return {"spec": spec, "tf": tf, "logic": logic, "name": name}


def _read_scanner(form, username: str) -> dict:
    settings = _read_scan(form)
    if problem := custom_scan.limit_problem(len(universe.resolve(settings["spec"], now_ist().date())), settings["tf"]):
        raise ValueError(problem)
    hooks, error = webhooks.selection(username, form.get("hook_mode", "default"), form.getlist("hook_ids"), form.get("webhook_payload", ""))
    if error:
        raise ValueError(error)
    channels = form.getlist("channels")
    return {**settings, "channels": [c for c in notify.CHANNELS if c in channels], **hooks}


def _list(request: Request, user: dict) -> HTMLResponse:
    return render(request, "partials/scan_list.html", _list_ctx(user))


# ---- saved scanners ------------------------------------------------------------------------------

@router.post("/scanners")
async def create(request: Request, user: dict = Depends(guard)):
    username = user["username"]
    try:
        if len(_mine(username)) >= custom_scan.MAX_SCANNERS:
            raise ValueError(f"You can keep up to {custom_scan.MAX_SCANNERS} custom scanners. Remove one first.")
        settings = _read_scanner(await request.form(), username)
    except ValueError as e:
        return fail(str(e))
    now = now_ist().isoformat()
    sc = {"id": new_id(), "user": username, **settings, "status": "active", "created_at": now, "armed_at": now,
          "matched": [], "evaluated_at": ""}
    store.put(custom_scan.COL, sc["id"], sc)
    nowhere = not sc["channels"] and not webhooks.targets(username, sc["hook_ids"])
    return toast(_list(request, user), "Scanner saved. From the next candle close you'll hear about each stock that newly "
                 "matches." + (" It has nowhere to send yet." if nowhere else ""))


@router.post("/scanners/{scanner_id}")
async def update(request: Request, scanner_id: str, user: dict = Depends(guard)):
    if not (sc := _own(scanner_id, user)):
        return fail("That scanner is gone.")
    try:
        settings = _read_scanner(await request.form(), user["username"])
    except ValueError as e:
        return fail(str(e))
    msg = "Scanner updated."
    if any(settings[k] != sc.get(k) for k in ("spec", "tf", "logic")):  # a different scan: start afresh
        settings.update(armed_at=now_ist().isoformat(), matched=[], evaluated_at="")
        msg += " It starts afresh at the next candle close."
    with custom_scan.lock(scanner_id):
        store.update(custom_scan.COL, scanner_id, settings)
    return toast(_list(request, user), msg)


@router.post("/scanners/{scanner_id}/pause")
def pause(request: Request, scanner_id: str, user: dict = Depends(guard)):
    if not (sc := _own(scanner_id, user)):
        return fail("That scanner is gone.")
    if sc["status"] == "active":
        store.update(custom_scan.COL, scanner_id, {"status": "paused"})
        return toast(_list(request, user), "Paused")
    store.update(custom_scan.COL, scanner_id, {"status": "active", "armed_at": now_ist().isoformat(), "matched": [], "evaluated_at": ""})
    return toast(_list(request, user), "Watching again from the next candle close")


@router.post("/scanners/{scanner_id}/delete")
def delete(request: Request, scanner_id: str, user: dict = Depends(guard)):
    if _own(scanner_id, user):
        store.delete(custom_scan.COL, scanner_id)
    return toast(_list(request, user), "Scanner removed")


@router.post("/scanners/{scanner_id}/test")
def test(scanner_id: str, user: dict = Depends(guard)):
    """A sample message on the scanner's channels and webhooks."""
    if not (sc := _own(scanner_id, user)):
        return fail("That scanner is gone.")
    now = now_ist()
    values = [[label, 1234.5] for label in scan_expr.labels(sc["logic"]) if label != "Close"]
    item = {"symbol": "SAMPLE", "close": 1534.5, "change": 1.25, "at": now.isoformat(), "values": values}
    subject, body = custom_scan.message(sc, [item], now)
    results = notify.send(user["username"], sc.get("channels", []), "[Test] " + subject, body)
    if hooks := webhooks.targets(user["username"], sc.get("hook_ids")):
        results["webhook"] = webhooks.send_now(hooks, custom_scan.hook_body(sc, [item], now, test=True))
    if not results:
        return fail("This scanner has no channels or webhooks to send to.")
    sent = [notify.CHANNELS.get(c, "Webhook") for c, r in results.items() if r == "sent"]
    failed = [f"{notify.CHANNELS.get(c, 'Webhook')}: {r}" for c, r in results.items() if r != "sent"]
    if failed:
        return fail("Test: " + "; ".join(failed) + (f" (sent on {', '.join(sent)})" if sent else ""))
    return toast(HTMLResponse("", headers={"HX-Reswap": "none"}), f"Test sent to {', '.join(sent)}")


# ---- Scan now ------------------------------------------------------------------------------------

@router.post("/screen")
async def screen_start(request: Request, user: dict = Depends(guard)):
    try:
        sc = _read_scan(await request.form())
        screen = custom_scan.start(user["username"], {**sc, "id": ""})
    except (ValueError, KiteError) as e:
        return fail(str(e))
    return render(request, "partials/scan_screen.html", _screen_ctx(screen))


@router.get("/screen/status")
def screen_status(request: Request, user: dict = Depends(guard)):
    screen = custom_scan.current(user["username"])
    return render(request, "partials/scan_screen.html", _screen_ctx(screen)) if screen else HTMLResponse('<div id="scan-screen"></div>')


@router.post("/screen/cancel")
def screen_cancel(user: dict = Depends(guard)):
    custom_scan.cancel(user["username"])
    return toast(HTMLResponse("", headers={"HX-Reswap": "none"}), "Stopping after the stock in progress")


@router.get("/screen/chart")
def screen_chart(symbol: str, user: dict = Depends(guard)):
    screen = custom_scan.current(user["username"])
    if not screen:
        return JSONResponse({"error": "Run a scan first."}, status_code=404)
    try:
        return JSONResponse(custom_scan.chart(screen, symbol))
    except KiteError as e:
        return JSONResponse({"error": str(e)}, status_code=404)


@router.get("/feed")
def feed(request: Request, user: dict = Depends(guard), day: str = ""):
    return render(request, "partials/scan_feed.html", _feed_ctx(user["username"], day))

