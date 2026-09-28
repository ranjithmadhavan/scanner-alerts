"""Free notification channels: Telegram bot, email (Brevo or Gmail SMTP), WhatsApp via CallMeBot.

Telegram and WhatsApp are fully per-user (each user brings their own bot / CallMeBot key).
Email is sent from one app-wide Gmail account configured in env; users only give an address.
"""

import html
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
        return email_provider() is not None
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


def email_provider() -> str | None:
    if config.BREVO_API_KEY and config.EMAIL_FROM:
        return "brevo"
    if config.SMTP_USER and config.SMTP_PASSWORD:
        return "smtp"
    return None


def _email_html(subject: str, body: str) -> str:
    lines = "".join(f"<p style='margin:0 0 8px'>{html.escape(line)}</p>" for line in body.splitlines() if line.strip())
    return (
        "<div style='font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#16373A;max-width:520px;"
        "padding:24px;border:1px solid #ECE6DC;border-radius:16px;background:#FFFFFF'>"
        f"<p style='margin:0 0 14px;font-size:18px;font-weight:700'>{html.escape(subject)}</p>{lines}"
        f"<p style='margin:18px 0 0;font-size:12px;color:#5F7476'>Sent by {html.escape(config.APP_NAME)}</p></div>"
    )


def _email_brevo(to: str, subject: str, body: str) -> None:
    try:
        r = httpx.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": config.BREVO_API_KEY, "accept": "application/json"},
            json={
                "sender": {"name": config.EMAIL_FROM_NAME, "email": config.EMAIL_FROM},
                "to": [{"email": to}],
                "subject": subject,
                "textContent": body,
                "htmlContent": _email_html(subject, body),
            },
            timeout=20,
        )
    except httpx.HTTPError:
        raise RuntimeError("Couldn't reach Brevo") from None
    if r.status_code >= 300:
        try:
            detail = r.json().get("message", "")
        except ValueError:
            detail = ""
        if r.status_code == 401:
            detail = "Brevo rejected the API key"
        raise RuntimeError(detail or f"Brevo returned HTTP {r.status_code}")


def _email_smtp(to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = config.SMTP_FROM, to, subject
    msg.set_content(body)
    with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as s:
        s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.send_message(msg)


def _email(contacts: dict, subject: str, body: str) -> None:
    if email_provider() == "brevo":
        _email_brevo(contacts["email"], subject, body)
    else:
        _email_smtp(contacts["email"], subject, body)


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
