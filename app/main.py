import logging
import threading
import time
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import config, kite, scanner
from app.store import db_stats, store
from app.routes import account, admin, alerts, auth, broker, notifications
from app.security import Forbidden, LoginRequired, seed_superadmin
from app.web import is_htmx, render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

if config.STORAGE == "firestore" and not (config.SESSION_SECRET and config.ENCRYPTION_KEY):
    raise RuntimeError("Set SESSION_SECRET and ENCRYPTION_KEY — stored broker secrets are encrypted with ENCRYPTION_KEY.")
if not config.SESSION_SECRET:
    logging.warning("SESSION_SECRET not set — using a random one; everyone is logged out on restart.")
    config.SESSION_SECRET = secrets.token_urlsafe(32)


def _warm_caches() -> None:
    """Preload what every page needs so the first clicks after a restart are fast too."""
    try:
        store.warm("users")
        store.warm("alerts")
        store.get("settings", "market")
    except Exception as e:
        logging.warning("couldn't preload data: %s", e)
    try:
        kite.instruments()
    except Exception as e:
        logging.warning("couldn't preload instruments: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    seed_superadmin()
    threading.Thread(target=_warm_caches, daemon=True).start()
    if config.SCANNER_ENABLED:
        scanner.start()
    yield
    scanner.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSION_SECRET,
    same_site="lax",
    https_only=config.COOKIE_SECURE,
    max_age=60 * 60 * 24 * 7,
)


@app.middleware("http")
async def timing(request: Request, call_next):
    """Log how long each page took and how much of it was the database.
    Also sent as a Server-Timing header, visible in the browser's Network tab."""
    if request.url.path.startswith("/static") or request.url.path == "/health":
        return await call_next(request)
    stats = [0, 0.0]
    token = db_stats.set(stats)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        db_stats.reset(token)
    total = (time.perf_counter() - start) * 1000
    db_ms = stats[1] * 1000
    response.headers["Server-Timing"] = f"db;desc=\"{stats[0]} calls\";dur={db_ms:.0f}, app;dur={total:.0f}"
    logging.getLogger("timing").info("%s %s %s %.0fms (db %d calls, %.0fms)",
                                     request.method, request.url.path, response.status_code, total, stats[0], db_ms)
    return response


app.mount("/static", StaticFiles(directory="app/static"), name="static")

for r in (auth, alerts, broker, notifications, admin, account):
    app.include_router(r.router)


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, exc: LoginRequired):
    if is_htmx(request):
        return HTMLResponse("", headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(Forbidden)
async def _forbidden(request: Request, exc: Forbidden):
    return render(request, "forbidden.html", status_code=403)


@app.api_route("/health", methods=["GET", "HEAD"])  # HEAD for UptimeRobot
def health():
    return {"ok": True}
