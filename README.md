# Stock Scanner

Price-level alerts for NSE stocks. Users add a stock, a level and a condition, and get a Telegram / email / WhatsApp message when it's hit. FastAPI + Jinja + HTMX + Tailwind, Firestore for storage, Zerodha Kite for prices.

## Run locally

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements-dev.txt
cp .env.example .env        # fill in SESSION_SECRET, ENCRYPTION_KEY, SUPERADMIN_*, Firebase
.venv/bin/uvicorn app.main:app --reload
```

Set `STORAGE=memory` to try it without Firebase (data is lost on restart).

Tests: `.venv/bin/python -m pytest`

Tailwind is pre-built into `app/static/app.css`. Rebuild after changing classes in templates or `app.js`:

```bash
npx tailwindcss@3 -i app/static/app.src.css -o app/static/app.css --minify
```

## Deploy on Render

1. New **Web Service** from this repo, runtime **Docker**, **one instance**. The free plan works. The app pings its own `RENDER_EXTERNAL_URL/health` every 5 minutes so Render doesn't put it to sleep (free instances sleep after 15 idle minutes). As a backup, add a free external monitor (e.g. cron-job.org or UptimeRobot) hitting `/health`. One always-on free service uses about 744 of the 750 free hours a month.
2. Set the env vars from `.env.example`. Use `FIREBASE_CREDENTIALS_JSON` with the full service-account JSON, and set `BASE_URL` to the Render URL.
3. For Kite Connect, set the app's redirect URL on developers.kite.trade to `BASE_URL/broker/kite/callback` (also shown on the Broker page).

## How it works

| Piece | Where |
|---|---|
| Market hours (IST via `zoneinfo`, Mon–Fri, admin-editable) | `app/market.py` |
| Scanner + scheduler (APScheduler, in-process) | `app/scanner.py` |
| Kite client (Kite Connect **or** enctoken) + NSE instrument list | `app/kite.py` |
| Telegram (each user's own bot) / email via Brevo or Gmail SMTP (app-wide) / WhatsApp (CallMeBot) | `app/notify.py` |
| Users, password hashing, permission guards | `app/security.py` |
| Grantable app areas | `app/modules.py` |
| Firestore / in-memory store (collections prefixed `ssa_`) | `app/store.py` |

**Conditions.** *Closes above/below* checks each completed candle of the chosen timeframe, 10 s after it closes. *Trades above/below* checks 1-minute data from the moment the alert is armed, so it fires as soon as any trade crosses the level. The timeframe doesn't matter for these. An alert fires once, then waits in *Triggered* until the user taps **Watch again**.

**Price and chart.** Picking a stock shows its last price and the day's change. **View chart** (and the chart icon on each alert) opens a side panel with 1D/5D/1M/6M/1Y candles and your level drawn in. Data comes from the user's own Kite session and is cached briefly (`app/prices.py`). The chart is drawn with TradingView's Lightweight Charts, loaded from jsDelivr on first use.

**Channels per alert.** Each alert has its own set of channels. New alerts start with every channel you've set up, and you can switch channels on or off for each alert from the list.

**Kite sessions.** An expired session isn't treated as an error. At market open, the scanner checks each user who has active alerts once. If Kite isn't connected or the session has died, it tells that user on all their ready channels (once a day) and skips them until they log in again. Scanning then resumes straight away.

**Speed.** Firestore reads are cached in memory (`app/store.py`), and the app's own writes clear the affected entries immediately. Every request logs a line such as `GET /alerts 200 4ms (db 0 calls, 0ms)`, and the same figures appear in the browser's Network tab as a `Server-Timing` header.

**Staying alive.** A background thread pings the app's public URL and restarts the scheduler if it has stopped.

**Ownership.** Each user has their own alerts, Kite connection and notification contacts. The super admin is seeded from `SUPERADMIN_USERNAME` / `SUPERADMIN_PASSWORD` on each start. It creates users and picks which areas each one can use.

**Adding an area** (e.g. trading): add a `Module` in `app/modules.py`, a router guarded by `require("<key>")`, and its templates. It then appears in the navigation and in the permissions form.

## Known limits

- NSE holidays aren't skipped. The scanner runs on those weekdays, and Kite just returns no new candles.
- Last prices shown on the alerts page live in memory and appear after the first scan following a restart.
