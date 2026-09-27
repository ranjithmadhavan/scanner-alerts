from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app import brokers, notify
from app import scanner
from app.kite import TIMEFRAMES, KiteAuthError, KiteError, instruments, search_instruments
from app.market import load_settings, now_ist
from app.scanner import CONDITIONS, last_prices, uses_close
from app.security import require
from app.store import new_id, store
from app.web import fail, render, toast

router = APIRouter(prefix="/alerts")
guard = require("scanner")

_STATUS_ORDER = {"active": 0, "triggered": 1, "paused": 2}


def rail(alert: dict, price: float | None) -> dict | None:
    """Where to draw the last price relative to the level on a ±3% scale."""
    if price is None:
        return None
    level = float(alert["level"])
    pct = (price - level) / level * 100
    pos = 50 + max(-3.0, min(3.0, pct)) / 3.0 * 44
    wants_up = alert["condition"] in ("close_above", "high_above")
    reached = price > level if wants_up else price < level
    return {"pos": round(pos, 1), "pct": abs(pct), "gap": abs(price - level), "reached": reached,
            "side": "above" if price >= level else "below"}


def _view(alerts: list[dict], username: str) -> list[dict]:
    out = []
    for a in sorted(alerts, key=lambda a: (_STATUS_ORDER.get(a["status"], 9), a["symbol"])):
        price, as_of = last_prices.get((username, a["symbol"]), (None, None))
        out.append({**a, "label": CONDITIONS[a["condition"]], "is_close": uses_close(a["condition"]),
                    "price": price, "price_at": as_of, "rail": rail(a, price)})
    return out


def _page_ctx(user: dict) -> dict:
    username = user["username"]
    contacts = notify.load_contacts(username)
    alerts = store.list("alerts", user=username)
    return {
        "alerts": _view(alerts, username),
        "counts": {s: sum(1 for a in alerts if a["status"] == s) for s in _STATUS_ORDER},
        "conditions": CONDITIONS,
        "timeframes": [t for t in TIMEFRAMES if t != "1m"] + ["1m"],
        "channels": [{"key": k, "label": v, "ready": notify.sender_ready(k) and notify.recipient_ready(k, contacts)}
                     for k, v in notify.CHANNELS.items()],
        "broker": brokers.load(username),
    }


@router.get("")
def page(request: Request, user: dict = Depends(guard)):
    return render(request, "alerts.html", _page_ctx(user))


@router.get("/list")
def list_partial(request: Request, user: dict = Depends(guard)):
    return render(request, "partials/alert_list.html", _page_ctx(user))


@router.get("/symbols")
def symbols(request: Request, symbol: str = "", user: dict = Depends(guard)):
    query = symbol.strip()
    if not query:
        return HTMLResponse("")  # empty results panel hides itself
    try:
        matches, error = search_instruments(query), None
    except Exception:
        matches, error = [], "Couldn't load the NSE stock list. Try again in a moment."
    return render(request, "partials/symbol_options.html", {"matches": matches, "query": query, "error": error})


def _build_alert(user: dict, symbol: str, condition: str, level: float, timeframe: str,
                 note: str, channels: list[str]) -> tuple[dict | None, str | None]:
    """Validate the New alert form. Returns (alert, None) or (None, error message)."""
    symbol = symbol.strip().upper()
    try:
        inst = instruments().get(symbol)
    except Exception:
        inst = None
    if not inst:
        return None, f"We couldn't find {symbol or 'that symbol'} on NSE. Pick one from the suggestions."
    if condition not in CONDITIONS:
        return None, "Choose when the alert should fire."
    if level <= 0:
        return None, "Enter a price level above zero."
    if uses_close(condition) and timeframe not in TIMEFRAMES:
        return None, "Choose a candle timeframe."
    now = now_ist().isoformat()
    return {
        "id": new_id(),
        "user": user["username"],
        "symbol": inst.symbol,
        "name": inst.name,
        "token": inst.token,
        "condition": condition,
        "level": level,
        "timeframe": timeframe if uses_close(condition) else "",
        "channels": [c for c in notify.CHANNELS if c in channels],
        "note": note.strip()[:140],
        "status": "active",
        "created_at": now,
        "armed_at": now,
    }, None


@router.post("")
def create(
    request: Request,
    user: dict = Depends(guard),
    symbol: str = Form(...),
    condition: str = Form(...),
    level: float = Form(...),
    timeframe: str = Form("15m"),
    note: str = Form(""),
    channels: list[str] = Form([]),
):
    alert, error = _build_alert(user, symbol, condition, level, timeframe, note, channels)
    if error:
        return fail(error)
    store.put("alerts", alert["id"], alert)
    ctx = _page_ctx(user)
    ctx["fresh_id"] = alert["id"]
    return toast(render(request, "partials/alert_list.html", ctx), f"Watching {alert['symbol']}")


@router.post("/simulate")
def simulate(
    request: Request,
    user: dict = Depends(guard),
    symbol: str = Form(...),
    condition: str = Form(...),
    level: float = Form(...),
    timeframe: str = Form("15m"),
    note: str = Form(""),
    channels: list[str] = Form([]),
):
    """Replay the form's alert on the last trading day with real Kite data and send the
    result to the chosen channels. Nothing is saved."""
    alert, error = _build_alert(user, symbol, condition, level, timeframe, note, channels)
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
    ctx = {"alert": alert, "result": result, "sent": sent, "failed": failed}
    msg = f"Simulation sent to {', '.join(sent)}" if sent else "Simulation ran, but the message couldn't be sent"
    return toast(render(request, "partials/simulation.html", ctx), msg, "success" if sent else "error")


def _own(alert_id: str, user: dict) -> dict | None:
    a = store.get("alerts", alert_id)
    return a if a and a["user"] == user["username"] else None


@router.post("/{alert_id}/rearm")
def rearm(request: Request, alert_id: str, user: dict = Depends(guard)):
    if a := _own(alert_id, user):
        store.update("alerts", alert_id, {"status": "active", "armed_at": now_ist().isoformat(),
                                          "triggered_at": None, "trigger_price": None})
        return toast(render(request, "partials/alert_list.html", _page_ctx(user)), f"{a['symbol']} is armed again")
    return HTMLResponse(status_code=404)


@router.post("/{alert_id}/pause")
def pause(request: Request, alert_id: str, user: dict = Depends(guard)):
    if a := _own(alert_id, user):
        store.update("alerts", alert_id, {"status": "paused"})
        return toast(render(request, "partials/alert_list.html", _page_ctx(user)), f"{a['symbol']} paused")
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
    return toast(render(request, "partials/alert_list.html", _page_ctx(user)), msg)


@router.delete("/{alert_id}")
def delete(request: Request, alert_id: str, user: dict = Depends(guard)):
    if a := _own(alert_id, user):
        store.delete("alerts", alert_id)
        return toast(render(request, "partials/alert_list.html", _page_ctx(user)), f"Removed {a['symbol']} alert")
    return HTMLResponse(status_code=404)
