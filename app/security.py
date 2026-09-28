"""Password hashing, secret encryption and request guards."""

import base64
import hashlib
import hmac
import os
import time

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Request

from app import config
from app.modules import MODULES
from app.store import store

_ITERATIONS = 240_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt, digest = stored.split("$")
    except (ValueError, AttributeError):
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters))
    return hmac.compare_digest(check.hex(), digest)


def _fernet() -> Fernet:
    key = config.ENCRYPTION_KEY
    if not key:
        # Dev fallback: derive from the session secret so local runs work without extra setup.
        key = base64.urlsafe_b64encode(hashlib.sha256(config.SESSION_SECRET.encode()).digest()).decode()
    return Fernet(key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode() if value else ""


def decrypt(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        return ""


# ---- guards -----------------------------------------------------------------

class LoginRequired(Exception):
    pass


class Forbidden(Exception):
    pass


def can_access(user: dict, module: str) -> bool:
    return user.get("role") == "superadmin" or module in user.get("modules", [])


def allowed_modules(user: dict) -> list:
    return [m for m in MODULES.values() if can_access(user, m.key)]


def start_session(request: Request, username: str) -> None:
    request.session.clear()
    request.session["user"] = username
    request.session["since"] = time.time()


def set_password(username: str, password: str) -> None:
    """Change a password. Sessions started before this moment are signed out."""
    store.update("users", username, {"password_hash": hash_password(password), "password_changed_at": time.time()})


def password_problem(password: str) -> str | None:
    if len(password) < 8:
        return "Passwords need at least 8 characters."
    if len(password) > 128:
        return "Passwords can be at most 128 characters."
    return None


def current_user(request: Request) -> dict:
    username = request.session.get("user")
    user = store.get("users", username) if username else None
    changed = (user or {}).get("password_changed_at")
    stale = changed and request.session.get("since", 0) < changed  # signed in before the last password change
    if not user or not user.get("active", True) or stale:
        request.session.clear()
        raise LoginRequired()
    request.state.user = user
    return user


def require(module: str):
    def dep(request: Request) -> dict:
        user = current_user(request)
        if not can_access(user, module):
            raise Forbidden()
        return user
    return dep


def require_superadmin(request: Request) -> dict:
    user = current_user(request)
    if user.get("role") != "superadmin":
        raise Forbidden()
    return user


def seed_superadmin() -> None:
    """Create/refresh the super admin from env. Env is the source of truth for its password."""
    name, password = config.SUPERADMIN_USERNAME, config.SUPERADMIN_PASSWORD
    if not name or not password:
        print("[auth] SUPERADMIN_USERNAME / SUPERADMIN_PASSWORD not set — no super admin seeded.")
        return
    existing = store.get("users", name)
    if existing and existing.get("role") == "superadmin" and verify_password(password, existing.get("password_hash", "")):
        return
    store.put("users", name, {
        **(existing or {}),
        "username": name,
        "name": (existing or {}).get("name") or "Super admin",
        "role": "superadmin",
        "modules": [],
        "active": True,
        "password_hash": hash_password(password),
        "password_changed_at": time.time(),
    })
    print(f"[auth] Super admin '{name}' seeded.")
