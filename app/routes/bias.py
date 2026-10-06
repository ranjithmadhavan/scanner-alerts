from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from datetime import datetime, timedelta

from app import bias, brokers, fractals, notify, oi, prices
from app.kite import instruments
from app.kite import KiteAuthError, KiteError
from app.market import is_market_open, load_settings as market_settings, now_ist
from app.security import require
from app.store import store
from app.web import fail, render, toast

router = APIRouter(prefix="/bias")
guard = require("bias")


def _telegram_ctx(user: dict) -> dict:
    return {"tg_on": bias.wants_telegram(user["username"]), "tg_url": "/bias/telegram",
            "tg_text": "The bias at the first snapshot of the day, then a message each time it changes, say from Neutral to Mildly bearish.",
            "tg_ready": notify.recipient_ready("telegram", notify.load_contacts(user["username"]))}


def _counted(cfg: dict) -> dict:
    """The settings a count was made with, in words."""
    return {"names": ", ".join(bias.TRIGGER_NAMES[t] for t in cfg["triggers"]).lower(), "min_candles": cfg["min_candles"]}


@router.get("")
def page(request: Request, user: dict = Depends(guard), day: str = "", at: str = ""):
    days = sorted(store.list("bias_days"), key=lambda d: d["date"], reverse=True)[:60]
    day = day or (days[0]["date"] if days else "")
    snaps = bias.day_snapshots(day) if day else []
    chosen = next((s for s in snaps if s["id"] == at), snaps[-1] if snaps else None)
    before = snaps[snaps.index(chosen) - 1] if chosen and snaps.index(chosen) > 0 else None
    seen_before = {g["key"]: g["status"] for g in before["signals"]} if before else {}
    ctx = {
        "days": days, "day": day, "snaps": snaps, "chosen": chosen,
        "reading": bias.read(chosen["signals"]) if chosen else None,
        "counted": _counted(chosen["settings"] if chosen else bias.load_settings()),
        "seen_before": seen_before, "has_before": before is not None,
        # The Nifty OI reading at the same moment (latest today if nothing is chosen), for those who can see it.
        "oi_now": oi.sentiment(user["username"], datetime.fromisoformat(chosen["at"]) if chosen else now_ist()),
        "series": bias.series(snaps),
        "chart_source": f"/bias/chart?snap={chosen['id']}" if chosen else "/bias/chart", "chart_named_lines": True, "settings": bias.load_settings(),
        "trigger_choices": [(k, bias.TRIGGER_NAMES[k], fractals.TRIGGERS[k]) for k in bias.TRIGGER_CHOICES],
        "trigger_names": bias.TRIGGER_NAMES, "min_choices": fractals.MIN_BETWEEN_CHOICES, "sessions": bias.SESSIONS,
        "broker_ok": brokers.load(user["username"]).get("status") == "connected",
        "is_admin": user.get("role") == "superadmin",
        "times": [(s["id"], bias.slot_time(s["id"]).removesuffix(" AM").removesuffix(" PM")) for s in snaps],
        **_telegram_ctx(user),
    }
    return render(request, "bias.html", ctx)


@router.get("/chart")
def chart_data(symbol: str, snap: str = "", range: str = "5D", interval: str = "", user: dict = Depends(guard)):
    """A Nifty 50 stock's candles, with the day's fractal signals on it as of the count `snap`."""
    inst = instruments().get(symbol)
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
        since = {"stopped": f"stopped out {_hm(g['until'])}", "target": f"at target {g['target']:,.2f}"}.get(g["status"], "holding")
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
    bias.announce(snap)
    response = HTMLResponse("", headers={"HX-Redirect": f"/bias?day={snap['date']}&at={snap['id']}"})
    return toast(response, f"Counted: {len(snap['signals'])} signals, {snap['label'].lower()}")


@router.post("/telegram")
def telegram(request: Request, user: dict = Depends(guard), on: str = Form("")):
    if on and not _telegram_ctx(user)["tg_ready"]:
        return fail("Set up Telegram on the Notifications page first.")
    bias.set_telegram(user["username"], bool(on))
    response = render(request, "partials/telegram_switch.html", _telegram_ctx(user))
    return toast(response, "Fractal bias updates will come on Telegram" if on else "Fractal bias updates on Telegram are off")


@router.post("/settings")
def save_settings(request: Request, user: dict = Depends(guard), triggers: list[str] = Form([]), min_candles: int = Form(5)):
    if user.get("role") != "superadmin":
        return fail("Only the super admin can change how the bias is counted.")
    triggers = [t for t in bias.TRIGGER_CHOICES if t in triggers]
    if not triggers or min_candles not in fractals.MIN_BETWEEN_CHOICES:
        return fail("Pick at least one kind of signal, and one of the gaps shown.")
    bias.save_settings(triggers, min_candles)
    names = ", ".join(bias.TRIGGER_NAMES[t].lower() for t in triggers)
    return toast(HTMLResponse("", headers={"HX-Reswap": "none"}), f"Counting {names}; at least {min_candles} candles after the fractal")
