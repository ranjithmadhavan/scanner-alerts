# Stock Scanner

Price-level alerts for stocks, indices, futures and options on NSE, BSE, NFO and BFO. Users add an instrument and one or more levels, each with a condition, and get a Telegram / email / WhatsApp message when it's hit. FastAPI + Jinja + HTMX + Tailwind, Firestore for storage, Zerodha Kite for prices.

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
| Kite client (Kite Connect **or** enctoken) + instrument lists for NSE, BSE, NFO, BFO | `app/kite.py` |
| Telegram (each user's own bot) / email via Brevo or Gmail SMTP (app-wide) / WhatsApp (CallMeBot) | `app/notify.py` |
| Users, password hashing, permission guards | `app/security.py` |
| Grantable app areas | `app/modules.py` |
| Firestore / in-memory store (collections prefixed `ssa_`) | `app/store.py` |

**Conditions.** *Closes above/below* checks each completed candle of the chosen timeframe, 10 s after it closes. *Trades above/below* checks 1-minute data from the moment the alert is armed, so it fires as soon as any trade crosses the level. The timeframe doesn't matter for these. *Crosses* and *Closes across* are the same two checks without a fixed direction: the level fires when price gets to the other side of it from where it started, which is the day's open, or the price at the moment the alert was armed if that was during today's session. A gap through a level overnight doesn't fire it; the level then waits for price to come back through from the new side.

**Levels.** An alert can hold up to 10 levels, each with its own condition. A level sends one message when it is hit and is then switched off; the other levels stay on watch. When the last level has fired the alert moves to *Triggered* and waits there until the user taps **Watch again**, which puts every level back on watch. Alerts saved before this (a single `level` and `condition` on the document) are read as one-level alerts.

**Fractal alerts.** The New alert form has a second kind, *Fractals*, where no price is entered: the levels are the instrument's unmitigated fractals. A fractal is three completed candles, the same rule as algo-nisha: a fractal high when the middle candle's high is >= both neighbours, a fractal low when its low is <= both. It is unmitigated until a later candle trades beyond it (`app/fractals.py`).

- Timeframes: 3, 5, 10, 15 and 30 min (default 30) look back 10 sessions, 1 hour looks back 20 and Daily 120.
- Per alert you choose what to hear about: the fractal is *taken* (any trade beyond it, caught on 1-minute data), *swept* (a candle of the timeframe trades beyond it but closes back), or its *break fails* (a candle closes beyond it and the next one closes back). You can also watch highs only or lows only.
- **Trigger candle.** *Swept* and *break fails* wait for a candle to close. By default that is a candle of the fractal timeframe, but each alert can name a shorter one (1, 3, 5, 10, 15, 30 min or 1 hour) as long as it is shorter than the fractal candles and divides into them: 30-minute fractals can be judged on 5-minute closes, so a sweep is reported five minutes after it happens rather than at the end of the half hour.
- A fractal high reads as a potential sell and a fractal low as a potential buy. Every message gives the target: the nearest unmitigated fractal on the other side of price.
- A gap through a fractal (the session, or a candle, opens beyond it) retires it without a message.
- The levels are recalculated each time a candle of the timeframe closes. A fractal alert never moves to *Triggered*; it keeps watching until paused or removed. Each fractal is reported once per chosen outcome.
- **Backtest fractals** (the Simulate button in Fractals mode) replays the look-back period on the trigger candles. It says which candles the fractals and the triggers used, draws those candles with an arrow on every signal, and lists each signal with its target, its stop (SL) and how it went: *target reached*, *SL gone* (and whether the target was reached later anyway), or *still open*, with a tally at the top. The SL is the high (sell) or low (buy) made by the candle that took the fractal, plus the candle before it for a failed break; if one candle trades both the SL and the target it counts as SL gone. Clicking a row jumps the chart to that signal and draws its fractal level, target and SL. Nothing is saved or sent. For daily fractals with a small trigger candle the period is capped by how far back Kite serves that candle size in one request (60 days of 1-minute, 100 days of 3 to 10-minute candles).
- Webhooks get `"event": "fractal_hit"` with `side`, `trigger` (`touch` / `reject` / `fail`), `signal`, `level`, `price`, `target`, `timeframe`, `trigger_timeframe` and `fractal_time`.

**Editing.** The pencil on an alert opens an edit panel: add levels, change or remove existing ones, tick *Watch again* on a level that has fired, and change the candle timeframe or message. Levels left untouched keep their state. If anything was added, changed or re-armed, the alert is re-armed from that moment, so a new level can't fire on a move that happened before it was added.

**Instruments.** Kite's public lists for NSE, BSE, NFO and BFO are loaded at start and refreshed every 4 hours, so newly listed futures and options show up in the search the same day. NSE symbols are plain (`INFY`); the others carry their exchange (`BSE:SENSEX`, `NFO:NIFTY26OCT24500CE`). The search matches every word against the symbol and a spelled-out name, so `nifty 24500 ce` or `sensex oct fut` finds contracts. An alert on a contract is paused once the contract has expired.

**Price and chart.** Picking a stock shows its last price and the day's change. **View chart** (and the chart icon on each alert) opens a side panel with 1D/5D/1M/6M/1Y candles and your levels drawn in. Data comes from the user's own Kite session and is cached briefly (`app/prices.py`). The chart is drawn with TradingView's Lightweight Charts, loaded from jsDelivr on first use.

**Alert message.** Each alert can carry its own message (the optional *Alert message* box), which leads the notification when a level is hit. Without one, the hit is worded as a liquidity trade: a move up through a level is a *Potential sell*, a move down through it a *Potential buy*.

**Several recipients.** Each channel can send to up to 10 recipients: several Telegram chats or groups on the user's bot, several email addresses (one email each), several WhatsApp numbers (each with its own CallMeBot key). Every alert on that channel goes to all of them.

**Confirmed email addresses.** An email address is added on its own and only after its owner enters a 6-digit code we email to it (valid 10 minutes, 5 tries, one resend a minute; only a hash of the code is stored). Alerts go to confirmed addresses only. An address saved before this existed is shown as not confirmed and receives nothing until it is.

**Webhooks (optional).** An alert can have up to 5 webhook URLs, set in the New alert form or the edit panel. When a level is hit, each URL gets a JSON `POST`:

```json
{"event": "level_hit", "test": false, "alert_id": "…", "symbol": "SENSEX", "exchange": "BSE", "name": "SENSEX",
 "message": "Potential sell", "text": "SENSEX crossed above your level of 82000.",
 "condition": "high_above", "direction": "above", "level": 82000.0, "price": 82014.5, "timeframe": null,
 "candle": "2026-10-01T11:00:00+05:30", "time": "2026-10-01T11:00:30+05:30",
 "still_watching": [{"condition": "cross", "level": 84000.0}],
 "payload": {"strategy": "sweep", "qty": 50}}
```

`message` is the alert's own message, or *Potential sell* / *Potential buy*. `payload` is the alert's own JSON, passed through untouched (`null` if none). Any 2xx reply counts as delivered; the result is shown under *Recently sent* on the Notifications page. URLs must be public http(s) addresses, redirects aren't followed, and each request times out after 8 seconds (`app/webhooks.py`). **Send a test request** in the edit panel posts a sample with `"test": true`. Simulations never call webhooks.

**Channels per alert.** Each alert has its own set of channels. New alerts start with every channel you've set up, and you can switch channels on or off for each alert from the list.

**When scanning happens.** Only on trading days, from the open until two minutes after the close (so the day's last candle can be checked once it has finished). Trading days are Monday to Friday minus the market holidays listed on the Market hours page. That list starts with NSE's 2026 holidays; the super admin can add or remove days, or pull NSE's current list with **Update from NSE**. Outside those times the scheduler still ticks but returns straight away: no Kite calls, no alerts, no login notices.

**Kite sessions.** An expired session isn't treated as an error. At market open, the scanner checks each user who has active alerts once. If Kite isn't connected or the session has died, it tells that user on all their ready channels (once a day) and skips them until they log in again. Scanning then resumes straight away.

**Speed.** Firestore reads are cached in memory (`app/store.py`), and the app's own writes clear the affected entries immediately. Every request logs a line such as `GET /alerts 200 4ms (db 0 calls, 0ms)`, and the same figures appear in the browser's Network tab as a `Server-Timing` header.

**Staying alive.** A background thread pings the app's public URL and restarts the scheduler if it has stopped.

**Ownership.** Each user has their own alerts, Kite connection and notification contacts. The super admin is seeded from `SUPERADMIN_USERNAME` / `SUPERADMIN_PASSWORD` on each start. It creates users and picks which areas each one can use.

**Adding an area** (e.g. trading): add a `Module` in `app/modules.py`, a router guarded by `require("<key>")`, and its templates. It then appears in the navigation and in the permissions form.

## Known limits

- Special sessions outside normal hours (Muhurat trading, weekend sessions) aren't scanned.
- Last prices shown on the alerts page live in memory and appear after the first scan following a restart.
