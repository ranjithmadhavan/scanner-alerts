"""The chart behind one signal in the indicator scanners' feed: the stock on the scanner's own candles with its lines
(moving averages, or the ADX panel), or on 5-minute candles from the setup onwards, with the level, SL and target
drawn across and where the entry happened, or would have, marked.

The entry comes from 5-minute candles either way, so both views agree on it:
* a recorded entry is where the scanner saw it (the 5-minute candle that confirmed it, or the one it was sent in);
* for a setup, it is the first 5-minute candle after the setup candle closed that traded beyond the level (or, for a
  scanner that waits for a 5-minute close, the first that closed beyond it), within the days shown. The live scanner
  may have dropped the setup sooner (a newer pullback candle, a crossover back), so the page calls it "would have".
After an entry, the first 5-minute candle to reach the SL or the target says how it went.
"""

from datetime import date, datetime, timedelta

from app import brokers, ma_backtest, ma_scanner
from app.kite import Candle, KiteError, instruments
from app.market import IST, is_trading_day, load_settings, now_ist

FINE = "5m"
BEFORE = 120  # scanner candles shown before the setup candle
AFTER_DAYS = {"1d": 10}  # calendar days followed after the signal (daily: about a trading week); intraday: 2
_cache: dict[tuple, dict] = {}  # finished days only; a handful is plenty


def _t(at: datetime, daily: bool):
    at = at.astimezone(IST)
    return at.date().isoformat() if daily else int(at.timestamp()) + 19800


def _window_end(tf: str, sent: datetime, today: date) -> date:
    return min(sent.astimezone(IST).date() + timedelta(days=AFTER_DAYS.get(tf, 2)), today)


def _entry(item: dict, fine: list[Candle], setup_end: datetime, confirm: bool) -> tuple[int, float, str] | None:
    """(index into `fine`, price, how) of the entry, or None if price never got there."""
    side, level = item["side"], item["level"]
    buy = side == "buy"
    if item["kind"] == "entry":
        # `confirmed` is when the confirming candle closed; `at`, when the quote saw the level trade.
        at = datetime.fromisoformat(item.get("confirmed") or item["at"])
        i = next((k for k in range(len(fine) - 1, -1, -1) if (fine[k].start < at if item.get("confirmed") else fine[k].start <= at)), None)
        if i is None:
            return None
        how = "opened beyond the level (gap)" if item.get("gap") else "5-min close beyond the level" if item.get("confirmed") else "traded beyond the level"
        return i, item.get("price") or level, how
    after = [k for k, c in enumerate(fine) if c.start >= setup_end]
    if not after:
        return None
    gap = (fine[after[0]].open > level) if buy else (fine[after[0]].open < level)
    if confirm:
        c = ma_scanner.confirmation(side, level, [fine[k] for k in after], gap)
        if c is None:
            return None
        return fine.index(c), c.close, "first 5-min close beyond the level"
    for k in after:
        c = fine[k]
        if (c.high >= level) if buy else (c.low <= level):
            opened_beyond = (c.open > level) if buy else (c.open < level)
            return k, (c.open if opened_beyond else level), ("opened beyond the level" if opened_beyond else "first trade beyond the level")
    return None


def _exit(item: dict, fine: list[Candle], i: int) -> tuple[int, float, str] | None:
    """The first candle after the entry's to reach the SL or the target. Both in one candle counts as the SL."""
    buy, stop, target = item["side"] == "buy", item.get("stop"), item.get("target")
    for k in range(i + 1, len(fine)):
        c = fine[k]
        if stop is not None and ((c.low <= stop) if buy else (c.high >= stop)):
            return k, stop, "SL"
        if target is not None and ((c.high >= target) if buy else (c.low <= target)):
            return k, target, "Target"
    return None


def _stepped(overlays: list[dict], coarse: list[Candle], tf: str, fine: list[Candle], s) -> list[dict]:
    """The scanner's lines on 5-minute candles: each 5-minute candle gets the value as of the last scanner candle that
    had closed by then (what the scanner knew), so a daily average is a step a day."""
    ends = [ma_backtest.candle_end(c, tf, s) for c in coarse]
    out = []
    for o in overlays:
        pts, j = [], -1
        for c in fine:
            while j + 1 < len(coarse) and ends[j + 1] <= c.start:
                j += 1
            if j >= 0 and o["values"][j] is not None:
                pts.append({"time": _t(c.start, False), "value": round(o["values"][j], 4)})
        out.append({"label": f"{o['label']} ({ma_backtest.label(tf).lower()})", "color": o["color"], "points": pts})
    return out


def build(username: str, item: dict, rules, confirm: bool, view: str) -> dict:
    """The chart for a feed item. `rules` may be None (the scanner is gone): then there are no lines.
    Raises KiteError / KiteAuthError."""
    tf, symbol = item["tf"], item["symbol"]
    view = FINE if view == FINE else tf
    inst = instruments().get(symbol)
    if not inst:
        raise KiteError(f"{symbol} isn't on Kite's NSE list")
    s, now = load_settings(), now_ist()
    sent = datetime.fromisoformat(item["at"])
    trigger = datetime.fromisoformat(item["trigger_at"]) if item.get("trigger_at") else sent
    setup_end = ma_backtest.candle_end(Candle(trigger, 0, 0, 0, 0), tf, s)
    end = _window_end(tf, sent, now.date())
    key = (username, symbol, tf, view, item["at"], item["kind"], bool(rules), confirm)
    if key in _cache:
        return _cache[key]
    client = brokers.client_for(username)

    # The scanner's own candles: for its lines, and the view itself unless that is 5-minute.
    want = (rules.warmup() if rules else 0) + BEFORE
    start = max(ma_backtest.warmup_start(tf, trigger.astimezone(IST).date(), rules, want) if rules
                else trigger.astimezone(IST).date() - timedelta(days=200 if tf == "1d" else 10),
                end - timedelta(days=ma_backtest.max_days(tf) - 1))
    coarse = ma_backtest.fetch(client, inst.token, tf, start, end, s, now)
    series = rules.series(coarse) if rules else {"overlays": [], "panel": None}
    # 5-minute candles from the setup day on, for the entry (and the 5-minute view).
    fine = ma_backtest.fetch(client, inst.token, FINE, trigger.astimezone(IST).date(), end, s, now) if tf != FINE else coarse
    entry = _entry(item, fine, setup_end, confirm)
    exit_ = _exit(item, fine, entry[0]) if entry else None

    if view == FINE:
        daily = False
        shown = [c for c in fine if c.start.astimezone(IST).date() >= trigger.astimezone(IST).date()]  # a suffix of fine
        if tf != FINE:
            overlays = _stepped(series["overlays"], coarse, tf, shown, s)
        else:  # a 5-minute scanner: its own lines, as they are
            first = len(coarse) - len(shown)
            overlays = [{"label": o["label"], "color": o["color"],
                         "points": [{"time": _t(c.start, False), "value": round(v, 4)} for c, v in zip(shown, o["values"][first:]) if v is not None]}
                        for o in series["overlays"]]
        data = {"daily": False, "overlays": overlays, "panel": None,
                "candles": [{"time": _t(c.start, False), "open": c.open, "high": c.high, "low": c.low, "close": c.close} for c in shown]}
        # Where the setup candle closed: the last 5-minute candle inside it.
        setup_at = next((_t(c.start, False) for c in reversed(shown) if c.start < setup_end), None)
        at = lambda k: _t(fine[k].start, False)  # noqa: E731
    else:
        daily = tf == "1d"
        upto = next((k for k, c in enumerate(coarse) if c.start >= trigger), len(coarse) - 1)
        data = ma_backtest.chart_data(rules, coarse, max(0, upto - BEFORE), daily) if rules else {
            "daily": daily, "overlays": [], "panel": None,
            "candles": [{"time": _t(c.start, daily), "open": c.open, "high": c.high, "low": c.low, "close": c.close} for c in coarse[max(0, upto - BEFORE):]]}
        setup_at = _t(trigger, daily)

        def at(k: int):  # the scanner candle the 5-minute candle falls in
            j = next((n for n in range(len(coarse) - 1, -1, -1) if coarse[n].start <= fine[k].start), 0)
            return _t(coarse[j].start, daily)

    def when(k: int) -> str:
        return f"{fine[k].start.astimezone(IST):%-d %b, %-I:%M %p}"

    data.update({
        "symbol": symbol, "view": view, "tf": tf, "tf_label": ma_backtest.label(tf), "daily": daily,
        "side": item["side"], "kind": item["kind"], "level": item["level"], "stop": item.get("stop"), "target": item.get("target"),
        "setup_at": setup_at,
        "entry": entry and {"time": at(entry[0]), "price": entry[1], "how": entry[2], "when": when(entry[0])},
        "exit": exit_ and {"time": at(exit_[0]), "price": exit_[1], "reason": exit_[2], "when": when(exit_[0])},
        "until": f"{end:%-d %b}", "lines": bool(rules),
    })
    if end < now.date() or (end == now.date() and not is_trading_day(now)):
        if len(_cache) > 12:
            _cache.clear()
        _cache[key] = data
    return data
