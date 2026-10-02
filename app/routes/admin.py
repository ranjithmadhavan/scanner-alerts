import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app import scanner
from datetime import date

from app.market import SCAN_INTERVALS, fetch_nse_holidays, load_settings, now_ist, parse_hhmm, save_holidays, save_settings
from app.modules import MODULES
from app.security import hash_password, password_problem, require_superadmin, set_password
from app.store import store
from app.web import fail, render, toast

router = APIRouter(prefix="/admin")


def _users_ctx(**extra) -> dict:
    users = sorted(store.list("users"), key=lambda u: (u.get("role") != "superadmin", u["username"]))
    return {"users": users, "modules": list(MODULES.values()), **extra}


@router.get("/users")
def users_page(request: Request, admin: dict = Depends(require_superadmin)):
    return render(request, "admin_users.html", _users_ctx())


@router.post("/users")
def create_user(request: Request, admin: dict = Depends(require_superadmin),
                username: str = Form(...), name: str = Form(""), password: str = Form(...),
                modules: list[str] = Form([])):
    username = username.strip().lower()
    error = None
    if not re.fullmatch(r"[a-z0-9._-]{3,32}", username):
        error = "Usernames use 3–32 letters, numbers, dots, dashes or underscores."
    elif store.get("users", username):
        error = f"{username} is already taken."
    elif len(password) < 8:
        error = "Passwords need at least 8 characters."
    if error:
        return fail(error)
    store.put("users", username, {
        "username": username, "name": name.strip() or username, "role": "user", "active": True,
        "modules": [m for m in modules if m in MODULES], "password_hash": hash_password(password),
    })
    return toast(render(request, "partials/user_list.html", _users_ctx(fresh=username)), f"Added {username}")


def _editable(username: str) -> dict | None:
    u = store.get("users", username)
    return u if u and u.get("role") != "superadmin" else None


@router.post("/users/{username}")
def update_user(request: Request, username: str, admin: dict = Depends(require_superadmin),
                name: str = Form(""), modules: list[str] = Form([]), active: str = Form("")):
    if not _editable(username):
        return HTMLResponse(status_code=404)
    store.update("users", username, {"name": name.strip() or username, "active": active == "on",
                                     "modules": [m for m in modules if m in MODULES]})
    return toast(render(request, "partials/user_list.html", _users_ctx()), f"Saved {username}")


@router.post("/users/{username}/password")
def reset_password(request: Request, username: str, admin: dict = Depends(require_superadmin),
                   password: str = Form(...)):
    if not _editable(username):
        return HTMLResponse(status_code=404)
    if problem := password_problem(password):
        return fail(problem)
    set_password(username, password)
    return toast(render(request, "partials/user_list.html", _users_ctx()), f"New password set for {username}")


@router.delete("/users/{username}")
def delete_user(request: Request, username: str, admin: dict = Depends(require_superadmin)):
    if not _editable(username):
        return HTMLResponse(status_code=404)
    store.delete("users", username)
    for a in store.list("alerts", user=username):
        store.delete("alerts", a["id"])
    store.delete("brokers", username)
    store.delete("contacts", username)
    return toast(render(request, "partials/user_list.html", _users_ctx()), f"Removed {username}")


# ---- market hours -----------------------------------------------------------

@router.get("/market")
def market_page(request: Request, admin: dict = Depends(require_superadmin)):
    return render(request, "admin_market.html", {"s": load_settings(), "intervals": SCAN_INTERVALS, **_holidays_ctx()})


def _holidays_ctx() -> dict:
    today = now_ist().date()
    days = [{"date": d, "day": date.fromisoformat(d), "name": n} for d, n in sorted(load_settings().holidays.items())]
    return {"today": today, "upcoming": [h for h in days if h["day"] >= today]}


def _holidays(request: Request, message: str, kind: str = "success"):
    return toast(render(request, "partials/holidays.html", _holidays_ctx()), message, kind)


@router.post("/market/holidays")
def add_holiday(request: Request, admin: dict = Depends(require_superadmin), day: str = Form(...), name: str = Form("")):
    try:
        day = date.fromisoformat(day.strip()).isoformat()
    except ValueError:
        return fail("Pick the holiday's date.")
    save_holidays({**load_settings().holidays, day: name.strip()[:60] or "Market holiday"})
    return _holidays(request, "Holiday added. Nothing is scanned that day.")


@router.post("/market/holidays/remove")
def remove_holiday(request: Request, admin: dict = Depends(require_superadmin), day: str = Form(...)):
    save_holidays({d: n for d, n in load_settings().holidays.items() if d != day})
    return _holidays(request, "Removed. That day is scanned like any other.")


@router.post("/market/holidays/fetch")
def fetch_holidays(request: Request, admin: dict = Depends(require_superadmin)):
    try:
        found = fetch_nse_holidays()
    except RuntimeError:
        return _holidays(request, "NSE didn't answer (it often blocks cloud servers). Add the dates by hand instead.", "error")
    current = load_settings().holidays
    new = {d: n for d, n in found.items() if d not in current}
    save_holidays({**current, **new})
    return _holidays(request, f"Added {len(new)} holiday{'s' if len(new) != 1 else ''} from NSE." if new
                     else "Already up to date with NSE's list.")


@router.post("/market")
def save_market(request: Request, admin: dict = Depends(require_superadmin),
                open_time: str = Form(...), close_time: str = Form(...), scan_interval: int = Form(60)):
    try:
        o, c = parse_hhmm(open_time), parse_hhmm(close_time)
    except ValueError:
        o = c = None
    if not o or not c or o >= c:
        return fail("Closing time has to be after opening time.")
    if scan_interval not in SCAN_INTERVALS:
        scan_interval = 60
    save_settings(open_time, close_time, scan_interval)
    scanner.reschedule(scan_interval)
    return toast(HTMLResponse("", headers={"HX-Reswap": "none"}), "Market hours saved")
