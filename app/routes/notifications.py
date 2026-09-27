import re

from fastapi import APIRouter, Depends, Form, Request

from app import notify
from app.market import now_ist
from app.security import decrypt, encrypt, require
from app.store import store
from app.web import render, toast

router = APIRouter(prefix="/notifications")
guard = require("notifications")


def _ctx(user: dict) -> dict:
    contacts = notify.load_contacts(user["username"])
    return {
        "contacts": contacts,
        "hints": {"telegram": _hint(contacts.get("telegram_bot_token")), "whatsapp": _hint(contacts.get("whatsapp_apikey"))},
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


def _hint(secret_enc: str) -> str:
    """Last 4 characters of a stored secret, so users can tell which one is saved."""
    plain = decrypt(secret_enc or "")
    return plain[-4:] if len(plain) >= 10 else plain[-2:] if len(plain) >= 5 else ""


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
    if chat:
        fields.update(telegram_chat_id=chat["id"], telegram_chat_name=chat["name"], test_telegram=None)
    if fields:
        store.update("contacts", username, fields)
    bot = fields.get("telegram_bot_username") or contacts.get("telegram_bot_username")
    if not chat:
        return _panel(request, user, f"Open @{bot} in Telegram, send it any message, then tap Find my chat ID again." if bot
                      else "Send your bot any message in Telegram, then tap Find my chat ID again.", "error")
    return _panel(request, user, f"Linked to {chat['name'] or chat['id']}. Now send a test message.")


@router.post("/{channel}/remove")
def remove(request: Request, channel: str, user: dict = Depends(guard)):
    if channel not in notify.CHANNEL_FIELDS:
        return _panel(request, user, "Unknown channel", "error")
    store.update("contacts", user["username"], {k: None for k in notify.CHANNEL_FIELDS[channel]} | {f"test_{channel}": None})
    return _panel(request, user, f"{notify.CHANNELS[channel]} removed")


@router.post("/{channel}")
def save(request: Request, channel: str, user: dict = Depends(guard),
         telegram_bot_token: str = Form(""), telegram_chat_id: str = Form(""), email: str = Form(""),
         whatsapp_phone: str = Form(""), whatsapp_apikey: str = Form("")):
    fields: dict = {}
    if channel == "telegram":
        if telegram_bot_token.strip():
            try:
                fields.update(_bot_fields(telegram_bot_token.strip()))
            except RuntimeError as e:
                return _panel(request, user, str(e), "error")
        if telegram_chat_id.strip():  # an empty box never wipes a linked chat; use Remove for that
            fields.update(telegram_chat_id=telegram_chat_id.strip(), telegram_chat_name="")
    elif channel == "email":
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip()):
            return _panel(request, user, "That email address looks incomplete.", "error")
        fields["email"] = email.strip()
    elif channel == "whatsapp":
        phone = re.sub(r"[^\d+]", "", whatsapp_phone)
        if phone:
            fields["whatsapp_phone"] = phone
        if whatsapp_apikey.strip():
            fields["whatsapp_apikey"] = encrypt(whatsapp_apikey.strip())
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
                 f"Test sent. Check your {notify.CHANNELS.get(channel, channel)}." if ok else f"Couldn't send: {result}", "success" if ok else "error")
