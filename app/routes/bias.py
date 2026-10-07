import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import bias, brokers, fractals, notify, oi, prices, webhooks
from app.kite import KiteAuthError, KiteError
from app.market import is_market_open, load_settings as market_settings, now_ist
from app.security import require
from app.store import store
from app.web import fail, render, toast

router = APIRouter(prefix="/bias")
guard = require("bias")


def _notify_ctx(user: dict, p: dict | None = None) -> dict:
    p = p or bias.prefs(user["username"])
    return {"np": p, "np_hooks": "\n".join(p["webhooks"]),
            "np_payload": json.dumps(json.loads(p["webhook_payload"]), indent=2) if p["webhook_payload"] else "",
            "tg_ready": notify.recipient_ready("telegram", notify.load_contacts(user["username"]))}


def _counted(cfg: dict) -> dict:
    """The settings a count was made with, in words."""
    return {"names": ", ".join(bias.TRIGGER_NAMES[t] for t in cfg["triggers"]).lower(), "min_candles": cfg["min_candles"],
            "trigger_tf": cfg.get("trigger_tf", "15m")}  # counts from before the setting existed were on 15-minute candles


@router.get("")
def page(request: Request, user: dict = Depends(guard), day: str = "", at: str = ""):
    days = sorted(store.list("bias_days"), key=lambda d: d["date"], reverse=True)[:60]
    day = day or (days[0]["date"] if days else "")
    day_doc = next((d for d in days if d["date"] == day), None) or store.get("bias_days", day) or {}
    # Only the count being looked at and the one before it are read; the day's summary has the rest.
    slots = sorted(day_doc.get("slots", []))
    sid = at if at in slots else (slots[-1] if slots else "")
    chosen = store.get("bias_snapshots", sid) if sid else None
    before = bias.previous(chosen) if chosen else None
    seen_before = {g["key"]: g["status"] for g in before["signals"]} if before else {}
    extremes_only = bias.prefs(user["username"])["extremes_only"]
    ctx = {
        "days": days, "day": day, "chosen": chosen, "extremes_only": extremes_only,
        "listed": bias.shown(chosen["signals"], extremes_only) if chosen else [],
        "reading": bias.read(chosen["signals"]) if chosen else None,
        "counted": _counted(chosen["settings"] if chosen else bias.load_settings()),
        "seen_before": seen_before, "has_before": before is not None,
        # The Nifty OI reading at the same moment (latest today if nothing is chosen), for those who can see it.
        "oi_now": oi.sentiment(user["username"], datetime.fromisoformat(chosen["at"]) if chosen else now_ist()),
        "series": bias.series(day_doc),
        "chart_source": f"/bias/chart?snap={chosen['id']}" if chosen else "/bias/chart", "chart_named_lines": True, "settings": bias.load_settings(),
        "trigger_choices": [(k, bias.TRIGGER_NAMES[k], fractals.TRIGGERS[k]) for k in bias.TRIGGER_CHOICES],
        "trigger_names": bias.TRIGGER_NAMES, "min_choices": fractals.MIN_BETWEEN_CHOICES, "sessions": bias.SESSIONS,
        "trigger_tfs": bias.TRIGGER_TFS, "every": bias.EVERY,
        "broker_ok": brokers.load(user["username"]).get("status") == "connected",
        "is_admin": user.get("role") == "superadmin",
        "times": [(x, bias.slot_time(x).removesuffix(" AM").removesuffix(" PM")) for x in slots],
        **_notify_ctx(user),
    }
    return render(request, "bias.html", ctx)


@router.get("/chart")
def chart_data(symbol: str, snap: str = "", range: str = "5D", interval: str = "", user: dict = Depends(guard)):
    """A Nifty 50 stock's candles, with the day's fractal signals on it as of the count `snap`."""
    try:
        inst = bias.instruments().get(symbol)
    except KiteError as e:
        return JSONResponse({"error": f"Kite: {e}"}, status_code=502)
    if not inst:
        return JSONResponse({"error": f"{symbol} isn't a symbol we know."}, status_code=404)
    try:
        data = prices.chart(user["username"], inst, range, interval)
    except KiteAuthError:
        return JSONResponse({"error": "Connect Kite on the Broker page to see charts."}, status_code=409)
    except KiteError as e:
        return JSONResponse({"error": f"Kite: {e}"}, status_code=502)
    hits = []
    for g in (store.get("bias_snapshots", snap) or {}).get("signals", []):
        t = prices.pin(data, datetime.fromisoformat(g["at"]) - timedelta(minutes=1)) if g["symbol"] == symbol else None
        if t is None:
            continue
        since = (f"stopped out {_hm(g['until'])}" if g["status"] == "stopped" else
                 f"at target {g['target']:,.2f}" if g["status"] == "target" else "holding")
        hits.append({"time": t, "price": g["price"], "signal": g["signal"], "label": f"{g['level']:,.2f}",
                     "summary": f"{bias.TRIGGER_NAMES[g['trigger']]} of the 30 min fractal {g['side']} {g['level']:,.2f}, {since}",
                     "at": datetime.fromisoformat(g["at"]).strftime("%a %-d %b, %-I:%M %p")})
    return {**data, "name": inst.name.title(), "hits": sorted(hits, key=lambda h: str(h["time"]))}


def _hm(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%-I:%M %p")


@router.post("/capture")
def capture_now(request: Request, user: dict = Depends(guard)):
    """Recount now with this user's Kite session, outside the schedule."""
    now = now_ist()
    if not is_market_open(market_settings(), now):
        return fail("The market is closed. Fractal bias is counted from the open to the close.")
    try:
        snap = bias.capture(brokers.client_for(user["username"]), now)
    except KiteAuthError:
        return fail("Kite isn't connected or the session has expired. Log in on the Broker page first.")
    except KiteError as e:
        return fail(f"Couldn't get candles from Kite: {e}")
    bias.notify_all(snap)
    response = HTMLResponse("", headers={"HX-Redirect": f"/bias?day={snap['date']}&at={snap['id']}"})
    return toast(response, f"Counted: {len(snap['signals'])} signals, {snap['label'].lower()}")


def _prefs_from_form(user: dict, changes: str, signals: str, telegram: str, hooks: str, payload: str) -> tuple[dict, str | None]:
    urls, error = webhooks.parse_urls(hooks)
    if not error:
        payload, error = webhooks.parse_payload(payload)
    if error:
        return {}, error
    if payload and not urls:
        return {}, "Add a webhook URL to send that JSON to, or clear the JSON box."
    if telegram and not notify.recipient_ready("telegram", notify.load_contacts(user["username"])):
        return {}, "Set up Telegram on the Notifications page first."
    return {"changes": bool(changes), "signals": bool(signals), "telegram": bool(telegram),
            "webhooks": urls, "webhook_payload": payload, "extremes_only": bias.prefs(user["username"])["extremes_only"]}, None


@router.post("/notify")
def save_notify(request: Request, user: dict = Depends(guard), changes: str = Form(""), signals: str = Form(""),
                telegram: str = Form(""), webhooks_text: str = Form("", alias="webhooks"), webhook_payload: str = Form("")):
    """What this person hears about (bias changes, new signals) and where (Telegram, their webhooks)."""
    p, error = _prefs_from_form(user, changes, signals, telegram, webhooks_text, webhook_payload)
    if error:
        return fail(error)
    bias.save_prefs(user["username"], p)
    what = [w for w, on in (("bias changes", p["changes"]), ("new signals", p["signals"])) if on]
    where = [w for w, on in (("Telegram", p["telegram"]), (f"{len(p['webhooks'])} webhook{'s' if len(p['webhooks']) != 1 else ''}", p["webhooks"])) if on]
    msg = f"Fractal bias: {' and '.join(what)} to {' and '.join(where)}" if what and where else "Fractal bias notifications are off"
    return toast(render(request, "partials/bias_notify.html", _notify_ctx(user, p)), msg)


@router.post("/extremes")
def set_extremes(user: dict = Depends(guard), on: str = Form("")):
    """All fractal signals, or only those on fractals that are a day's high or low: for this person's table, cards and messages."""
    bias.save_prefs(user["username"], {**bias.prefs(user["username"]), "extremes_only": bool(on)})
    response = HTMLResponse("", headers={"HX-Refresh": "true"})
    return toast(response, "Showing and sending only signals at a day's high or low" if on else "Showing and sending every fractal signal")


@router.post("/notify/test")
def test_notify(user: dict = Depends(guard), webhooks_text: str = Form("", alias="webhooks"), webhook_payload: str = Form("")):
    """Send a sample new-signal request to the webhook URLs in the form, without saving anything."""
    urls, error = webhooks.parse_urls(webhooks_text)
    if not error:
        webhook_payload, error = webhooks.parse_payload(webhook_payload)
    if error or not urls:
        return fail(error or "Add a webhook URL to test.")
    now = now_ist()
    sample = {"symbol": "INFY", "signal": "buy", "trigger": "confirm", "side": "low", "flipped": False, "level": 1500.0,
              "price": 1504.5, "stop": 1497.2, "target": 1528.0, "at": now.isoformat(), "status": "held"}
    snap = {"id": f"{now.date().isoformat()}T{now:%H:%M}", "date": now.date().isoformat(), "at": now.isoformat(), "signals": [sample]}
    body = {**bias.hook_body("fractal_bias_signals", snap, {"webhook_payload": webhook_payload}, signals=[sample]), "test": True}
    result = webhooks.send(urls, body)
    if result != "sent":
        return fail(f"Webhook test failed. {result}")
    return toast(HTMLResponse("", headers={"HX-Reswap": "none"}), f"Test request sent to {len(urls)} webhook URL{'s' if len(urls) != 1 else ''}")


@router.post("/settings")
def save_settings(request: Request, user: dict = Depends(guard), triggers: list[str] = Form([]), min_candles: int = Form(5),
                  trigger_tf: str = Form("5m")):
    if user.get("role") != "superadmin":
        return fail("Only the super admin can change how the bias is counted.")
    triggers = [t for t in bias.TRIGGER_CHOICES if t in triggers]
    if not triggers or min_candles not in fractals.MIN_BETWEEN_CHOICES or trigger_tf not in bias.TRIGGER_TFS:
        return fail("Pick at least one kind of signal, and one of the gaps shown.")
    bias.save_settings(triggers, min_candles, trigger_tf)
    names = ", ".join(bias.TRIGGER_NAMES[t].lower() for t in triggers)
    return toast(HTMLResponse("", headers={"HX-Reswap": "none"}), f"Counting {names}; at least {min_candles} candles after the fractal")
