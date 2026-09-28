from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app.security import allowed_modules, current_user, start_session, verify_password
from app.store import store
from app.web import render

router = APIRouter()


@router.get("/")
def home(request: Request, user: dict = Depends(current_user)):
    modules = allowed_modules(user)
    if modules:
        return RedirectResponse(modules[0].path, status_code=303)
    if user.get("role") == "superadmin":
        return RedirectResponse("/admin/users", status_code=303)
    return render(request, "forbidden.html", {"no_access": True})


@router.get("/login")
def login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html")


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    user = store.get("users", username.strip().lower())
    if not user or not user.get("active", True) or not verify_password(password, user.get("password_hash", "")):
        return render(request, "login.html", {"error": "That username and password don't match.", "username": username}, 401)
    start_session(request, user["username"])
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
