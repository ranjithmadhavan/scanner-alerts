import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request

from app import notify
from app.market import now_ist
from app.security import decrypt, encrypt, require
from app.store import store
from app.web import render, toast

router = APIRouter(prefix="/notifications")
guard = require("notifications")

# Email addresses are confirmed with a code sent to them before any alert goes there.
CODE_MINUTES = 10
CODE_TRIES = 5
CODE_RESEND_SECONDS = 60


def _ctx(user: dict) -> dict:
    contacts = notify.load_contacts(user["username"])
    return {
        "contacts": contacts,
        "hints": {"telegram": _hint(contacts.get("telegram_bot_token")),
                  "whatsapp": ", ".join("••••" + h for _, key in notify.whatsapp_recipients(contacts) if (h := _tail(key)))},
        "telegram_chats": _chats(contacts),
        "emails": contacts.get("emails") or [],
        "email_pending": contacts.get("email_pending"),
        "email_unverified": _unverified(contacts),
        "count": {"telegram": len(_chats(contacts)), "email": len(contacts.get("emails") or []),
                  "whatsapp": len(notify.split_list(contacts.get("whatsapp_phone")))},
        "channels": [
            {"key": k, "label": v, "server_ready": notify.sender_ready(k), "ready": notify.recipient_ready(k, contacts),
             "configured": notify.configured(k, contacts), "last_test": contacts.get(f"test_{k}")}
            for k, v in notify.CHANNELS.items()
        ],
        "events": sorted(store.list("events", user=user["username"]), key=lambda e: e["at"], reverse=True)[:15],
    }


@router.get("")
def page(request: Request, user: dict = Depends(guard)):
    return render(request, "notifications.html", _ctx(user))


def _tail(plain: str) -> str:
    return plain[-4:] if len(plain) >= 10 else plain[-2:] if len(plain) >= 5 else ""


def _hint(secret_enc: str) -> str:
    """Last 4 characters of a stored secret, so users can tell which one is saved."""
    return _tail(decrypt(secret_enc or ""))


def _chats(contacts: dict) -> list[dict]:
    """The linked Telegram chats as {id, name}; names are known for chats found by the bot."""
    ids = notify.split_list(contacts.get("telegram_chat_id"))
    names = contacts.get("telegram_chat_names") or {}
    legacy = contacts.get("telegram_chat_name") if len(ids) == 1 else ""  # saved before several chats were allowed
    return [{"id": i, "name": names.get(i) or legacy or ""} for i in ids]


def _unverified(contacts: dict) -> list[str]:
    """Addresses saved before verification existed. They get nothing until confirmed."""
    skip = set(contacts.get("emails") or []) | {(contacts.get("email_pending") or {}).get("address")}
    return [a for a in (x.lower() for x in notify.split_list(contacts.get("email"))) if a not in skip]


def _code_hash(address: str, code: str) -> str:
    return hashlib.sha256(f"{address}:{code}".encode()).hexdigest()


def _send_code(request: Request, user: dict, address: str):
    """Email a 6-digit code to `address` and remember it as the address waiting to be confirmed."""
    address = address.strip().lower()
    contacts = notify.load_contacts(user["username"])
    emails, pending, now = contacts.get("emails") or [], contacts.get("email_pending") or {}, now_ist()
    if re.search(r"[,;\s]", address):
        return _panel(request, user, "Add one address at a time. Each one is confirmed separately.", "error")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
        return _panel(request, user, "That email address looks incomplete.", "error")
    if address in emails:
        return _panel(request, user, f"{address} is already confirmed.", "error")
    if error := _too_many(emails + [address]):
        return _panel(request, user, error, "error")
    if not notify.sender_ready("email"):
        return _panel(request, user, "Email isn't set up on the server yet, so we can't send a code.", "error")
    if pending.get("address") == address and now < datetime.fromisoformat(pending["sent_at"]) + timedelta(seconds=CODE_RESEND_SECONDS):
        return _panel(request, user, "We just sent a code to that address. Give it a minute before asking for another.", "error")
    code = f"{secrets.randbelow(10 ** 6):06d}"
    try:
        notify.send_email(address, f"{code} is your code to confirm this address",
                          f"Enter this code on the Notifications page to get price alerts at this address: {code}\n"
                          f"It works for {CODE_MINUTES} minutes.\n"
                          "If you didn't ask for this, ignore this email. Nothing will be sent to you.")
    except Exception as e:
        return _panel(request, user, f"Couldn't send the code: {str(e)[:200] or e.__class__.__name__}", "error")
    store.update("contacts", user["username"], {"email_pending": {
        "address": address, "code": _code_hash(address, code), "tries": 0, "sent_at": now.isoformat(),
        "expires": (now + timedelta(minutes=CODE_MINUTES)).isoformat()}})
    return _panel(request, user, f"We emailed a 6-digit code to {address}. Enter it to confirm. Check spam if you don't see it.")


def _confirm_code(request: Request, user: dict, code: str):
    contacts = notify.load_contacts(user["username"])
    pending = contacts.get("email_pending")
    if not pending:
        return _panel(request, user, "Add an email address first.", "error")
    if now_ist() > datetime.fromisoformat(pending["expires"]):
        return _panel(request, user, "That code has expired. Send a new one.", "error")
    if pending["tries"] >= CODE_TRIES:
        return _panel(request, user, "Too many wrong codes. Send a new one.", "error")
    address = pending["address"]
    if not hmac.compare_digest(_code_hash(address, re.sub(r"\D", "", code)), pending["code"]):
        left = CODE_TRIES - pending["tries"] - 1
        store.update("contacts", user["username"], {"email_pending": {**pending, "tries": pending["tries"] + 1}})
        return _panel(request, user, f"That code isn't right. {left} {'try' if left == 1 else 'tries'} left." if left
                      else "That code isn't right. Send a new one.", "error")
    old = [a for a in notify.split_list(contacts.get("email")) if a.lower() != address]
    store.update("contacts", user["username"], {
        "emails": (contacts.get("emails") or []) + [address], "email_pending": None,
        "email": ", ".join(old) or None, "test_email": None})
    return _panel(request, user, f"{address} is confirmed. Alerts sent by email will go there.")


def _too_many(items: list) -> str | None:
    return f"You can add up to {notify.MAX_RECIPIENTS} here." if len(items) > notify.MAX_RECIPIENTS else None


def _panel(request: Request, user: dict, message: str, kind: str = "success"):
    return toast(render(request, "partials/channels.html", _ctx(user)), message, kind)


def _bot_fields(token: str) -> dict:
    """Validate the token with Telegram and return what to store. Raises RuntimeError if invalid."""
    info = notify.telegram_bot_info(token)
    return {"telegram_bot_token": encrypt(token), "telegram_bot_username": info["username"],
            "telegram_bot_name": info["name"], "test_telegram": None}


@router.post("/telegram/detect")
def telegram_detect(request: Request, user: dict = Depends(guard), telegram_bot_token: str = Form("")):
    """Save the bot token (if given) and link the chat that last messaged the bot."""
    username = user["username"]
    contacts = notify.load_contacts(username)
    token = telegram_bot_token.strip() or decrypt(contacts.get("telegram_bot_token", ""))
    if not token:
        return _panel(request, user, "Paste your bot token first.", "error")
    try:
        # New token, or one saved before we recorded the bot's name: validate and look it up.
        fields = _bot_fields(token) if telegram_bot_token.strip() or not contacts.get("telegram_bot_username") else {}
        chat = notify.telegram_find_chat(token)
    except RuntimeError as e:
        return _panel(request, user, str(e), "error")
    # A new bot starts with no chats. Otherwise the chat found is added to the ones already linked.
    linked = [] if "telegram_bot_token" in fields else _chats(contacts)
    known = chat and chat["id"] in [c["id"] for c in linked]
    if chat and not known:
        if len(linked) >= notify.MAX_RECIPIENTS:
            return _panel(request, user, _too_many(linked + [chat]), "error")
        chats = linked + [chat]
        fields.update(telegram_chat_id=", ".join(c["id"] for c in chats), telegram_chat_name="",
                      telegram_chat_names={c["id"]: c["name"] for c in chats}, test_telegram=None)
    if fields:
        store.update("contacts", username, fields)
    bot = fields.get("telegram_bot_username") or contacts.get("telegram_bot_username")
    who = chat and (chat["name"] or chat["id"])
    if not chat:
        return _panel(request, user, f"Open @{bot} in Telegram, send it any message, then tap Find my chat ID again." if bot
                      else "Send your bot any message in Telegram, then tap Find my chat ID again.", "error")
    if known:
        return _panel(request, user, f"{who} is already linked. To add someone else, have them message "
                                     f"{'@' + bot if bot else 'the bot'} first, then tap Find my chat ID.", "error")
    return _panel(request, user, f"{'Added' if linked else 'Linked to'} {who}. Now send a test message.")


@router.post("/email/cancel")
def email_cancel(request: Request, user: dict = Depends(guard)):
    store.update("contacts", user["username"], {"email_pending": None})
    return _panel(request, user, "Cancelled. Nothing was added.")


@router.post("/email/drop")
def email_drop(request: Request, user: dict = Depends(guard), address: str = Form(...)):
    """Stop sending to one address (confirmed, or one saved before confirmation existed)."""
    contacts = notify.load_contacts(user["username"])
    store.update("contacts", user["username"], {
        "emails": [a for a in contacts.get("emails") or [] if a != address],
        "email": ", ".join(a for a in notify.split_list(contacts.get("email")) if a.lower() != address) or None})
    return _panel(request, user, f"{address} removed")


@router.post("/{channel}/remove")
def remove(request: Request, channel: str, user: dict = Depends(guard)):
    if channel not in notify.CHANNEL_FIELDS:
        return _panel(request, user, "Unknown channel", "error")
    store.update("contacts", user["username"], {k: None for k in notify.CHANNEL_FIELDS[channel]} | {f"test_{channel}": None})
    return _panel(request, user, f"{notify.CHANNELS[channel]} removed")


@router.post("/{channel}")
def save(request: Request, channel: str, user: dict = Depends(guard),
         telegram_bot_token: str = Form(""), telegram_chat_id: str = Form(""), email: str = Form(""),
         whatsapp_phone: str = Form(""), whatsapp_apikey: str = Form(""),
         code: str = Form(""), action: str = Form("")):
    if channel == "email":  # nothing is saved directly: an address is added by confirming its code
        if action == "confirm" or (not action and code.strip() and not email.strip()):
            return _confirm_code(request, user, code)
        return _send_code(request, user, email)
    fields: dict = {}
    if channel == "telegram":
        if telegram_bot_token.strip():
            try:
                fields.update(_bot_fields(telegram_bot_token.strip()))
            except RuntimeError as e:
                return _panel(request, user, str(e), "error")
        ids = notify.split_list(telegram_chat_id)
        if ids:  # an empty box never wipes a linked chat; use Remove for that
            if error := _too_many(ids):
                return _panel(request, user, error, "error")
            names = {c["id"]: c["name"] for c in _chats(notify.load_contacts(user["username"])) if c["name"]}
            fields.update(telegram_chat_id=", ".join(ids), telegram_chat_name="",
                          telegram_chat_names={i: names[i] for i in ids if i in names})
    elif channel == "whatsapp":
        contacts = notify.load_contacts(user["username"])
        saved = dict(notify.whatsapp_recipients(contacts))  # number -> its key
        typed = list(dict.fromkeys(filter(None, (re.sub(r"[^\d+]", "", p) for p in re.split(r"[,;\n]+", whatsapp_phone)))))
        phones = typed or notify.split_list(contacts.get("whatsapp_phone"))
        keys = notify.split_list(whatsapp_apikey)
        if error := _too_many(phones):
            return _panel(request, user, error, "error")
        if len(phones) <= 1:
            if len(keys) > 1:
                return _panel(request, user, "That's more keys than numbers. Add each number, separated by commas.", "error")
            if typed:
                fields["whatsapp_phone"] = typed[0]
            if keys or (typed and typed[0] in saved):
                fields["whatsapp_apikey"] = encrypt(keys[0] if keys else saved[typed[0]])
        else:
            # Keys go with numbers in order: one for every number, or only for the numbers that are new.
            new = [p for p in phones if p not in saved]
            if len(keys) == len(phones):
                saved = dict(zip(phones, keys))
            elif len(keys) == len(new):
                saved.update(zip(new, keys))
            else:
                return _panel(request, user, "Each number needs its own CallMeBot key. Give the keys for "
                              f"{'all ' + str(len(phones)) + ' numbers' if not new else ', '.join(new)}, "
                              "in the same order, separated by commas.", "error")
            fields["whatsapp_phone"] = ", ".join(phones)
            fields["whatsapp_apikey"] = encrypt(", ".join(saved[p] for p in phones))
    if not fields:
        return _panel(request, user, "Nothing to save. Fill in the details first.", "error")
    fields[f"test_{channel}"] = None  # new details haven't been tested yet
    store.update("contacts", user["username"], fields)
    label = notify.CHANNELS.get(channel, "Settings")
    contacts = notify.load_contacts(user["username"])
    ready = notify.recipient_ready(channel, contacts) and notify.sender_ready(channel)
    return _panel(request, user, f"{label} saved. Send a test message to make sure it reaches you." if ready else f"{label} saved")


@router.post("/{channel}/test")
def test(request: Request, channel: str, user: dict = Depends(guard)):
    now = now_ist()
    result = notify.send(user["username"], [channel], "✅ Test from your stock scanner",
                         f"If you can read this, price alerts will reach you here.\nSent {now:%d %b, %-I:%M %p} IST").get(channel, "Unknown channel")
    ok = result == "sent"
    store.update("contacts", user["username"],
                 {f"test_{channel}": {"at": now.isoformat(), "ok": ok, "detail": "" if ok else result}})
    return toast(render(request, "partials/channels.html", _ctx(user)),
                 _sent_message(channel) if ok else f"Couldn't send: {result}", "success" if ok else "error")


def _sent_message(channel: str) -> str:
    if channel == "email":
        return "Test email sent. Check your inbox and spam folder. If it's in spam, mark it as not spam."
    return f"Test sent. Check your {notify.CHANNELS.get(channel, channel)}."
