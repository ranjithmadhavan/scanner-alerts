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
| MA crossover rules / backtest runner / live scanners | `app/crossover.py`, `app/ma_backtest.py`, `app/ma_scanner.py` |
| Stock lists (Nifty 50–500, F&O) | `app/universe.py` |
| Firestore / in-memory store (collections prefixed `ssa_`) | `app/store.py` |

**Conditions.** *Closes above/below* checks each completed candle of the chosen timeframe, 10 s after it closes. *Trades above/below* checks 1-minute data from the moment the alert is armed, so it fires as soon as any trade crosses the level. The timeframe doesn't matter for these. *Crosses* and *Closes across* are the same two checks without a fixed direction: the level fires when price gets to the other side of it from where it started, which is the day's open, or the price at the moment the alert was armed if that was during today's session. A gap through a level overnight doesn't fire it; the level then waits for price to come back through from the new side.

**Levels.** An alert can hold up to 10 levels, each with its own condition. A level sends one message when it is hit and is then switched off; the other levels stay on watch. When the last level has fired the alert moves to *Triggered* and waits there until the user taps **Watch again**, which puts every level back on watch. Alerts saved before this (a single `level` and `condition` on the document) are read as one-level alerts.

**Fractal alerts.** The New alert form has a second kind, *Fractals*, where no price is entered: the levels are the instrument's unmitigated fractals. A fractal is three completed candles, the same rule as algo-nisha: a fractal high when the middle candle's high is >= both neighbours, a fractal low when its low is <= both. It is unmitigated until a later candle trades beyond it (`app/fractals.py`).

- Timeframes: 3, 5, 10, 15 and 30 min (default 30) look back 10 sessions, 1 hour looks back 20 and Daily 120.
- Per alert you choose what to hear about: the fractal is *taken* (any trade beyond it, caught on 1-minute data), *swept* (a candle of the timeframe trades beyond it but closes back), its *sweep holds* (a sweep, and the next candle also closes back, so neither candle closes beyond the level), or its *break fails* (a candle closes beyond it and the next two both close back). You can also watch highs only or lows only.
- **Trigger candle.** *Swept* and *break fails* wait for a candle to close. By default that is a candle of the fractal timeframe, but each alert can name a shorter one (1, 3, 5, 10, 15, 30 min or 1 hour) as long as it is shorter than the fractal candles and divides into them: 30-minute fractals can be judged on 5-minute closes, so a sweep is reported five minutes after it happens rather than at the end of the half hour.
- **Minimum gap.** A fractal only counts if enough candles of its own timeframe sit between its middle candle and the candle that takes it: 5 by default, selectable per alert (none, 2, 3, 4, 5, 6, 8 or 10). The fractal's third candle is one of them. A fractal taken sooner is retired without a message, and the backtest leaves it out.
- A fractal high reads as a potential sell and a fractal low as a potential buy. Every message gives the target: the nearest unmitigated fractal on the other side of price.
- A gap through a fractal (the session, or a candle, opens beyond it) sends no message but flips its role. A fractal low that price has gapped below is now overhead and acts as resistance: when price comes back up and takes it, that is a potential sell. A fractal high gapped above becomes support and reads as a potential buy. Targets follow roles: a sell aims at the nearest unmitigated support below, a buy at the nearest resistance above. Webhooks carry `role` and `flipped`.
- *Only fractals at a day's high or low* (a checkbox on the alert) reports only fractals that are the high or low of their day: an earlier day's high or low, or today's as it stood when price came back to it. The backtest follows it too.
- The levels are recalculated each time a candle of the timeframe closes. A fractal alert never moves to *Triggered*; it keeps watching until paused or removed. Each fractal is reported once per chosen outcome.
- **Backtest fractals** (the Simulate button in Fractals mode) replays the look-back period on the trigger candles. *Backtest as intraday: square off at 3:15 PM* (ticked by default; the time can be changed) closes every trade the day it was taken: at its target, its SL, or the close of the last candle before that time, scored on that price. Signals at or after that time aren't traded. Unticked, trades run until target or SL, into later days if need be. It says which candles the fractals and the triggers used, draws those candles with an arrow on every signal and RSI (14) in a panel underneath, and lists each signal with its target, its stop (SL) and how it went: *target reached*, *SL gone* (and whether the target was reached later anyway), or *still open*, with a tally at the top. It also adds up the points: earned (entry to target), lost (entry to SL) and net, with each trade's points and the running net in the table. Entry is the fractal level for *taken* and the deciding candle's close for *swept* and *break fails*; open trades and signals without a target aren't counted, and no costs are included. The SL is the high (sell) or low (buy) made by the candle that took the fractal, plus the candle before it for a failed break; if one candle trades both the SL and the target it counts as SL gone. Clicking a row jumps the chart to that signal and draws its fractal level, target and SL. Nothing is saved or sent. For daily fractals with a small trigger candle the period is capped by how far back Kite serves that candle size in one request (60 days of 1-minute, 100 days of 3 to 10-minute candles).
- Webhooks get `"event": "fractal_hit"` with `side`, `trigger` (`touch` / `reject` / `confirm` / `fail`), `signal`, `level`, `price`, `target`, `timeframe`, `trigger_timeframe` and `fractal_time`.

**Editing.** The pencil on an alert opens an edit panel: add levels, change or remove existing ones, tick *Watch again* on a level that has fired, and change the candle timeframe or message. Levels left untouched keep their state. If anything was added, changed or re-armed, the alert is re-armed from that moment, so a new level can't fire on a move that happened before it was added.

**Instruments.** Kite's public lists for NSE, BSE, NFO and BFO are loaded at start and refreshed every 4 hours, so newly listed futures and options show up in the search the same day. NSE symbols are plain (`INFY`); the others carry their exchange (`BSE:SENSEX`, `NFO:NIFTY26OCT24500CE`). The search matches every word against the symbol and a spelled-out name, so `nifty 24500 ce` or `sensex oct fut` finds contracts. An alert on a contract is paused once the contract has expired.

**The alert list.** Alerts are listed under their instrument: the symbol, last price and a chart with every level on it show once, beside the instrument's first alert, and each alert takes one row (its levels, how close it is, when it was checked, where it is sent). A line above the list sums up the watch: how many alerts on how many instruments, how many levels are on watch, the nearest one, and anything needing attention (alerts that are sent nowhere, fractal alerts waiting for their first scan). The New alert form stays folded once there are alerts. Cards show one card per instrument with its alerts stacked inside.

**Price and chart.** Picking a stock shows its last price and the day's change. **View chart** (and the chart icon beside each instrument) opens a side panel with 1D/5D/1M/6M/1Y candles, 5D first. A **Candles** menu picks the candle size (1, 3, 5, 10, 15, 30 min, 1 hour, daily, or Auto, the range's own). Opened from an alert it starts on that alert's own candle (a fractal alert's trigger candle, or a close-based price alert's timeframe; the smallest when an instrument has several), so a hit shows on the candle that decided it. Small candles over a long range show only what Kite serves in one request (60 days of 1-minute candles, 100 days of 3 to 10-minute ones), and the panel says so. From the alert list it draws every level still on watch on that instrument, named for what it does (the price alert's condition, or resistance/support for fractals), marks the last few hits inside the range with buy/sell arrows on the candles they happened in, and lists them under the chart with the nearest level above and below price. Hits come from the sent-alerts log; ones recorded before this change are read from their wording. Data comes from the user's own Kite session and is cached briefly (`app/prices.py`). The chart is drawn with TradingView's Lightweight Charts, loaded from jsDelivr on first use.

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

**Nifty OI.** A separate tab (grant *Nifty OI* in People & access). At the open and then every 15 minutes (30 is a setting), the scanner's tick takes one Kite quote request for the nearest-expiry Nifty options, 10 strikes either side of the at-the-money strike (also a setting), plus the index, and saves the snapshot (`oi_snapshots`, with a summary per day in `oi_days`). Any account that can see the tab and has Kite connected is used; the data is the same for everyone. The tab lets you pick any saved day and time and shows:

- the reading at that moment: the change in put OI against call OI since the day's first snapshot (put writing faster means support being built under price, bullish; call writing faster means a ceiling, bearish), PCR, and the strikes with the most put OI (support) and call OI (resistance);
- a chart of the day: call and put OI change since the open, with Nifty; click a point to open that moment;
- the strike table for that moment, with the change since the open.

**Capture now** takes a snapshot outside the schedule with your own Kite session, while the market is open (before the open OI is still yesterday's, so such a snapshot would spoil the day's starting point). With a Kite web session (enctoken), quotes come off Kite's live websocket, since `/oms/quote` refuses web sessions. `OI_CAPTURE=0` stops scheduled snapshots. The logic is in `app/oi.py`.

**Sentiment on Telegram.** Tick *Send to Telegram* on the Nifty OI tab (Telegram must be set up on the Notifications page) to get the reading at the open, then a message each time it changes, e.g. Neutral to Mildly bullish. Each message has Nifty and its move, call and put OI change, PCR, support and resistance, and a link to that moment on the tab.

Every other alert (price levels and fractals) also ends with the latest reading, e.g. `Nifty OI: Mildly bullish · PCR 1.12 · support 24,400, resistance 24,700 (10:45 AM)`, and webhooks get it as `nifty_oi`. This is only for people who can see the Nifty OI tab, and only once the day's first snapshot is in.

**Fractal bias.** A separate tab (grant *Fractal bias* in People & access). Each time a 5-minute candle closes (9:20 to 3:30), every Nifty 50 stock is recounted for the day: fractals on 30-minute candles over 10 sessions, swept or broken-and-failed on today's 5-minute closes (15-minute is a setting; sweep holds and break fails by default, with at least 5 candles between fractal and sweep; the super admin can change all three). A swept low is a potential buy, a swept high a potential sell. The candles that make a signal must all be in one session: a sweep on one day's last candle that the next day's gap "holds" is not a signal (the same rule applies to fractal price alerts and their backtests). Each signal is followed to its stop (the extreme of the candles that made it) and its target (the next fractal the other way): reaching the target first is a win. Trading through the stop only counts as a stop-out, which leans the bias the other way, if price is still closing beyond the fractal a set number of candles later (3 by default, counting the candle that broke the stop; a super-admin setting). Back on the near side by then, the signal keeps its direction and the page shows *stop hit* while it is being watched. Signals themselves still go out the moment a sweep holds or a break fails. Buys holding and sells stopped out lean bullish, sells holding and buys stopped out bearish; the reading is the balance of the two, with the same labels as Nifty OI (fewer than 3 signals reads Neutral). Counts are saved (`bias_snapshots`; `bias_days` keeps each day's list and every count's totals, so the page reads only the count shown and the one before it). The tab shows any saved moment, the day's bullish and bearish counts on a chart, and every signal with what became of it (list or cards), new and changed ones highlighted. *Only at day high / day low* above the signals (per person) narrows the table, cards and that person's signal messages to fractals that are a day's high or low: an earlier day's high or low, or today's as it stood when price came back to it (the bias reading still counts every signal). Click a stock for its chart (5-minute candles to start; any candle size and range) with the signal's fractal, stop and target drawn and the day's signals on that stock marked. **Notifications** on the tab, per person, to Telegram and/or their own webhooks (up to 5 URLs, optional extra JSON passed as `payload`, with a test button): *bias changes*, looked at every 15 minutes (9:30, 9:45, …, and the close) so the label doesn't flicker, the first reading of the day and each change after it; and *every new signal*, at each 5-minute check, the signals that weren't in the previous count (gap flips, where price gapped through a fractal and it now plays the opposite role, still count toward the bias and show on the page but aren't sent). Webhooks get JSON with `event` (`fractal_bias_signals` with `signals`, or `fractal_bias_change` with `label` and `was`), the current `bias`, a `link` and your `payload`. The Fractal bias tab shows the Nifty OI reading at the same moment along the top, the Nifty OI tab shows the fractal bias, and bias-change and OI Telegram messages carry the other reading (for those who can see both tabs). The Nifty 50 list comes from NSE once a day (a built-in copy if NSE won't answer). Earlier sessions' candles are fetched once a day per stock; each count then asks Kite for today's candles only, about 50 small requests (~20 s), on its own job so alert scans aren't held up; `BIAS_CAPTURE=0` stops it. Saved counts keep the rules they were made with; after a rule change the super admin can **Recount this day** on the tab, which rebuilds every count of a chosen day from Kite's candles with the current rules and settings (about a minute; nothing is sent). The logic is in `app/bias.py`.

**MA crossover.** A separate tab (grant *MA crossover* in People & access) with **live scanners** and a **backtest**, sharing one set of rules (`app/crossover.py`):

- Two moving averages, each an EMA or SMA with its own period (fast shorter than slow). Fast above slow is an uptrend, where buys are looked for; below, sells. The trend is read from where the averages stand, so a crossover from before the candles start still counts.
- A *pullback (trigger) candle* is a red candle in an uptrend (green in a downtrend), other than the crossover candle itself. Optionally it must touch an average: its low at or below the fast or the slow MA for a buy (its high at or above, for a sell). Each newer pullback candle replaces the previous one as the trigger. A later candle trading above its high (below its low, for a sell) is the entry; if that candle opened beyond the level, the entry came on a *gap*. The opposite crossover cancels a setup that hasn't been entered. **Only the first entry after each crossover** (ticked by default, backtests and scanners alike): once a crossover has given its entry, nothing more is looked for until the next crossover, however that trade ends (a gap entry the backtest skips still uses it up). This needs the crossover itself among the candles read, so a trend already under way when they start gives no setups; live scanners read five times the candles the averages need (still one request per stock) so recent crossovers are seen. With it, **…and a second entry if the first is stopped out** (unticked by default) allows one more entry from the next pullback when the first trade ended at its SL (the trigger candle's, or the slow MA when that is the SL) with nothing booked, while the crossover still stands; never a third. The backtest decides this with your exits; a live scanner follows the first entry from the candles with its own SL and target, and sets nothing up while that trade is still running. Unticked, every later pullback in the same trend sets up another entry. *Buys only*, *Sells only* or both.
- **4 hour candles** aren't served by Kite, so they are built from its hourly ones, counted from the open as TradingView draws NSE: 9:15 to 1:15 PM, then 1:15 PM to the close. A year of them is two hourly requests per stock.
- **Stocks:** one stock or your own list (type to search NSE stocks and indices, pick with Enter or a click, and each becomes a removable pill; Backspace removes the last, and pasting a comma- or line-separated list adds them all; up to 500), or Nifty 50, 100, 200, 500 or the F&O stocks. The Nifty lists come from NSE's index files once a day (`app/universe.py`; the last list fetched, or the built-in copy in `app/data/indices.json`, if NSE won't answer). F&O stocks are every stock with futures in Kite's NFO list.

**Backtest** (3, 5, 10, 15, 30 min, 1 hour, 4 hour or daily candles, any date range; a year back by default on daily). It runs in the background with a progress bar (one run per person; cancel any time), because Kite serves about 3 candle requests a second: the Nifty 500 on daily candles takes about 3 minutes. A run is refused if it would need more than 1,500 requests. Each stock's candles are fetched with enough earlier ones for the averages to settle (`app/ma_backtest.py`). Exits:

- **SL**: the trigger candle's low (high, for a sell), or the **slow MA**, which moves with the MA and closes the trade at the close of a candle beyond it. 1R is entry to SL (to the MA's value at entry, for the MA stop; an entry already on the wrong side of the MA isn't taken).
- **Target** at a risk : reward multiple, booking a share of the position there (100% for a plain target, 0% for none). What's left runs on a **trail**: previous candle's low (high), the fast MA (exit on a close beyond it), or a step trail that lifts the SL 1R for every 1R gained. **Breakeven at 1R** moves the SL to entry once price has gone 1R your way, and only then starts the trail. **Full exit on a close beyond the slow MA** closes everything at that close.
- **Gap entries** fill at the open, at the trigger level, or are skipped. Intraday candles can **square off** at a set time (3:15 PM by default): the last candle before it closes what's left, and later entries aren't taken.
- One position per stock at a time. The SL is checked before the target, so a candle that trades both counts as the SL; on the entry candle, trading beyond the SL counts as stopped (a replay can't tell if it came before the entry), while the target counts only on a close beyond it. Gaps through either fill at the open. No costs or slippage.

Results: win rate, net R and summed %, expectancy (average R), average win and loss, profit factor and maximum drawdown (in R, trades taken in the order they closed), and with an optional ₹ risk per trade, the same in rupees. A chart of the running total, a per-stock table, the trades (latest 500; the **CSV** has all), and clicking a trade draws its stock's candles with both averages, every trade marked and the trade's entry, SL and target. The latest run stays in memory until the app restarts; nothing is saved.

**Live scanners** (up to 5 per person, on 5, 10, 15, 30 min, 1 hour, 4 hour or daily candles). Each sends to its own channels and webhooks:

- *Setup*: when a trigger candle closes, e.g. `INFY buy above ₹1,534.50 · SL ₹1,512.00 · target ₹1,579.00 (1:2)`, so a stop order can go in. Several stocks at one candle close come as one message. Daily scanners read the day's candles just after the close.
- *Entry*: when the level trades. Every 30 seconds while the market is open, one quote request covers every stock waiting on a level, so this comes within about a minute. Anything the quotes missed (a quick spike) is caught from the candles at the next candle close; either way it is sent once. An entry on a gap (the session opening beyond a level set on an earlier day) is sent only if the scanner asks for gap entries.
- Each stock's candles are read again at every candle close (earlier sessions once a day, then today's only), one Kite request per stock, so a scanner's list is capped by candle size: 5 min up to 100 stocks, 10 min up to 200, 15 and 30 min up to 500, 1 hour and daily any list. Both jobs run on their own scheduler jobs, so price alerts and Fractal bias aren't held up.
- Setups waiting on a level are saved on the scanner (`ma_scanners`), so a restart picks up where it was. A new or resumed scanner reports nothing that happened before it was switched on. **Test** sends a sample message; **Backtest this** opens the Backtest tab with the scanner's settings.
- Webhooks get `"event": "ma_setups"` or `"ma_entries"` with `scanner`, `timeframe`, `averages`, `signals` (each with `symbol`, `side`, `level`, `stop`, `target`, `price`, `gap`, `trigger_at`) and your `payload`.

**Channels per alert.** Each alert has its own set of channels. New alerts start with every channel you've set up, and you can switch channels on or off for each alert from the list.

**When scanning happens.** Only on trading days, from the open until two minutes after the close (so the day's last candle can be checked once it has finished). Trading days are Monday to Friday minus the market holidays listed on the Market hours page. That list starts with NSE's 2026 holidays; the super admin can add or remove days, or pull NSE's current list with **Update from NSE**. Outside those times the scheduler still ticks but returns straight away: no Kite calls, no alerts, no login notices.

**Kite sessions.** An expired session isn't treated as an error. At market open, the scanner checks each user who has active alerts once. If Kite isn't connected or the session has died, it tells that user on all their ready channels (once a day) and skips them until they log in again. Scanning then resumes straight away.

**Speed.** Firestore reads are cached in memory (`app/store.py`), and the app's own writes clear the affected entries immediately. Every request logs a line such as `GET /alerts 200 4ms (db 0 calls, 0ms)`, and the same figures appear in the browser's Network tab as a `Server-Timing` header.

**Motion.** Kept to what explains a change. The alert list is replaced every 30 seconds, so `app.js` compares each alert before and after the swap: a newly hit level sweeps its row in marigold (the one authored moment), a changed price flashes green or red, the price dot glides to its new place, and the Triggered count pulses when an alert moves there. Changing tab or sort uses the browser's View Transitions so rows glide to their new positions (browsers without it just swap). With *reduce motion* set, nothing travels or pulses but fades and colour changes remain.

**Staying alive.** A background thread pings the app's public URL and restarts the scheduler if it has stopped.

**Ownership.** Each user has their own alerts, Kite connection and notification contacts. The super admin is seeded from `SUPERADMIN_USERNAME` / `SUPERADMIN_PASSWORD` on each start. It creates users and picks which areas each one can use.

**Adding an area** (e.g. trading): add a `Module` in `app/modules.py`, a router guarded by `require("<key>")`, and its templates. It then appears in the navigation and in the permissions form.

## Known limits

- Special sessions outside normal hours (Muhurat trading, weekend sessions) aren't scanned.
- Last prices shown on the alerts page live in memory and appear after the first scan following a restart.
