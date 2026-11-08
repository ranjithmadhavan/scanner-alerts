from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app import brokers, config, scanner
from app.kite import KiteAuthError, KiteError, clean_enctoken, connect_exchange_token, connect_login_url, web_login
from app.market import IST, now_ist
from app.security import decrypt, require
from app.web import render, toast

router = APIRouter(prefix="/broker")
guard = require("broker")

CALLBACK_PATH = "/broker/kite/callback"


def _session_is_stale(doc: dict) -> bool:
    """Kite sessions end around 6 AM IST each day."""
    at = doc.get("status_at")
    if doc.get("status") != "connected" or not at:
        return False
    now = now_ist()
    cutoff = now.replace(hour=6, minute=0, second=0, microsecond=0)
    return now >= cutoff and datetime.fromisoformat(at).astimezone(IST) < cutoff


def _ctx(user: dict, **extra) -> dict:
    doc = brokers.load(user["username"])
    return {
        "broker": doc,
        "has_secret": bool(decrypt(doc.get("api_secret", ""))),
        "stale": _session_is_stale(doc),
        "logins": brokers.saved_logins(doc),
        "callback_url": config.BASE_URL + CALLBACK_PATH,
        **extra,
    }


@router.get("")
def page(request: Request, user: dict = Depends(guard)):
    return render(request, "broker.html", _ctx(user, error=request.query_params.get("error")))


@router.post("/connect")
def save_connect(request: Request, user: dict = Depends(guard),
                 api_key: str = Form(...), api_secret: str = Form("")):
    fields = {"mode": "connect", "api_key": api_key.strip()}
    if api_secret.strip():
        fields["api_secret"] = api_secret.strip()
    brokers.save(user["username"], fields)
    return RedirectResponse("/broker/kite/login", status_code=303)


@router.get("/kite/login")
def kite_login(user: dict = Depends(guard)):
    doc = brokers.load(user["username"])
    if not doc.get("api_key"):
        return RedirectResponse("/broker?error=Add your API key first.", status_code=303)
    return RedirectResponse(connect_login_url(doc["api_key"]), status_code=303)


@router.get("/kite/callback")
def kite_callback(user: dict = Depends(guard), request_token: str = "", status: str = ""):
    username = user["username"]
    doc = brokers.load(username)
    if status != "success" or not request_token:
        return RedirectResponse("/broker?error=Kite login was cancelled.", status_code=303)
    try:
        data = connect_exchange_token(doc.get("api_key", ""), decrypt(doc.get("api_secret", "")), request_token)
    except KiteError as e:
        brokers.set_status(username, "error", str(e))
        return RedirectResponse(f"/broker?error={quote(str(e))}", status_code=303)
    brokers.save(username, {"mode": "connect", "access_token": data["access_token"],
                            "kite_user_id": data.get("user_id", ""), "kite_name": data.get("user_name", "")})
    brokers.set_status(username, "connected")
    scanner.session_restored(username)
    return RedirectResponse("/broker", status_code=303)


@router.post("/enctoken")
def save_enctoken(request: Request, user: dict = Depends(guard),
                  kite_user_id: str = Form(...), enctoken: str = Form(...)):
    username = user["username"]
    token = clean_enctoken(enctoken)
    if len(token) < 20:
        return toast(render(request, "partials/broker_status.html", _ctx(user)),
                     "That doesn't look like a full enctoken. Copy the whole cookie value.", "error")
    brokers.save(username, {"mode": "enctoken", "enctoken": token, "kite_user_id": kite_user_id.strip().upper()})
    return _verify(request, user, "Connected with enctoken")


def _status(request: Request, user: dict, message: str, kind: str = "error"):
    return toast(render(request, "partials/broker_status.html", _ctx(user)), message, kind)


@router.post("/login")
def kite_web_login(request: Request, user: dict = Depends(guard), saved: str = Form(""),
                   kite_user_id: str = Form(""), password: str = Form(""), code: str = Form(...),
                   remember: str = Form("")):
    """Log in to Kite with password + 2FA code. Pick a saved account (`saved`) to need only the code; a new
    account's password is kept, encrypted, when `remember` is ticked."""
    username = user["username"]
    code = "".join(code.split())
    if saved:
        user_id, password = saved.strip().upper(), brokers.saved_password(username, saved.strip().upper())
        if not password:
            return _status(request, user, "That saved account is gone. Choose another or add it again.")
    else:
        user_id = kite_user_id.strip().upper()
        if not user_id or not password:
            return _status(request, user, "Enter your Kite user ID and password.")
    if not (code.isdigit() and len(code) == 6):
        return _status(request, user, "The code should be the 6 digits from your authenticator app.")
    try:
        token = web_login(user_id, password, code)
    except KiteAuthError as e:
        hint = " If you changed your Kite password, update the saved one under Saved accounts." if saved else ""
        return _status(request, user, f"Kite said: {e}{hint}")
    except KiteError as e:
        return _status(request, user, str(e))
    brokers.save(username, {"mode": "enctoken", "enctoken": token, "kite_user_id": user_id})
    if not saved and remember:
        brokers.remember_login(username, user_id, password)
    return _verify(request, user, "Logged in to Kite", login_panel=True)


def _login_panel(request: Request, user: dict, message: str = "", kind: str = "success"):
    response = render(request, "partials/kite_login.html", _ctx(user))
    return toast(response, message, kind) if message else response


@router.post("/logins/edit")
def edit_saved_login(request: Request, user: dict = Depends(guard), original: str = Form(...),
                     kite_user_id: str = Form(...), password: str = Form("")):
    """Change a saved account's user ID and/or password. A blank password keeps the saved one."""
    username, original, user_id = user["username"], original.strip().upper(), kite_user_id.strip().upper()
    password = password or brokers.saved_password(username, original)
    if not user_id or not password:
        return _login_panel(request, user, "Enter a Kite user ID and password.", "error")
    brokers.remember_login(username, user_id, password, replaces=original)
    return _login_panel(request, user, f"Saved {user_id}")


@router.post("/logins/delete")
def delete_saved_login(request: Request, user: dict = Depends(guard), kite_user_id: str = Form(...)):
    brokers.forget_login(user["username"], kite_user_id.strip().upper())
    return _login_panel(request, user, f"Removed {kite_user_id.strip().upper()}")


@router.post("/test")
def test(request: Request, user: dict = Depends(guard)):
    return _verify(request, user, "Kite session is working")


def _verify(request: Request, user: dict, ok_message: str, login_panel: bool = False):
    username = user["username"]
    try:
        profile = brokers.client_for(username).profile()
        brokers.save(username, {"kite_name": profile.get("user_name", "")})
        brokers.set_status(username, "connected")
        scanner.session_restored(username)
        msg, kind = ok_message, "success"
    except KiteAuthError as e:
        brokers.set_status(username, "expired", str(e))
        msg, kind = f"Kite said: {e}", "error"
    except KiteError as e:
        msg, kind = f"Couldn't reach Kite: {e}", "error"
    # After a web login the saved-accounts list may have changed, so the form comes back too (out of band).
    name = "partials/broker_login_result.html" if login_panel else "partials/broker_status.html"
    return toast(render(request, name, _ctx(user)), msg, kind)


@router.post("/mode")
def switch_mode(request: Request, user: dict = Depends(guard), mode: str = Form(...)):
    if mode in ("connect", "enctoken"):
        brokers.save(user["username"], {"mode": mode})
        brokers.set_status(user["username"], "not_set")
    return RedirectResponse("/broker", status_code=303)
