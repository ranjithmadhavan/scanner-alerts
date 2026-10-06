import json
from bisect import bisect_right
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import brokers, fractals, notify, prices
from app import webhooks as hooks_module  # the forms have a field called `webhooks`
from app import scanner
from app.kite import TIMEFRAMES, Candle, KiteAuthError, KiteError, find_instrument, search_instruments
from app.market import in_scan_window, load_settings, now_ist
from app.scanner import CONDITIONS, EITHER_WAY, alert_key, last_prices, levels_of, uses_close
from app.security import require
from app.store import new_id, store
from app.web import _company, fail, render, toast

router = APIRouter(prefix="/alerts")
guard = require("scanner")

MAX_LEVELS = 10


def rail(alert: dict, price: float | None) -> dict | None:
    """Where to draw the last price relative to the level on a ±3% scale.
    `alert` only needs a level and a condition, so a single level works too."""
    if price is None:
        return None
    level = float(alert["level"])
    pct = (price - level) / level * 100
    pos = 50 + max(-3.0, min(3.0, pct)) / 3.0 * 44
    cond = alert["condition"]
    # Either-way levels are never "past": whichever side price is on, they wait for the other.
    reached = cond not in EITHER_WAY and (price > level if cond in ("close_above", "high_above") else price < level)
    return {"pos": round(pos, 1), "pct": abs(pct), "gap": abs(price - level), "reached": reached,
            "side": "above" if price >= level else "below"}


TABS = {"active": "Watching", "triggered": "Triggered", "paused": "Paused", "all": "All"}
SORTS = {"near": "Closest to level", "symbol": "Symbol A–Z", "new": "Newest first"}


def _filters(request: Request) -> dict:
    """Tab/search/sort live in the session so every refresh and action keeps the user's view."""
    f = {"tab": "active", "q": "", "sort": "near", **request.session.get("alert_filters", {})}
    changed = False
    for key, allowed in (("tab", TABS), ("sort", SORTS), ("q", None)):
        if key in request.query_params:
            value = request.query_params[key].strip()[:40]
            if allowed is None or value in allowed:
                f[key], changed = value, True
    if changed:
        request.session["alert_filters"] = f
    return f


def _view(alerts: list[dict], username: str, broker_ok: bool, now) -> list[dict]:
    s, tick = load_settings(), scanner.next_tick()
    today = now.date().isoformat()
    out = []
    for a in alerts:
        key = alert_key(a)
        price, as_of = last_prices.get((username, key), (None, None))
        if scanner.is_fractal(a):
            out.append({
                **a, "key": key, "levels": [], "hit_count": 0, "level": 0, "label": "", "is_close": False,
                "price": price, "price_at": as_of, "rail": None, "fractal": _fractal_view(a, price),
                "expired": bool(a.get("expiry")) and a["expiry"] < today,
                "checked_at": scanner.last_checked_at.get(a["id"]),
                "next": scanner.next_check(a, s, now, tick) if a["status"] == "active" and broker_ok else None,
            })
            continue
        levels = [{**lv, "label": CONDITIONS[lv["condition"]], "is_close": uses_close(lv["condition"]),
                   "rail": rail(lv, price)} for lv in levels_of(a)]
        waiting = [lv for lv in levels if lv["status"] == "active"]
        # The level the price bar and "closest" sort follow: the nearest one still waiting.
        focus = min(waiting or levels, key=lambda lv: (lv["rail"] is None, lv["rail"]["pct"] if lv["rail"] else 0))
        out.append({
            **a, "key": key, "levels": levels, "focus": focus, "hit_count": len(levels) - len(waiting),
            "level": focus["level"], "label": focus["label"], "is_close": any(lv["is_close"] for lv in levels),
            "price": price, "price_at": as_of, "rail": focus["rail"],
            "expired": bool(a.get("expiry")) and a["expiry"] < today,
            "checked_at": scanner.last_checked_at.get(a["id"]),
            "next": scanner.next_check(a, s, now, tick) if a["status"] == "active" and broker_ok else None,
        })
    return out


def _fractal_view(a: dict, price: float | None) -> dict:
    """What the list shows for a fractal alert: its unmitigated levels as of the last scan."""
    found = scanner.fractal_levels.get(a["id"])
    sides = a.get("sides", "both")
    watched = [f for f in (found or {}).get("resistance", []) + (found or {}).get("support", [])
               if sides in ("both", f.side)]
    over = [f for f in watched if f.role == "resistance"]   # nearest first: rising
    under = [f for f in watched if f.role == "support"]     # nearest first: falling
    return {
        "tf": fractals.TIMEFRAMES[a["timeframe"]][0], "scanned": found is not None,
        "trigger": fractals.label(scanner.trigger_timeframe(a)), "min_candles": scanner.min_between(a),
        "last": (a.get("last_hit") or {}).get("text", "").removeprefix(
            f"{a['symbol']} {fractals.label(a['timeframe']).lower()} fractal "),
        "sides": fractals.SIDES[sides], "triggers": [fractals.TRIGGERS[t] for t in a.get("triggers", [])],
        "above": next((f for f in over if price is None or f.level > price), None),
        "below": next((f for f in under if price is None or f.level < price), None),
        "count": len(watched), "all": watched,
    }


def _distance(a: dict) -> float | None:
    """How far, in percent of price, the alert is from firing; 0 once a level has been passed.
    None when there's no price yet or nothing left to watch."""
    if a.get("fractal"):
        price, fr = a["price"], a["fractal"]
        gaps = [abs(f.level - price) / price * 100 for f in (fr["above"], fr["below"]) if f and price]
        return min(gaps, default=None)
    if a["rail"]:
        return 0.0 if a["rail"]["reached"] else a["rail"]["pct"]
    return None


def _groups(alerts: list[dict]) -> list[dict]:
    """The sorted alerts gathered under their instrument, in the order each instrument first appears.
    Price and chart are per instrument, so they show once however many alerts it has."""
    groups: dict[str, dict] = {}
    for a in alerts:
        g = groups.setdefault(a["key"], {"key": a["key"], "symbol": a["symbol"], "name": a.get("name", ""),
                                         "exchange": a.get("exchange"), "price": a["price"], "alerts": [], "levels": [],
                                         "interval": ""})
        g["alerts"].append(a)
        # The candle this alert decides on: a fractal alert's trigger candle, a close-based price alert's timeframe.
        tf = scanner.trigger_timeframe(a) if a.get("fractal") else (a["timeframe"] if a.get("is_close") else "")
        if tf and (not g["interval"] or TIMEFRAMES[tf][1] and (TIMEFRAMES[g["interval"]][1] or 10 ** 6) > TIMEFRAMES[tf][1]):
            g["interval"] = tf
        if a["status"] != "active":
            continue  # a paused or triggered alert isn't watching anything
        if a.get("fractal"):
            g["levels"] += [{"price": f.level, "title": f.role.capitalize() + (" (flipped)" if f.flipped else "")}
                            for f in a["fractal"]["all"]]
        else:
            g["levels"] += [{"price": lv["level"], "title": lv["label"]} for lv in a["levels"] if lv["status"] == "active"]
    for g in groups.values():  # the same price from two alerts is one line
        g["levels"] = list({lv["price"]: lv for lv in g["levels"]}.values())
    return list(groups.values())


def _overview(everything: list[dict]) -> dict:
    """Figures for the line above the list: what is on watch, and what needs attention."""
    active = [a for a in everything if a["status"] == "active"]
    near = [(d, a) for a in active if (d := _distance(a)) is not None]
    nearest = min(near, key=lambda pair: pair[0], default=None)
    return {
        "instruments": len({a["key"] for a in everything}),
        "price_levels": sum(1 for a in active if not a.get("fractal") for lv in a["levels"] if lv["status"] == "active"),
        "fractal_levels": sum(a["fractal"]["count"] for a in active if a.get("fractal")),
        "fractal_alerts": sum(1 for a in active if a.get("fractal")),
        "nearest": {"symbol": nearest[1]["symbol"], "pct": nearest[0]} if nearest else None,
        "unsent": sum(1 for a in active if not a.get("channels") and not a.get("webhooks")),
        "unscanned": sum(1 for a in active if a.get("fractal") and not a["fractal"]["scanned"]),
    }


def _sorted(alerts: list[dict], f: dict) -> list[dict]:
    q = f["q"].lower()
    if q:
        alerts = [a for a in alerts if q in a["symbol"].lower() or q in a.get("name", "").lower()
                  or q in a.get("note", "").lower()]
    if f["tab"] != "all":
        alerts = [a for a in alerts if a["status"] == f["tab"]]
    if f["sort"] == "symbol":
        return sorted(alerts, key=lambda a: (a["symbol"], a["level"] or 0))
    if f["sort"] == "new":
        return sorted(alerts, key=lambda a: a.get("created_at", ""), reverse=True)
    # Closest to level: triggered ones by most recent, then watching ones nearest to firing.
    def near(a):
        if a["status"] == "triggered":
            return (0, -datetime.fromisoformat(a["triggered_at"]).timestamp() if a.get("triggered_at") else 0, "")
        pct = _distance(a)
        return (1 if a["status"] == "active" else 2, 1e9 if pct is None else pct, a["symbol"])
    return sorted(alerts, key=near)


def _page_ctx(request: Request, user: dict) -> dict:
    username = user["username"]
    contacts = notify.load_contacts(username)
    broker = brokers.load(username)
    broker_ok = broker.get("status") == "connected"
    now = now_ist()
    raw = store.list("alerts", user=username)
    everything = _view(raw, username, broker_ok, now)
    f = _filters(request)
    s = load_settings()
    shown = _sorted(everything, f)
    return {
        "alerts": shown,
        "groups": _groups(shown),
        "overview": _overview(everything),
        "total": len(everything),
        "counts": {**{k: sum(1 for a in everything if a["status"] == k) for k in ("active", "triggered", "paused")},
                   "all": len(everything)},
        "filters": f, "tabs": TABS, "sorts": SORTS,
        "scan": {
            "running": scanner.scheduler.running,
            "in_window": in_scan_window(s, now),
            "last": scanner.last_run.get("at"),
            "checked": scanner.last_run.get("alerts", 0),
            "next": scanner.next_tick() if in_scan_window(s, now) else scanner.next_open(s, now),
        },
        "broker_ok": broker_ok,
        "conditions": CONDITIONS,
        "max_levels": MAX_LEVELS,
        **_fractal_options(),
        "timeframes": [t for t in TIMEFRAMES if t != "1m"] + ["1m"],
        "channels": [{"key": k, "label": v, "ready": notify.sender_ready(k) and notify.recipient_ready(k, contacts)}
                     for k, v in notify.CHANNELS.items()],
        "broker": broker,
    }


@router.get("")
def page(request: Request, user: dict = Depends(guard)):
    return render(request, "alerts.html", _page_ctx(request, user))


@router.get("/list")
def list_partial(request: Request, user: dict = Depends(guard)):
    return render(request, "partials/alert_list.html", _page_ctx(request, user))


@router.get("/symbols")
def symbols(request: Request, symbol: str = "", user: dict = Depends(guard)):
    query = symbol.strip()
    if not query:
        return HTMLResponse("")  # empty results panel hides itself
    try:
        matches, error = search_instruments(query), None
    except Exception:
        matches, error = [], "Couldn't load the list of stocks and contracts. Try again in a moment."
    return render(request, "partials/symbol_options.html", {"matches": matches, "query": query, "error": error})


def _levels(condition: str, level: float, extra_condition: list[str], extra_level: list[str]):
    """The form's first level plus any 'more levels' rows. Returns (levels, None) or (None, error)."""
    rows = [(condition, level)]
    for cond, raw in zip(extra_condition, extra_level):
        if not raw.strip():
            continue  # a row that was added and left empty
        try:
            rows.append((cond, float(raw)))
        except ValueError:
            return None, f"{raw.strip()[:20]} isn't a price."
    if any(cond not in CONDITIONS for cond, _ in rows):
        return None, "Choose when the alert should fire."
    if any(not 0 < price < float("inf") for _, price in rows):
        return None, "Enter a price level above zero."
    rows = list(dict.fromkeys(rows))  # the same level entered twice counts once
    if len(rows) > MAX_LEVELS:
        return None, f"An alert can have up to {MAX_LEVELS} levels."
    return [{"level": price, "condition": cond, "status": "active"} for cond, price in rows], None


def _build_alert(user: dict, symbol: str, condition: str, level: float, timeframe: str,
                 note: str, channels: list[str], extra_condition: list[str] = (),
                 extra_level: list[str] = (), hooks: str = "", hook_payload: str = "") -> tuple[dict | None, str | None]:
    """Validate the New alert form. Returns (alert, None) or (None, error message)."""
    inst = _instrument(symbol)
    if not inst:
        return None, f"We couldn't find {symbol.strip().upper() or 'that symbol'}. Pick one from the suggestions."
    levels, error = _levels(condition, level, list(extra_condition), list(extra_level))
    if error:
        return None, error
    closes = any(uses_close(lv["condition"]) for lv in levels)
    if closes and timeframe not in TIMEFRAMES:
        return None, "Choose a candle timeframe."
    hook_fields, error = _webhook_fields(hooks, hook_payload)
    if error:
        return None, error
    now = now_ist().isoformat()
    return {
        **hook_fields,
        "id": new_id(),
        "user": user["username"],
        "symbol": inst.symbol,
        "name": inst.name,
        "token": inst.token,
        "exchange": inst.exchange,
        "expiry": inst.expiry,
        "levels": levels,
        "timeframe": timeframe if closes else "",
        "channels": [c for c in notify.CHANNELS if c in channels],
        "note": note.strip()[:140],
        "status": "active",
        "created_at": now,
        "armed_at": now,
    }, None


def _fractal_options() -> dict:
    return {"fractal_timeframes": {k: (*v, fractals.minutes(k)) for k, v in fractals.TIMEFRAMES.items()},
            "fractal_default": fractals.DEFAULT_TIMEFRAME,
            "fractal_triggers": fractals.TRIGGERS, "fractal_sides": fractals.SIDES,
            "min_choices": fractals.MIN_BETWEEN_CHOICES, "min_default": fractals.DEFAULT_MIN_BETWEEN,
            "trigger_candles": [(tf, fractals.label(tf), fractals.minutes(tf)) for tf in TIMEFRAMES if tf != "1d"]}


def _fractal_fields(fractal_timeframe: str, sides: str, triggers: list[str], confirm_timeframe: str = "",
                    min_candles: int = fractals.DEFAULT_MIN_BETWEEN) -> tuple[dict, str | None]:
    """The fractal part of the form as alert fields. Returns (fields, None) or ({}, error)."""
    if fractal_timeframe not in fractals.TIMEFRAMES:
        return {}, "Choose a timeframe to find fractals on."
    if confirm_timeframe == fractal_timeframe:
        confirm_timeframe = ""  # the same candles: nothing separate to remember
    if confirm_timeframe and confirm_timeframe not in fractals.trigger_choices(fractal_timeframe):
        return {}, (f"A {fractals.label(confirm_timeframe).lower()} trigger candle doesn't fit {fractals.label(fractal_timeframe).lower()} "
                    "fractals. Pick a shorter candle that divides into the fractal timeframe.")
    if sides not in fractals.SIDES:
        return {}, "Choose which fractals to watch."
    chosen = [t for t in fractals.TRIGGERS if t in triggers]
    if not chosen:
        return {}, "Choose at least one thing to be alerted about."
    if min_candles not in fractals.MIN_BETWEEN_CHOICES:
        return {}, "Choose how many candles there must be between a fractal and the candle that takes it."
    return {"timeframe": fractal_timeframe, "confirm_timeframe": confirm_timeframe, "sides": sides, "triggers": chosen,
            "min_candles": min_candles}, None


def _build_fractal_alert(user: dict, symbol: str, fractal_timeframe: str, sides: str, triggers: list[str], note: str,
                         channels: list[str], hooks: str = "", hook_payload: str = "", confirm_timeframe: str = "",
                         min_candles: int = fractals.DEFAULT_MIN_BETWEEN) -> tuple[dict | None, str | None]:
    """Validate the New alert form in Fractals mode. Returns (alert, None) or (None, error message)."""
    inst = _instrument(symbol)
    if not inst:
        return None, f"We couldn't find {symbol.strip().upper() or 'that symbol'}. Pick one from the suggestions."
    fields, error = _fractal_fields(fractal_timeframe, sides, triggers, confirm_timeframe, min_candles)
    if error:
        return None, error
    hook_fields, error = _webhook_fields(hooks, hook_payload)
    if error:
        return None, error
    now = now_ist().isoformat()
    return {
        **hook_fields, **fields,
        "id": new_id(), "kind": "fractal", "user": user["username"],
        "symbol": inst.symbol, "name": inst.name, "token": inst.token, "exchange": inst.exchange, "expiry": inst.expiry,
        "levels": [],  # its levels are the instrument's fractals, worked out by the scanner
        "fired": [],
        "channels": [c for c in notify.CHANNELS if c in channels],
        "note": note.strip()[:140],
        "status": "active", "created_at": now, "armed_at": now,
    }, None


def _price_level(raw: str) -> float:
    """The form's first price box; 0 (rejected as a level) if it's empty or not a number."""
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _webhook_fields(hooks: str, hook_payload: str) -> tuple[dict, str | None]:
    """The form's webhook boxes as alert fields. Returns (fields, None) or ({}, error)."""
    urls, error = hooks_module.parse_urls(hooks)
    if error:
        return {}, error
    payload, error = hooks_module.parse_payload(hook_payload)
    if error:
        return {}, error
    if payload and not urls:
        return {}, "Add a webhook URL to send that JSON to, or clear the JSON box."
    return {"webhooks": urls, "webhook_payload": payload}, None


def _instrument(symbol: str):
    try:
        return find_instrument(symbol)
    except Exception:
        return None


def _armed_price(username: str, alert: dict) -> float | None:
    """Price right now, for either-way levels to know which side they start on. None when it
    can't be had (no Kite session); the scanner then goes by the candle the alert was armed in."""
    if not any(lv["condition"] in EITHER_WAY for lv in levels_of(alert)):
        return None
    try:
        q = prices.quote(username, find_instrument(alert_key(alert)))
        return q["price"] if q else None
    except Exception:
        return None


@router.get("/quote")
def quote(request: Request, symbol: str = "", user: dict = Depends(guard)):
    """Price strip under the Stock box. Empty when nothing valid is selected."""
    inst = _instrument(symbol)
    if not inst:
        return HTMLResponse("")
    ctx = {"inst": inst, "q": None, "error": None, "needs_login": False}
    try:
        ctx["q"] = prices.quote(user["username"], inst)
    except KiteAuthError:
        ctx["needs_login"] = True
    except KiteError as e:
        ctx["error"] = str(e)
    return render(request, "partials/quote.html", ctx)


@router.get("/chart")
def chart_data(symbol: str, range: str = "5D", interval: str = "", user: dict = Depends(guard)):
    inst = _instrument(symbol)
    if not inst:
        return JSONResponse({"error": f"{symbol} isn't a symbol we know."}, status_code=404)
    try:
        data = prices.chart(user["username"], inst, range, interval)
    except KiteAuthError:
        return JSONResponse({"error": "Connect Kite on the Broker page to see charts."}, status_code=409)
    except KiteError as e:
        return JSONResponse({"error": f"Kite: {e}"}, status_code=502)
    return {**data, "name": _company(inst.name), "hits": _chart_hits(user["username"], inst, data)}


def _event_signal(e: dict) -> str:
    """Buy or sell for a hit; older records didn't store it, so read it off their wording."""
    if e.get("signal"):
        return e["signal"]
    text = e.get("summary", "")
    if "as resistance" in text or ("as support" not in text and (" above " in text or "fractal high" in text)):
        return "sell"
    return "buy"


def _chart_hits(username: str, inst, chart: dict, limit: int = 8) -> list[dict]:
    """The most recent alert hits on this instrument that fall inside the chart, each pinned to the
    candle it happened in, oldest first (the order the chart wants them)."""
    candles = chart["candles"]
    if not candles:
        return []
    times = [c["time"] for c in candles]
    # A hit after the last candle (the chart is a little behind) has no candle to sit on yet.
    newest = times[-1] if chart["daily"] else times[-1] + (times[-1] - times[-2] if len(times) > 1 else 300)
    mine = [e for e in store.list("events", user=username)
            if e.get("key") == inst.key or (not e.get("key") and e.get("symbol") == inst.symbol)]
    out = []
    for e in sorted(mine, key=lambda e: e["at"], reverse=True):
        at = datetime.fromisoformat(e["at"]).astimezone(prices.IST)
        t = at.date().isoformat() if chart["daily"] else int(at.timestamp()) + prices.IST_OFFSET
        if t > newest:
            continue
        i = bisect_right(times, t) - 1
        if i < 0:
            break  # older than the chart, and so is everything after it
        level = e.get("level")
        out.append({"time": times[i], "price": e["price"], "signal": _event_signal(e),
                    "label": f"{level if level is not None else e['price']:,.2f}".rstrip("0").rstrip("."),
                    "summary": e.get("summary", ""), "at": at.strftime("%a %-d %b, %-I:%M %p")})
        if len(out) == limit:
            break
    return out[::-1]


@router.post("")
def create(
    request: Request,
    user: dict = Depends(guard),
    symbol: str = Form(...),
    kind: str = Form("price"),
    condition: str = Form(""),
    level: str = Form(""),
    timeframe: str = Form("15m"),
    note: str = Form(""),
    channels: list[str] = Form([]),
    extra_condition: list[str] = Form([]),
    extra_level: list[str] = Form([]),
    webhooks: str = Form(""),
    webhook_payload: str = Form(""),
    fractal_timeframe: str = Form(fractals.DEFAULT_TIMEFRAME),
    confirm_timeframe: str = Form(""),
    sides: str = Form("both"),
    triggers: list[str] = Form([]),
    min_candles: int = Form(fractals.DEFAULT_MIN_BETWEEN),
):
    if kind == "fractal":
        alert, error = _build_fractal_alert(user, symbol, fractal_timeframe, sides, triggers, note, channels,
                                            webhooks, webhook_payload, confirm_timeframe, min_candles)
    else:
        alert, error = _build_alert(user, symbol, condition, _price_level(level), timeframe, note, channels,
                                    extra_condition, extra_level, webhooks, webhook_payload)
    if error:
        return fail(error)
    alert["armed_price"] = _armed_price(user["username"], alert)
    store.put("alerts", alert["id"], alert)
    # Show the new alert even if the user was on another tab or searching.
    request.session["alert_filters"] = {**request.session.get("alert_filters", {}), "tab": "active", "q": ""}
    ctx = _page_ctx(request, user)
    ctx["fresh_id"] = alert["id"]
    count = len(alert["levels"])
    what = f" {fractals.TIMEFRAMES[alert['timeframe']][0].lower()} fractals" if kind == "fractal" else ""
    return toast(render(request, "partials/alert_list.html", ctx),
                 f"Watching {alert['symbol']}{what}" + (f" at {count} levels" if count > 1 else ""))


@router.post("/simulate")
def simulate(
    request: Request,
    user: dict = Depends(guard),
    symbol: str = Form(...),
    kind: str = Form("price"),
    condition: str = Form(""),
    level: str = Form(""),
    timeframe: str = Form("15m"),
    note: str = Form(""),
    channels: list[str] = Form([]),
    extra_condition: list[str] = Form([]),
    extra_level: list[str] = Form([]),
    fractal_timeframe: str = Form(fractals.DEFAULT_TIMEFRAME),
    confirm_timeframe: str = Form(""),
    sides: str = Form("both"),
    triggers: list[str] = Form([]),
    min_candles: int = Form(fractals.DEFAULT_MIN_BETWEEN),
):
    """Replay the form's alert on the last trading day with real Kite data and send the
    result to the chosen channels. Nothing is saved. In Fractals mode: a backtest over the
    sessions fractals are searched on, shown on the page."""
    if kind == "fractal":
        return _fractal_backtest(request, user, symbol, fractal_timeframe, sides, triggers, confirm_timeframe, min_candles)
    alert, error = _build_alert(user, symbol, condition, _price_level(level), timeframe, note, channels,
                                extra_condition, extra_level)
    if error:
        return fail(error)
    if not alert["channels"]:
        return fail("Pick at least one place to send the simulation to.")
    try:
        client = brokers.client_for(user["username"])
        result = scanner.simulate(alert, client, load_settings(), now_ist())
    except KiteAuthError:
        return fail("Kite isn't connected or the session has expired. Log in on the Broker page first.")
    except KiteError as e:
        return fail(f"Couldn't get prices from Kite: {e}")
    if not result:
        return fail(f"Kite returned no candles for {alert['symbol']} in the last week.")
    subject, body = scanner.simulation_message(alert, result)
    delivery = notify.send(user["username"], alert["channels"], subject, body)
    sent = [notify.CHANNELS[c] for c, r in delivery.items() if r == "sent"]
    failed = {notify.CHANNELS[c]: r for c, r in delivery.items() if r != "sent"}
    ctx = {"alert": alert, "result": result, "sent": sent, "failed": failed, "conditions": CONDITIONS}
    msg = f"Simulation sent to {', '.join(sent)}" if sent else "Simulation ran, but the message couldn't be sent"
    return toast(render(request, "partials/simulation.html", ctx), msg, "success" if sent else "error")


def _fractal_backtest(request: Request, user: dict, symbol: str, fractal_timeframe: str, sides: str, triggers: list[str],
                      confirm_timeframe: str = "", min_candles: int = fractals.DEFAULT_MIN_BETWEEN):
    alert, error = _build_fractal_alert(user, symbol, fractal_timeframe, sides, triggers, "", [],
                                        confirm_timeframe=confirm_timeframe, min_candles=min_candles)
    if error:
        return fail(error)
    s, now, tf, trigger_tf = load_settings(), now_ist(), alert["timeframe"], scanner.trigger_timeframe(alert)
    try:
        client = brokers.client_for(user["username"])
        history = scanner.fractal_candles(client, alert, s, now)
        if len(history) < 3:
            return fail(f"Kite returned too few candles for {alert['symbol']} to find fractals.")
        trigger_candles = None if trigger_tf == tf else scanner.fractal_trigger_candles(
            client, alert, history[0].start.date(), s, now)
    except KiteAuthError:
        return fail("Kite isn't connected or the session has expired. Log in on the Broker page first.")
    except KiteError as e:
        return fail(f"Couldn't get prices from Kite: {e}")
    hits, unmitigated, stream, first, _ = scanner.fractal_run(alert, history, trigger_candles, s)
    if first >= len(stream):
        return fail(f"Kite returned no {fractals.label(trigger_tf).lower()} candles for {alert['symbol']}.")
    daily = trigger_tf == "1d"

    def chart_time(c: Candle):
        return c.start.astimezone(prices.IST).date().isoformat() if daily else int(c.start.timestamp()) + prices.IST_OFFSET

    # Signals come only from the trigger candles; anything earlier just settles what was already mitigated.
    shown = stream[first:][-6000:]  # what the chart draws
    rows = []
    for h in hits:
        if h.index < first or not scanner.fractal_wanted(alert, h):
            continue
        rows.append({"hit": h, "outcome": fractals.outcome(h, stream),
                     # A touch is dated by the candle it happened in, the others by that candle's close.
                     "when": h.candle.start if h.trigger == "touch" else scanner.candle_end(h.candle, trigger_tf, s),
                     "n": None})
    signals = []
    for r in rows:
        h = r["hit"]
        if h.candle.start >= shown[0].start:
            r["n"] = len(signals)
            signals.append({"time": chart_time(h.candle), "signal": h.signal, "level": h.fractal.level,
                            "side": h.fractal.side + (f", now {h.fractal.role}" if h.fractal.flipped else ""),
                            "target": h.target.level if h.target else None,
                            "stop": r["outcome"].stop,
                            "label": f"{scanner.FRACTAL_OUTCOME[h.trigger].capitalize()} {h.fractal.level:g}"})
    results = [r["outcome"].result for r in rows]
    # Points: target hit earns entry-to-target, SL gone loses entry-to-SL. Added up in the order the trades were taken.
    running = 0.0
    for r in rows:
        r["points"] = r["outcome"].points
        running += r["points"] or 0
        r["running"] = running
    scored = [r["points"] for r in rows if r["points"] is not None]
    ctx = {
        "alert": alert, "tf_label": fractals.label(tf), "trigger_label": fractals.label(trigger_tf),
        "same_candles": trigger_tf == tf, "daily": daily, "fractal_daily": tf == "1d",
        "min_candles": alert["min_candles"],
        "first": stream[first].start, "last": stream[-1].start, "sessions": len({c.start.date() for c in stream[first:]}),
        "rows": rows[::-1][:60], "total": len(rows),
        "points": {"earned": sum(p for p in scored if p > 0), "lost": -sum(p for p in scored if p < 0),
                   "net": sum(scored), "trades": len(scored)},
        "tally": {"target": results.count("target"), "stop": results.count("stop"),
                  "open": results.count("open") + results.count("none"),
                  "late": sum(1 for r in rows if r["outcome"].reached_after_stop)},
        "resistance": sorted((f for f in unmitigated if f.role == "resistance"), key=lambda f: f.level),
        "support": sorted((f for f in unmitigated if f.role == "support"), key=lambda f: f.level, reverse=True),
        "chart": {"daily": daily, "signals": signals,
                  "candles": [{"time": chart_time(c), "open": c.open, "high": c.high, "low": c.low, "close": c.close}
                              for c in shown]},
    }
    return toast(render(request, "partials/fractal_backtest.html", ctx), f"Backtest ready: {len(rows)} signal{'s' if len(rows) != 1 else ''}")


def _own(alert_id: str, user: dict) -> dict | None:
    a = store.get("alerts", alert_id)
    return a if a and a["user"] == user["username"] else None


@router.post("/{alert_id}/rearm")
def rearm(request: Request, alert_id: str, user: dict = Depends(guard)):
    if a := _own(alert_id, user):
        if a.get("expiry") and a["expiry"] < now_ist().date().isoformat():
            return fail(f"{a['symbol']} expired on {a['expiry']}. Add an alert on a current contract instead.")
        changes = {"status": "active", "armed_at": now_ist().isoformat(), "triggered_at": None, "trigger_price": None,
                   "armed_price": _armed_price(user["username"], a)}
        # Watch again puts every level back on watch. Resuming a paused alert leaves fired levels off.
        if a["status"] == "triggered" and "levels" in a:
            changes["levels"] = [{"level": lv["level"], "condition": lv["condition"], "status": "active"}
                                 for lv in a["levels"]]
        store.update("alerts", alert_id, changes)
        return toast(render(request, "partials/alert_list.html", _page_ctx(request, user)), f"{a['symbol']} is armed again")
    return HTMLResponse(status_code=404)


@router.get("/{alert_id}/edit")
def edit_form(request: Request, alert_id: str, user: dict = Depends(guard)):
    a = _own(alert_id, user)
    if not a:
        return HTMLResponse(status_code=404)
    payload = json.dumps(json.loads(a["webhook_payload"]), indent=2) if a.get("webhook_payload") else ""
    if scanner.is_fractal(a):
        return render(request, "partials/alert_fractal_edit.html", {
            "a": a, "hooks": a.get("webhooks") or [], "hook_payload": payload, **_fractal_options()})
    return render(request, "partials/alert_edit.html", {
        "a": a, "levels": levels_of(a), "conditions": CONDITIONS, "max_levels": MAX_LEVELS,
        "hooks": a.get("webhooks") or [], "hook_payload": payload,
        "timeframes": [t for t in TIMEFRAMES if t != "1m"] + ["1m"]})


@router.post("/{alert_id}/edit")
def edit(
    request: Request,
    alert_id: str,
    user: dict = Depends(guard),
    extra_index: list[str] = Form([]),
    extra_condition: list[str] = Form([]),
    extra_level: list[str] = Form([]),
    rearm: list[str] = Form([]),
    timeframe: str = Form("15m"),
    note: str = Form(""),
    webhooks: str = Form(""),
    webhook_payload: str = Form(""),
    fractal_timeframe: str = Form(fractals.DEFAULT_TIMEFRAME),
    confirm_timeframe: str = Form(""),
    sides: str = Form("both"),
    triggers: list[str] = Form([]),
    min_candles: int = Form(fractals.DEFAULT_MIN_BETWEEN),
):
    """Save the Edit panel: levels changed, removed, added or put back on watch."""
    a = _own(alert_id, user)
    if not a:
        return HTMLResponse(status_code=404)
    if scanner.is_fractal(a):
        fields, error = _fractal_fields(fractal_timeframe, sides, triggers, confirm_timeframe, min_candles)
        if not error:
            hook_fields, error = _webhook_fields(webhooks, webhook_payload)
        if error:
            return fail(error)
        changes = {**fields, **hook_fields, "note": note.strip()[:140]}
        if fields["timeframe"] != a["timeframe"]:
            # Different candles, different fractals: start afresh from now.
            changes.update(fired=[], armed_at=now_ist().isoformat())
            scanner.fractal_levels.pop(alert_id, None)
        store.update("alerts", alert_id, changes)
        return toast(render(request, "partials/alert_list.html", _page_ctx(request, user)), f"{a['symbol']} fractal alert updated")
    old, levels, fresh = levels_of(a), [], False
    for index, cond, raw in zip(extra_index, extra_condition, extra_level):
        if not raw.strip():
            continue  # a row that was added and left empty
        try:
            price = float(raw)
        except ValueError:
            return fail(f"{raw.strip()[:20]} isn't a price.")
        if cond not in CONDITIONS:
            return fail("Choose when each level should fire.")
        if not 0 < price < float("inf"):
            return fail("Enter a price level above zero.")
        if any((lv["condition"], lv["level"]) == (cond, price) for lv in levels):
            continue  # the same level entered twice counts once
        was = old[int(index)] if index.isdigit() and int(index) < len(old) else None
        # A level left as it was keeps its state, fired or waiting. Anything new or changed starts watching now.
        if was and (was["condition"], float(was["level"])) == (cond, price) and not (was["status"] == "hit" and index in rearm):
            levels.append(was)
        else:
            levels.append({"level": price, "condition": cond, "status": "active"})
            fresh = True
    if not levels:
        return fail("An alert needs at least one level. Remove the alert if you no longer want it.")
    if len(levels) > MAX_LEVELS:
        return fail(f"An alert can have up to {MAX_LEVELS} levels.")
    closes = any(uses_close(lv["condition"]) for lv in levels)
    if closes and timeframe not in TIMEFRAMES:
        return fail("Choose a candle timeframe.")
    hook_fields, error = _webhook_fields(webhooks, webhook_payload)
    if error:
        return fail(error)
    changes = {"levels": levels, "timeframe": timeframe if closes else "", "note": note.strip()[:140], **hook_fields}
    waiting = any(lv["status"] == "active" for lv in levels)
    if fresh:
        # Re-arm from now so a new level can't fire on a move that happened before it was added.
        changes["armed_at"] = now_ist().isoformat()
        changes["armed_price"] = _armed_price(user["username"], {**a, **changes})
    if a["status"] == "triggered" and waiting:
        changes.update(status="active", triggered_at=None, trigger_price=None)
    elif a["status"] == "active" and not waiting:  # only fired levels are left
        last = max(levels, key=lambda lv: lv.get("hit_at") or "")
        changes.update(status="triggered", triggered_at=last.get("hit_at"), trigger_price=last.get("hit_price"))
    store.update("alerts", alert_id, changes)
    return toast(render(request, "partials/alert_list.html", _page_ctx(request, user)), f"{a['symbol']} alert updated")


@router.post("/{alert_id}/webhooks/test")
def webhook_test(alert_id: str, user: dict = Depends(guard), webhooks: str = Form(""), webhook_payload: str = Form(""),
                 note: str = Form("")):
    """POST a sample hit, marked as a test, to the URLs currently in the Edit panel (saved or not)."""
    a = _own(alert_id, user)
    if not a:
        return HTMLResponse(status_code=404)
    fields, error = _webhook_fields(webhooks, webhook_payload)
    if error:
        return fail(error)
    if not fields["webhooks"]:
        return fail("Add a webhook URL first.")
    sample = {**a, **fields}
    if scanner.is_fractal(a):
        known = scanner.fractal_levels.get(a["id"], {})
        f = (known.get("resistance") or known.get("support") or [fractals.Fractal("high", 0.0, now_ist())])[0]
        sample_hit = fractals.Hit(f, "touch", Candle(now_ist(), f.level, f.level, f.level, f.level), -1, f.level)
        message = note.strip()[:140] or f"Potential {sample_hit.signal}"
        text = f"Test: {scanner.fractal_text(sample, sample_hit)[0]} Nothing was hit."
        body = scanner.fractal_webhook_body(sample, sample_hit, message, text, f.level, now_ist(), test=True)
        return _webhook_test_result(fields["webhooks"], body)
    hit = scanner.resolved(scanner.level_view(sample, levels_of(sample)[0]), float("inf"))
    up = hit["condition"] in ("high_above", "close_above")
    message = note.strip()[:140] or ("Potential sell" if up else "Potential buy")
    text = f"Test: {scanner.describe(hit)}. Nothing was hit."
    body = scanner.webhook_body(sample, hit, message, text, hit["level"], None, now_ist(), test=True)
    return _webhook_test_result(fields["webhooks"], body)


def _webhook_test_result(urls: list[str], body: dict):
    result = hooks_module.send(urls, body)
    if result != "sent":
        return fail(f"Webhook test failed. {result}")
    count = len(urls)
    return toast(HTMLResponse("", headers={"HX-Reswap": "none"}), f"Test request sent to {count} webhook URL{'s' if count != 1 else ''}")


@router.post("/{alert_id}/pause")
def pause(request: Request, alert_id: str, user: dict = Depends(guard)):
    if a := _own(alert_id, user):
        store.update("alerts", alert_id, {"status": "paused"})
        return toast(render(request, "partials/alert_list.html", _page_ctx(request, user)), f"{a['symbol']} paused")
    return HTMLResponse(status_code=404)


@router.post("/{alert_id}/channels/{channel}")
def toggle_channel(request: Request, alert_id: str, channel: str, user: dict = Depends(guard)):
    a = _own(alert_id, user)
    if not a or channel not in notify.CHANNELS:
        return HTMLResponse(status_code=404)
    chosen = set(a.get("channels", [])) ^ {channel}
    store.update("alerts", alert_id, {"channels": [c for c in notify.CHANNELS if c in chosen]})
    label = notify.CHANNELS[channel]
    msg = f"{a['symbol']}: {label} {'on' if channel in chosen else 'off'}"
    return toast(render(request, "partials/alert_list.html", _page_ctx(request, user)), msg)


@router.delete("/{alert_id}")
def delete(request: Request, alert_id: str, user: dict = Depends(guard)):
    if a := _own(alert_id, user):
        store.delete("alerts", alert_id)
        return toast(render(request, "partials/alert_list.html", _page_ctx(request, user)), f"Removed {a['symbol']} alert")
    return HTMLResponse(status_code=404)
