"""Optional per-alert webhooks: when a level is hit, POST a JSON description of it to the
alert's URLs. Each alert can carry its own extra JSON, passed through untouched as "payload".

The server makes these requests, so URLs must be public http(s) addresses (nothing on the
server's own network), redirects aren't followed, and this is re-checked on every send.
"""

import ipaddress
import json
import socket
from urllib.parse import urlsplit

import httpx

MAX_URLS = 5
MAX_PAYLOAD_CHARS = 4000
TIMEOUT = 8


def _is_public(host: str) -> bool:
    try:
        found = socket.getaddrinfo(host, None)
    except OSError:
        return False
    return bool(found) and all(ipaddress.ip_address(info[4][0]).is_global for info in found)


def url_problem(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or len(url) > 500:
        return f"{url[:60]} isn't a web address. Webhook URLs start with https://"
    if not _is_public(parts.hostname):
        return f"{parts.hostname} can't be reached from the internet, so it can't be used as a webhook."
    return None


def parse_urls(text: str) -> tuple[list[str], str | None]:
    """The form's box of URLs, one per line. Returns (urls, None) or ([], error)."""
    urls = list(dict.fromkeys(text.split()))
    if len(urls) > MAX_URLS:
        return [], f"An alert can have up to {MAX_URLS} webhook URLs."
    for url in urls:
        if error := url_problem(url):
            return [], error
    return urls, None


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


def send(urls: list[str], body: dict) -> str:
    """POST `body` to every URL. Returns "sent", or which ones failed. Never raises."""
    failed = []
    for url in urls:
        host = urlsplit(url).hostname or url[:40]
        try:
            if error := url_problem(url):
                raise RuntimeError("not a public address" if "internet" in error else "not a web address")
            r = httpx.post(url, json=body, timeout=TIMEOUT, follow_redirects=False,
                           headers={"User-Agent": "stock-scanner-alerts"})
            if r.status_code >= 300:
                raise RuntimeError(f"HTTP {r.status_code}")
        except httpx.HTTPError as e:
            failed.append(f"{host}: {e.__class__.__name__}")
        except RuntimeError as e:
            failed.append(f"{host}: {e}")
    if not failed:
        return "sent"
    return (f"Reached {len(urls) - len(failed)} of {len(urls)}. " if len(urls) > 1 else "") + "; ".join(failed)
