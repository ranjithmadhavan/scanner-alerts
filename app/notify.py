"""Free notification channels: Telegram bot, Gmail SMTP, WhatsApp via CallMeBot.

Telegram and WhatsApp are fully per-user (each user brings their own bot / CallMeBot key).
Email is sent from one app-wide Gmail account configured in env; users only give an address.
"""

import smtplib
from email.message import EmailMessage

import httpx

from app import config
from app.security import decrypt
from app.store import store

CHANNELS = {
    "telegram": "Telegram",
    "email": "Email",
    "whatsapp": "WhatsApp",
}


def sender_ready(channel: str) -> bool:
    if channel == "email":
        return bool(config.SMTP_USER and config.SMTP_PASSWORD)
    return True  # Telegram and CallMeBot need nothing app-wide


def load_contacts(username: str) -> dict:
    return store.get("contacts", username) or {}


def recipient_ready(channel: str, contacts: dict) -> bool:
    if channel == "telegram":
        return bool(contacts.get("telegram_bot_token") and contacts.get("telegram_chat_id"))
    if channel == "email":
        return bool(contacts.get("email"))
    if channel == "whatsapp":
        return bool(contacts.get("whatsapp_phone") and contacts.get("whatsapp_apikey"))
    return False


def _telegram_call(method: str, bot_token: str, **kwargs) -> httpx.Response:
    # Never let the URL (which contains the bot token) end up in an error message.
    try:
        return httpx.request(method, f"https://api.telegram.org/bot{bot_token}/" + kwargs.pop("path"), timeout=15, **kwargs)
    except httpx.HTTPError:
        raise RuntimeError("Couldn't reach Telegram") from None


def _telegram_error(r: httpx.Response) -> str:
    """Turn Telegram's terse errors into something the user can act on."""
    try:
        desc = r.json().get("description", "")
    except ValueError:
        desc = ""
    low = desc.lower()
    if r.status_code in (401, 404) or "unauthorized" in low:
        return "Telegram doesn't recognise this bot token. Copy it again from BotFather."
    if "chat not found" in low:
        return "Telegram can't find that chat. Send your bot a message, then tap Find my chat ID."
    if "blocked by the user" in low:
        return "You've blocked the bot in Telegram. Unblock it and try again."
    return desc or f"Telegram returned HTTP {r.status_code}"


def _telegram(contacts: dict, subject: str, body: str) -> None:
    r = _telegram_call("POST", decrypt(contacts["telegram_bot_token"]), path="sendMessage",
                       json={"chat_id": contacts["telegram_chat_id"], "text": f"{subject}\n\n{body}"})
    if r.status_code != 200:
        raise RuntimeError(_telegram_error(r))


def _email(contacts: dict, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = config.SMTP_FROM, contacts["email"], subject
    msg.set_content(body)
    with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as s:
        s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.send_message(msg)


def _whatsapp(contacts: dict, subject: str, body: str) -> None:
    r = httpx.get(
        "https://api.callmebot.com/whatsapp.php",
        params={
            "phone": contacts["whatsapp_phone"],
            "text": f"*{subject}*\n{body}",
            "apikey": decrypt(contacts["whatsapp_apikey"]),
        },
        timeout=20,
    )
    if r.status_code != 200 or "ERROR" in r.text.upper():
        raise RuntimeError(r.text[:200] or f"HTTP {r.status_code}")


def telegram_bot_info(bot_token: str) -> dict:
    """Validate a bot token; returns {"username", "name"} of the bot."""
    r = _telegram_call("GET", bot_token, path="getMe")
    if r.status_code != 200:
        raise RuntimeError(_telegram_error(r))
    bot = r.json()["result"]
    return {"username": bot.get("username", ""), "name": bot.get("first_name", "")}


def telegram_find_chat(bot_token: str) -> dict | None:
    """The chat that most recently messaged the bot: {"id", "name"}. None if nobody has yet."""
    r = _telegram_call("GET", bot_token, path="getUpdates")
    if r.status_code != 200:
        raise RuntimeError(_telegram_error(r))
    for update in reversed(r.json().get("result", [])):
        chat = (update.get("message") or update.get("my_chat_member") or {}).get("chat")
        if chat:
            name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
            if chat.get("username"):
                name = f"{name} (@{chat['username']})" if name else f"@{chat['username']}"
            return {"id": str(chat["id"]), "name": name}
    return None


def configured(channel: str, contacts: dict) -> bool:
    """Has the user saved anything for this channel (even if it isn't complete yet)?"""
    keys = {"telegram": ("telegram_bot_token", "telegram_chat_id"), "email": ("email",),
            "whatsapp": ("whatsapp_phone", "whatsapp_apikey")}[channel]
    return any(contacts.get(k) for k in keys)


CHANNEL_FIELDS = {
    "telegram": ["telegram_bot_token", "telegram_bot_username", "telegram_bot_name", "telegram_chat_id", "telegram_chat_name"],
    "email": ["email"],
    "whatsapp": ["whatsapp_phone", "whatsapp_apikey"],
}


def ready_channels(username: str) -> list[str]:
    contacts = load_contacts(username)
    return [c for c in CHANNELS if sender_ready(c) and recipient_ready(c, contacts)]


_SENDERS = {"telegram": _telegram, "email": _email, "whatsapp": _whatsapp}


def send(username: str, channels: list[str], subject: str, body: str) -> dict[str, str]:
    """Send on each channel; returns {channel: "sent" | error message}. Never raises."""
    contacts = load_contacts(username)
    results = {}
    for ch in channels:
        if not sender_ready(ch):
            results[ch] = "Not set up on the server"
        elif not recipient_ready(ch, contacts):
            results[ch] = "No recipient details saved"
        else:
            try:
                _SENDERS[ch](contacts, subject, body)
                results[ch] = "sent"
            except Exception as e:  # one bad channel must not block the others
                results[ch] = str(e)[:200] or e.__class__.__name__
    return results
