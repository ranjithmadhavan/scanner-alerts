"""Incoming webhooks: addresses a charting tool (TradingView, Chartink, Amibroker, n8n…) POSTs to, so what it sends
reaches the person on Telegram (or any of their other channels) and is kept for later.

Each one is a secret URL, /hook/<token>: charting tools can't sign a request or set a header, so knowing the URL is the
key. The token is kept encrypted (so its owner can copy the URL again) and the record is filed under a hash of it, so a
request is matched without decrypting anything. A leaked URL is fixed by making a new one.

A request is answered at once and delivered in the background (TradingView gives up after a few seconds). The body can
be JSON (sent on as "key: value" lines) or plain text (sent as is). The last RECENT ones are kept on the hook, with how
their delivery went, and each also shows under "Recently sent" on the Notifications page.
"""

import hashlib
import json
import logging
import secrets
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

from app import config, notify
from app.market import now_ist
from app.security import decrypt, encrypt
from app.store import new_id, store

log = logging.getLogger("inbound")

COL = "inbound_hooks"
MAX_HOOKS = 10
MAX_BODY = 10_000  # bytes accepted per request
KEEP_CHARS = 4000  # of a body, kept and sent (Telegram's limit is 4096)
RECENT = 20
RATE = (30, 60)  # at most this many requests per hook in this many seconds
DEFAULT_CHANNELS = ["telegram"]

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="inbound")
_run = _pool.submit  # tests run deliveries inline
_lock = threading.Lock()  # one process: keeps two quick requests from losing each other's "recent" entry
_hits: dict[str, deque] = defaultdict(deque)


def _key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:32]


def url(hook: dict) -> str:
    return f"{config.BASE_URL}/hook/{decrypt(hook['token'])}"


def hooks(username: str) -> list[dict]:
    return sorted(store.list(COL, user=username), key=lambda h: h["created_at"])


def own(hook_id: str, username: str) -> dict | None:
    hook = store.get(COL, hook_id)
    return hook if hook and hook["user"] == username else None


def _channels(picked: list[str]) -> list[str]:
    chosen = [c for c in dict.fromkeys(picked) if c in notify.CHANNELS]
    if not chosen:
        raise ValueError("Pick at least one place to send what arrives, like Telegram.")
    return chosen


def create(username: str, name: str, channels: list[str]) -> dict:
    """A new incoming webhook. Raises ValueError with something to show."""
    if len(hooks(username)) >= MAX_HOOKS:
        raise ValueError(f"You can keep up to {MAX_HOOKS} incoming webhooks. Remove one first.")
    token = secrets.token_urlsafe(24)
    hook = {"id": _key(token), "user": username, "name": name.strip()[:40] or "Chart alerts", "channels": _channels(channels),
            "token": encrypt(token), "created_at": now_ist().isoformat(), "count": 0, "recent": []}
    store.put(COL, hook["id"], hook)
    return hook


def edit(hook: dict, name: str, channels: list[str]) -> dict:
    fields = {"name": name.strip()[:40] or hook["name"], "channels": _channels(channels)}
    store.update(COL, hook["id"], fields)
    return {**hook, **fields}


def new_url(hook: dict) -> dict:
    """Replace the URL (the old one stops working at once). The record moves to the new token's key."""
    token = secrets.token_urlsafe(24)
    moved = {**hook, "id": _key(token), "token": encrypt(token)}
    store.put(COL, moved["id"], moved)
    store.delete(COL, hook["id"])
    return moved


def remove(hook: dict) -> None:
    store.delete(COL, hook["id"])


# ---- receiving ----------------------------------------------------------------------------------

def parse(raw: bytes):
    """The body as JSON if it is JSON, else as text."""
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        return json.loads(text)
    except ValueError:
        return text


def message(data) -> str:
    """What's sent: a JSON object as "key: value" lines, anything else as it came."""
    if isinstance(data, dict):
        lines = [f"{k}: {v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}" for k, v in data.items()]
        text = "\n".join(lines)
    elif isinstance(data, str):
        text = data
    else:
        text = json.dumps(data, ensure_ascii=False, indent=1)
    text = text or "(empty)"
    return text if len(text) <= KEEP_CHARS else text[:KEEP_CHARS - 1] + "…"


def _limited(hook_id: str) -> bool:
    most, seconds = RATE
    now, seen = time.monotonic(), _hits[hook_id]
    while seen and seen[0] < now - seconds:
        seen.popleft()
    if len(seen) >= most:
        return True
    seen.append(now)
    return False


def find(token: str) -> dict | None:
    return store.get(COL, _key(token)) if 16 <= len(token) <= 64 else None


def receive(token: str, raw: bytes) -> tuple[int, str]:
    """Take one request to /hook/<token>. Returns (HTTP status, what to answer). Delivery happens in the background."""
    hook = find(token)
    owner = hook and store.get("users", hook["user"])
    if not hook or not owner or not owner.get("active", True):
        return 404, "No webhook here"
    if len(raw) > MAX_BODY:
        return 413, f"Send at most {MAX_BODY} bytes"
    if _limited(hook["id"]):
        return 429, f"Too many requests: at most {RATE[0]} a minute"
    data, at = parse(raw), now_ist()
    _run(deliver, hook, data, at)
    return 200, "ok"


def deliver(hook: dict, data, at, test: bool = False) -> dict[str, str]:
    """Send what arrived to the hook's channels, then keep it on the hook and in the person's events."""
    text = message(data)
    results = notify.send(hook["user"], hook["channels"], f"📩 {hook['name']}{' (test)' if test else ''}", text)
    kept = data if len(json.dumps(data, ensure_ascii=False)) <= KEEP_CHARS else text
    entry = {"at": at.isoformat(), "data": kept, "delivery": results, "test": test}
    try:
        with _lock:
            if current := store.get(COL, hook["id"]):  # gone if removed or given a new URL meanwhile
                store.update(COL, hook["id"], {"recent": [entry] + (current.get("recent") or [])[:RECENT - 1],
                                               "count": current.get("count", 0) + 1, "last_at": entry["at"]})
        first = text.splitlines()[0][:80]
        store.put("events", new_id(), {"user": hook["user"], "inbound_id": hook["id"], "summary": f"{hook['name']}: {first}",
                                       "at": entry["at"], "delivery": results})
    except Exception:
        log.exception("couldn't record what arrived on %s", hook["id"])
    if any(r != "sent" for r in results.values()):
        log.warning("incoming webhook %s: %s", hook["id"], results)
    return results
