"""Template rendering helpers shared by all routers."""

import json
import time
from datetime import datetime

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app import config
from app.market import IST, load_settings, now_ist, status_text
from app.security import allowed_modules

templates = Jinja2Templates(directory="app/templates")
STATIC_VERSION = str(int(time.time()))  # busts browser caches on each deploy


def _ist(value: str | datetime | None, fmt: str = "%d %b, %-I:%M %p") -> str:
    if not value:
        return ""
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    return dt.astimezone(IST).strftime(fmt)


def _price(value) -> str:
    return "" if value is None else f"{float(value):,.2f}"


def _company(name: str) -> str:
    """Kite names are ALL CAPS. Soften them but keep acronyms: 'HDFC BANK' -> 'HDFC Bank'."""
    def word(w: str) -> str:
        return w if len(w) <= 3 or not any(ch in "AEIOU" for ch in w) else w.capitalize()
    return " ".join(word(w) for w in (name or "").split())


templates.env.filters["company"] = _company
templates.env.filters["ist"] = _ist
templates.env.filters["price"] = _price


def render(request: Request, name: str, ctx: dict | None = None, status_code: int = 200) -> HTMLResponse:
    user = getattr(request.state, "user", None)
    is_open, market_text = status_text(load_settings(), now_ist())
    base = {
        "app_name": config.APP_NAME,
        "v": STATIC_VERSION,
        "user": user,
        "nav": allowed_modules(user) if user else [],
        "market_open": is_open,
        "market_text": market_text,
        "path": request.url.path,
    }
    return templates.TemplateResponse(request, name, {**base, **(ctx or {})}, status_code=status_code)


def toast(response: HTMLResponse, message: str, kind: str = "success") -> HTMLResponse:
    response.headers["HX-Trigger"] = json.dumps({"toast": {"message": message, "kind": kind}})
    return response


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def fail(message: str) -> HTMLResponse:
    """Validation error for an htmx form: leave the page as-is and show a toast."""
    return toast(HTMLResponse("", headers={"HX-Reswap": "none"}), message, "error")
