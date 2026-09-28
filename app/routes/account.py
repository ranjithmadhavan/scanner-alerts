"""Settings every signed-in user can reach, regardless of module permissions."""

from fastapi import APIRouter, Depends, Form, Request

from app.security import current_user, password_problem, set_password, start_session, verify_password
from app.web import fail, render, toast

router = APIRouter(prefix="/settings")


@router.get("")
def page(request: Request, user: dict = Depends(current_user)):
    return render(request, "settings.html")


@router.post("/password")
def change_password(request: Request, user: dict = Depends(current_user),
                    current: str = Form(...), new: str = Form(...), confirm: str = Form(...)):
    if user.get("role") == "superadmin":
        return fail("The super admin password is set by SUPERADMIN_PASSWORD on the server.")
    if not verify_password(current, user.get("password_hash", "")):
        return fail("Your current password isn't right.")
    if problem := password_problem(new):
        return fail(problem)
    if new != confirm:
        return fail("The new passwords don't match.")
    if new == current:
        return fail("Choose a password different from your current one.")
    set_password(user["username"], new)
    start_session(request, user["username"])  # keep this browser signed in; others are signed out
    return toast(render(request, "partials/password_form.html", {"done": True}),
                 "Password changed. Other devices have been signed out.")
