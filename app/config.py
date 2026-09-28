"""Environment-driven settings. Everything secret lives in env vars, never in Firestore."""

import os

from dotenv import load_dotenv

load_dotenv()  # local .env; on Render the dashboard env is used


def _env(name: str, default: str = "") -> str:
    """Env value, treating a blank setting (NAME=) the same as a missing one."""
    return (os.environ.get(name) or "").strip() or default


APP_NAME = _env("APP_NAME", "Stock Scanner")
BASE_URL = _env("BASE_URL", "http://127.0.0.1:8000").rstrip("/")

# Sessions + at-rest encryption of broker secrets.
SESSION_SECRET = _env("SESSION_SECRET")
ENCRYPTION_KEY = _env("ENCRYPTION_KEY")  # Fernet key: python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
COOKIE_SECURE = BASE_URL.startswith("https://")

# Super admin is seeded from env on every startup (env is the source of truth).
SUPERADMIN_USERNAME = _env("SUPERADMIN_USERNAME").lower()
SUPERADMIN_PASSWORD = _env("SUPERADMIN_PASSWORD")

# Storage: "firestore" (default) or "memory" (local dev only, data lost on restart).
STORAGE = _env("STORAGE", "firestore")
FIREBASE_CREDENTIALS_JSON = _env("FIREBASE_CREDENTIALS_JSON")  # raw JSON (Render)
FIREBASE_CREDENTIALS_PATH = _env("FIREBASE_CREDENTIALS_PATH", "firebase-credentials.json")
COLLECTION_PREFIX = _env("COLLECTION_PREFIX", "ssa_")
# Reads are cached in-process; writes invalidate immediately. Only affects edits made outside the app.
CACHE_TTL_SECONDS = float(_env("CACHE_TTL_SECONDS", "300"))

# Scanner runs in-process; disable on extra instances or in tests.
SCANNER_ENABLED = _env("SCANNER_ENABLED", "1") == "1"

# Keep-alive: ping our own public URL so Render's free tier doesn't put the app to sleep.
# Render sets RENDER_EXTERNAL_URL automatically.
KEEP_ALIVE_URL = (_env("KEEP_ALIVE_URL") or _env("RENDER_EXTERNAL_URL")).rstrip("/")
KEEP_ALIVE_SECONDS = int(_env("KEEP_ALIVE_SECONDS", "300"))

# Email sender (app-wide). Telegram/WhatsApp are configured per user in the UI.
# Brevo (HTTPS API, free 300/day) is used when BREVO_API_KEY is set; otherwise Gmail SMTP.
BREVO_API_KEY = _env("BREVO_API_KEY")
EMAIL_FROM = _env("EMAIL_FROM")            # must be a verified sender in Brevo
EMAIL_FROM_NAME = _env("EMAIL_FROM_NAME", APP_NAME)
SMTP_HOST = _env("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(_env("SMTP_PORT", "465"))
SMTP_USER = _env("SMTP_USER")
SMTP_PASSWORD = _env("SMTP_PASSWORD")
SMTP_FROM = _env("SMTP_FROM") or SMTP_USER
