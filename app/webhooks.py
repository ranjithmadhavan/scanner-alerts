"""Webhooks: each person's endpoints, set up once on the Notifications page, and the signed JSON requests sent to them.

What goes where: an alert, a scanner or the fractal-bias settings carry `hook_ids`. Absent (None), they send to every
endpoint marked as a default; a list, to those endpoints only (an empty list: to none). Each can also carry its own
extra JSON, passed through untouched as "payload".

How a receiver knows a request is ours:
* every request is signed. `X-Signature: t=<unix time>,v1=<hex>`, the hex being the HMAC-SHA256 of "<t>.<raw body>"
  keyed with the endpoint's secret. The receiver recomputes it, compares in constant time, and refuses a `t` more than
  a few minutes old (so a captured request can't be replayed). While a secret is being rotated the old one signs too,
  as a second v1, for ROTATE_GRACE;
* `X-Webhook-Id` is the same on every try of one delivery, so a receiver can drop repeats;
* optionally, a header of the person's choosing (an API key, a bearer token), for receivers that only check that.
Secrets and header values are kept encrypted, shown in full once, and never logged or put in a body.

The server makes these requests, so URLs must be public https addresses (nothing on the server's own network),
redirects aren't followed (a key never goes on to another host), and this is re-checked on every send. Deliveries run
in the background with a couple of retries, so a slow receiver holds nothing up; each endpoint remembers how its last
delivery went.
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx

from app.market import now_ist
from app.security import decrypt, encrypt
from app.store import new_id, store

log = logging.getLogger("webhooks")

COL = "webhook_endpoints"
MAX_ENDPOINTS = 10
MAX_PAYLOAD_CHARS = 4000
TIMEOUT = 8
RETRY_WAITS = (2, 6)  # seconds before the second and third tries (network errors, 429 and 5xx only)
ROTATE_GRACE = timedelta(hours=24)
VERSION = 1
HEADER_NAME = re.compile(r"[A-Za-z0-9-]{1,64}")
RESERVED = {"host", "content-length", "content-type", "transfer-encoding", "connection", "user-agent", "cookie",
            "x-signature", "x-webhook-id", "x-webhook-timestamp"}

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="webhook")
_run = _pool.submit  # tests run deliveries inline


# ---- addresses --------------------------------------------------------------------------------

def _is_public(host: str) -> bool:
    try:
        found = socket.getaddrinfo(host, None)
    except OSError:
        return False
    return bool(found) and all(ipaddress.ip_address(info[4][0]).is_global for info in found)


def url_problem(url: str, https_only: bool = False) -> str | None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or len(url) > 500:
        return f"{url[:60]} isn't a web address. Webhook URLs start with https://"
    if https_only and parts.scheme != "https":
        return "Webhook URLs must start with https://, so the request and its key are encrypted on the way."
    if not _is_public(parts.hostname):
        return f"{parts.hostname} can't be reached from the internet, so it can't be used as a webhook."
    return None


def parse_payload(text: str) -> tuple[str, str | None]:
    """The form's custom JSON, kept as text (compact). Returns (json_text, None) or ("", error)."""
    text = text.strip()
    if not text:
        return "", None
    if len(text) > MAX_PAYLOAD_CHARS:
        return "", f"The webhook payload can be at most {MAX_PAYLOAD_CHARS} characters."
    try:
        return json.dumps(json.loads(text), separators=(",", ":")), None
    except ValueError as e:
        return "", f"The webhook payload isn't valid JSON: {e}"


def header_problem(name: str, value: str) -> str | None:
    if not HEADER_NAME.fullmatch(name):
        return "A header name is letters, digits and dashes, like X-API-Key."
    if name.lower() in RESERVED:
        return f"{name} is set by the app itself. Pick another header name, like X-API-Key."
    if not value or len(value) > 500 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return "The header value must be 1 to 500 characters on one line."
    return None


# ---- endpoints --------------------------------------------------------------------------------

def endpoints(username: str) -> list[dict]:
    return sorted(store.list(COL, user=username), key=lambda e: e["created_at"])


def pick(eps: list[dict], hook_ids: list[str] | None) -> list[dict]:
    """The endpoints a selection sends to: the defaults when nothing was picked, else the ones picked."""
    if hook_ids is None:
        return [e for e in eps if e.get("default")]
    return [e for e in eps if e["id"] in hook_ids]


def targets(username: str, hook_ids: list[str] | None) -> list[dict]:
    return pick(endpoints(username), hook_ids)


def selection(username: str, mode: str, ids: list[str], payload: str = "") -> tuple[dict, str | None]:
    """A form's webhook choice as fields to save: {"hook_ids", "webhook_payload"}. Returns (fields, None) or ({}, error).
    mode "default": the person's default endpoints; "none": no webhooks; "pick": the endpoints ticked."""
    payload, error = parse_payload(payload)
    if error:
        return {}, error
    if mode == "none":
        return {"hook_ids": [], "webhook_payload": payload}, None
    if mode == "pick":
        mine = {e["id"] for e in endpoints(username)}
        chosen = [i for i in dict.fromkeys(ids) if i in mine]
        if not chosen:
            return {}, "Tick at least one webhook, or choose your default webhooks or none."
        return {"hook_ids": chosen, "webhook_payload": payload}, None
    return {"hook_ids": None, "webhook_payload": payload}, None


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def create(username: str, name: str, url: str, default: bool, header_name: str = "", header_value: str = "") -> tuple[dict, str]:
    """Save a new endpoint. Returns (endpoint, its signing secret, to show once). Raises ValueError with something to show."""
    if len(endpoints(username)) >= MAX_ENDPOINTS:
        raise ValueError(f"You can keep up to {MAX_ENDPOINTS} webhooks. Remove one first.")
    fields = _fields(name, url, default, header_name, header_value, keep_header=False)
    secret = new_secret()
    ep = {"id": new_id(), "user": username, **fields, "secret": encrypt(secret), "secret_tail": secret[-4:],
          "created_at": now_ist().isoformat(), "last": None}
    store.put(COL, ep["id"], ep)
    return ep, secret


def edit(ep: dict, name: str, url: str, default: bool, header_name: str = "", header_value: str = "",
         drop_header: bool = False) -> dict:
    """Save changes to an endpoint. A blank header value keeps the saved one. Raises ValueError with something to show."""
    fields = _fields(name, url, default, header_name, header_value, keep_header=not drop_header and bool(ep.get("header_value")))
    if drop_header:
        fields.update(header_name="", header_value="", header_tail="")
    store.update(COL, ep["id"], fields)
    return {**ep, **fields}


def _fields(name: str, url: str, default: bool, header_name: str, header_value: str, keep_header: bool) -> dict:
    url, header_name, header_value = url.strip(), header_name.strip(), header_value.strip()
    if error := url_problem(url, https_only=True):
        raise ValueError(error)
    fields = {"name": name.strip()[:40] or urlsplit(url).hostname, "url": url, "default": bool(default)}
    if header_name or header_value:
        if keep_header and header_name and not header_value:  # a new name for the saved value
            fields["header_name"] = header_name
            if not HEADER_NAME.fullmatch(header_name) or header_name.lower() in RESERVED:
                raise ValueError(header_problem(header_name, "x"))
        else:
            if error := header_problem(header_name, header_value):
                raise ValueError(error)
            fields.update(header_name=header_name, header_value=encrypt(header_value), header_tail=header_value[-4:])
    return fields


def rotate(ep: dict) -> str:
    """A new signing secret. The old one keeps signing alongside it for ROTATE_GRACE. Returns the new secret."""
    secret = new_secret()
    store.update(COL, ep["id"], {"secret": encrypt(secret), "secret_tail": secret[-4:], "prev_secret": ep["secret"],
                                 "prev_until": (now_ist() + ROTATE_GRACE).isoformat()})
    return secret


def remove(ep: dict) -> None:
    """Delete an endpoint, and take it off whatever had picked it."""
    store.delete(COL, ep["id"])
    for col in ("alerts", "ma_scanners"):
        for doc in store.list(col, user=ep["user"]):
            if ep["id"] in (doc.get("hook_ids") or []):
                store.update(col, doc["id"], {"hook_ids": [i for i in doc["hook_ids"] if i != ep["id"]]})
    bias = store.get("bias_alerts", ep["user"])
    if bias and ep["id"] in (bias.get("hook_ids") or []):
        store.update("bias_alerts", ep["user"], {"hook_ids": [i for i in bias["hook_ids"] if i != ep["id"]]})


# ---- sending ----------------------------------------------------------------------------------

def sign(secret: str, ts: str, raw: bytes) -> str:
    return hmac.new(secret.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()


def headers(ep: dict, raw: bytes, delivery_id: str) -> dict:
    ts = str(int(time.time()))
    keys = [decrypt(ep["secret"])]
    if ep.get("prev_secret") and now_ist() < datetime.fromisoformat(ep["prev_until"]):
        keys.append(decrypt(ep["prev_secret"]))
    out = {"Content-Type": "application/json", "User-Agent": "stock-scanner-alerts", "X-Webhook-Id": delivery_id,
           "X-Webhook-Timestamp": ts, "X-Signature": ",".join([f"t={ts}"] + [f"v1={sign(k, ts, raw)}" for k in keys])}
    if ep.get("header_name") and ep.get("header_value"):
        out[ep["header_name"]] = decrypt(ep["header_value"])
    return out


def post(ep: dict, raw: bytes, delivery_id: str, retries: bool = True) -> str:
    """POST one signed body to one endpoint. Returns "sent" or what went wrong. Never raises."""
    waits = RETRY_WAITS if retries else ()
    result = ""
    for attempt in range(len(waits) + 1):
        if attempt:
            time.sleep(waits[attempt - 1])
        try:
            if error := url_problem(ep["url"]):
                result = "not a public address" if "internet" in error else "not a web address"
                break
            r = httpx.post(ep["url"], content=raw, headers=headers(ep, raw, delivery_id), timeout=TIMEOUT, follow_redirects=False)
            if r.status_code < 300:
                result = "sent"
                break
            result = f"HTTP {r.status_code}"
            if r.status_code < 500 and r.status_code != 429:
                break  # the receiver turned it down: trying again won't change that
        except httpx.HTTPError as e:
            result = e.__class__.__name__
    try:
        store.update(COL, ep["id"], {"last": {"at": now_ist().isoformat(), "ok": result == "sent", "detail": result}})
    except Exception:
        log.exception("couldn't record webhook delivery for %s", ep["id"])
    return result


def summary(eps: list[dict], results: list[str]) -> str:
    failed = [f"{ep['name']}: {r}" for ep, r in zip(eps, results) if r != "sent"]
    if not failed:
        return "sent"
    return (f"Reached {len(eps) - len(failed)} of {len(eps)}. " if len(eps) > 1 else "") + "; ".join(failed)


def encode(body: dict) -> bytes:
    return json.dumps({"version": VERSION, **body}, separators=(",", ":")).encode()


def send_now(eps: list[dict], body: dict) -> str:
    """Send to each endpoint right away, once (for test buttons). Returns "sent", or which ones failed."""
    raw = encode(body)
    return summary(eps, [post(ep, raw, str(uuid.uuid4()), retries=False) for ep in eps])


def dispatch(eps: list[dict], body: dict, event_id: str | None = None) -> None:
    """Send to each endpoint in the background, with retries. When done, the event's delivery shows how it went."""
    if not eps:
        return
    raw = encode(body)

    def deliver():
        result = summary(eps, [post(ep, raw, str(uuid.uuid4())) for ep in eps])
        if event_id and (event := store.get("events", event_id)):
            store.update("events", event_id, {"delivery": {**event.get("delivery", {}), "webhook": result}})
        if result != "sent":
            log.warning("webhook delivery: %s", result)

    _run(deliver)


# ---- endpoints from before ----------------------------------------------------------------------

def migrate() -> int:
    """Move webhook URLs saved on alerts, scanners and fractal-bias settings (before endpoints existed) to endpoints,
    picked by what had them. Nothing that had none changes: it now sends to the person's defaults (there are none yet).
    Returns how many records moved."""
    made: dict[tuple[str, str], str] = {(e["user"], e["url"]): e["id"] for e in store.list(COL)}

    def ids_for(user: str, urls: list[str]) -> list[str]:
        out = []
        for url in urls:
            if (user, url) not in made:
                secret = new_secret()
                ep = {"id": new_id(), "user": user, "name": (urlsplit(url).hostname or url)[:40], "url": url, "default": False,
                      "secret": encrypt(secret), "secret_tail": secret[-4:], "created_at": now_ist().isoformat(), "last": None}
                store.put(COL, ep["id"], ep)
                made[(user, url)] = ep["id"]
            out.append(made[(user, url)])
        return out

    docs = [(col, doc["id"], doc["user"], doc) for col in ("alerts", "ma_scanners") for doc in store.list(col)]
    docs += [("bias_alerts", u["username"], u["username"], doc) for u in store.list("users")
             if (doc := store.get("bias_alerts", u["username"]))]  # kept under the username
    moved = 0
    for col, doc_id, user, doc in docs:
        if doc.get("webhooks") is None:
            continue
        store.update(col, doc_id, {"hook_ids": ids_for(user, doc["webhooks"]) if doc["webhooks"] else None, "webhooks": None})
        moved += 1
    return moved
