"""Per-user broker account persistence. Secrets are encrypted at rest."""

from app.kite import KiteClient
from app.market import now_ist
from app.security import decrypt, encrypt
from app.store import store

COL = "brokers"


def load(username: str) -> dict:
    return store.get(COL, username) or {"mode": "connect", "status": "not_set"}


def save(username: str, fields: dict) -> None:
    secret_keys = ("api_secret", "access_token", "enctoken")
    doc = {k: (encrypt(v) if k in secret_keys else v) for k, v in fields.items()}
    store.update(COL, username, doc)


def set_status(username: str, status: str, message: str = "") -> None:
    fields = {"status": status, "message": message, "status_at": now_ist().isoformat()}
    if status == "connected":
        fields["notice_on"] = ""  # a later expiry the same day should notify again
    store.update(COL, username, fields)


def client_for(username: str, doc: dict | None = None) -> KiteClient:
    """Raises KiteAuthError if the account isn't connected."""
    doc = doc or load(username)
    return KiteClient(
        doc.get("mode", "connect"),
        api_key=doc.get("api_key", ""),
        access_token=decrypt(doc.get("access_token", "")),
        enctoken=decrypt(doc.get("enctoken", "")),
        user_id=doc.get("kite_user_id", ""),
    )


# ---- saved Kite logins (user ID + password, password encrypted) ----------------
# Kept on the broker doc as {"kite_logins": {user_id: {"password": <encrypted>, "saved_at": iso}}}, so the daily
# login needs only the 2FA code. The map is always written whole (store.update replaces a field outright).

def saved_logins(doc: dict) -> list[dict]:
    """The saved accounts, newest first, without their passwords."""
    logins = doc.get("kite_logins") or {}
    rows = [{"user_id": uid, "saved_at": v.get("saved_at", "")} for uid, v in logins.items()]
    return sorted(rows, key=lambda r: r["saved_at"], reverse=True)


def saved_password(username: str, user_id: str) -> str:
    entry = (load(username).get("kite_logins") or {}).get(user_id) or {}
    return decrypt(entry.get("password", ""))


def remember_login(username: str, user_id: str, password: str, replaces: str = "") -> None:
    logins = dict(load(username).get("kite_logins") or {})
    if replaces and replaces != user_id:
        logins.pop(replaces, None)
    logins[user_id] = {"password": encrypt(password), "saved_at": now_ist().isoformat()}
    store.update(COL, username, {"kite_logins": logins})


def forget_login(username: str, user_id: str) -> None:
    logins = dict(load(username).get("kite_logins") or {})
    logins.pop(user_id, None)
    store.update(COL, username, {"kite_logins": logins})
