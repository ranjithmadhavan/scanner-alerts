import os
import tempfile

os.environ.setdefault("STORAGE", "memory")
os.environ.setdefault("SCANNER_ENABLED", "0")
os.environ.setdefault("BIAS_CAPTURE", "0")
os.environ.setdefault("OI_CAPTURE", "0")  # tests that want scheduled OI snapshots switch it on
os.environ.setdefault("SESSION_SECRET", "test")
os.environ.setdefault("INSTRUMENTS_CACHE_DIR", tempfile.mkdtemp(prefix="instruments-"))  # never the real cache


import pytest


@pytest.fixture(autouse=True)
def _kite_without_waiting(monkeypatch):
    """Tests never wait on the Kite rate limiters, and never reach the real Kite for instrument lists."""
    from app import kite
    for name in ("HISTORICAL", "QUOTES", "OTHER", "DUMPS", "BACKTESTS"):
        monkeypatch.setattr(kite, name, kite.RateLimit(0))
    monkeypatch.setattr(kite, "TOO_MANY_PAUSE", 0)
    monkeypatch.setattr(kite, "DUMPS_PAUSE", 0)
    monkeypatch.setattr(kite, "INSTRUMENTS_URL", "http://127.0.0.1:9/instruments")  # refused at once
    monkeypatch.setattr(kite, "_waits", {})
    monkeypatch.setattr(kite, "_waits_loaded", True)


@pytest.fixture(autouse=True)
def _webhooks_inline(monkeypatch):
    """Webhook deliveries run as they are sent rather than in the background, and retry without waiting."""
    from app import webhooks
    monkeypatch.setattr(webhooks, "_run", lambda fn, *a: fn(*a))
    monkeypatch.setattr(webhooks, "RETRY_WAITS", (0, 0))
