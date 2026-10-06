from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import brokers, oi
from app.kite import KiteAuthError, KiteError
from app.market import IST, now_ist
from app.security import require
from app.store import store
from app.web import fail, render, toast

router = APIRouter(prefix="/oi")
guard = require("oi")


@router.get("")
def page(request: Request, user: dict = Depends(guard), day: str = "", at: str = ""):
    days = sorted(store.list("oi_days"), key=lambda d: d["date"], reverse=True)[:60]
    day = day or (days[0]["date"] if days else "")
    snaps = oi.day_snapshots(day) if day else []
    chosen = next((s for s in snaps if s["id"] == at), snaps[-1] if snaps else None)
    ctx = {
        "days": days, "day": day, "snaps": snaps, "chosen": chosen,
        "reading": oi.analyse(chosen, snaps[0]) if chosen else None,
        "series": oi.series(snaps), "settings": oi.load_settings(),
        "intervals": oi.INTERVALS, "strike_choices": oi.STRIKE_CHOICES,
        "broker_ok": brokers.load(user["username"]).get("status") == "connected",
        "is_admin": user.get("role") == "superadmin",
        "times": [(s["id"], datetime.fromisoformat(s["at"]).astimezone(IST).strftime("%-I:%M")) for s in snaps],
        "lakhs": oi.lakhs,
    }
    return render(request, "oi.html", ctx)


@router.post("/capture")
def capture_now(request: Request, user: dict = Depends(guard)):
    """Take a snapshot now with this user's Kite session, outside the schedule."""
    try:
        snap = oi.capture(brokers.client_for(user["username"]), now_ist())
    except KiteAuthError:
        return fail("Kite isn't connected or the session has expired. Log in on the Broker page first.")
    except KiteError as e:
        return fail(f"Couldn't get OI from Kite: {e}")
    response = HTMLResponse("", headers={"HX-Redirect": f"/oi?day={snap['date']}&at={snap['id']}"})
    return toast(response, f"Snapshot taken: {len(snap['rows'])} strikes")


@router.post("/settings")
def save_settings(request: Request, user: dict = Depends(guard), interval: int = Form(15), strikes: int = Form(10)):
    if user.get("role") != "superadmin":
        return fail("Only the super admin can change how OI is captured.")
    if interval not in oi.INTERVALS or strikes not in oi.STRIKE_CHOICES:
        return fail("Pick one of the options shown.")
    oi.save_settings(interval, strikes)
    return toast(HTMLResponse("", headers={"HX-Reswap": "none"}), f"OI is captured every {interval} min, {strikes} strikes either side")
