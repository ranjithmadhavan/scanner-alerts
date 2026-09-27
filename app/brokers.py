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
