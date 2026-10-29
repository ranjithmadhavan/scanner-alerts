"""Indicator scanners: one page per strategy (MA pullback at /ma, ADX trend at /adx), each its own permission, with
the same three tabs: saved live scanners (and the signals they sent), Scan now (a day's close), and backtests.
Everything that differs between strategies is in STRATEGY_FORMS (defaults, rule fields, exit options)."""

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app import brokers, crossover, ma_backtest, ma_scanner, ma_screen, notify, universe, webhooks
from app.adx import AdxRules
from app.crossover import Exits, Rules
from app.kite import KiteAuthError, KiteError, instruments, refresh_instruments, search_instruments
from app.market import is_market_open, load_settings, now_ist, parse_hhmm
from app.security import require
from app.store import new_id, store
from app.strategies import STRATEGIES, Strategy
from app.web import _company, fail, render, toast

MAX_SYMBOLS = 500
# How far back a backtest starts by default, per candle size.
DEFAULT_DAYS = {"3m": 30, "5m": 60, "10m": 90, "15m": 120, "30m": 180, "1h": 365, "4h": 365, "1d": 365}
UNIVERSE_DEFAULTS = {"universe_kind": "list", "universe_list": "nifty50", "symbols": ""}


# ---- what differs between strategies ------------------------------------------------------------

def _int(raw, low: int, high: int, what: str) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{what} should be a whole number.") from None
    if not low <= n <= high:
        raise ValueError(f"{what} should be between {low} and {high}.")
    return n


def _num(raw, low: float, high: float, what: str) -> float:
    try:
        n = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{what} should be a number.") from None
    if not low <= n <= high:
        raise ValueError(f"{what} should be between {low:g} and {high:g}.")
    return n


def _sides(form) -> str:
    sides = form.get("sides", "both")
    if sides not in crossover.SIDES:
        raise ValueError("Pick which side to trade.")
    return sides


def _ma_rules(form) -> Rules:
    fast_kind, slow_kind = form.get("fast_kind"), form.get("slow_kind")
    if fast_kind not in crossover.MA_KINDS or slow_kind not in crossover.MA_KINDS:
        raise ValueError("Pick EMA or SMA for each moving average.")
    fast_len = _int(form.get("fast_len", ""), 1, 400, "The fast period")
    slow_len = _int(form.get("slow_len", ""), 2, 400, "The slow period")
    if fast_len >= slow_len:
        raise ValueError("The fast moving average needs a shorter period than the slow one.")
    touch = form.get("touch", "")
    if touch not in crossover.TOUCH:
        raise ValueError("Pick the pullback rule.")
    once = bool(form.get("once"))
    return Rules(fast_kind, fast_len, slow_kind, slow_len, touch, _sides(form), once, once and bool(form.get("second")))


def _adx_rules(form) -> AdxRules:
    di_min = _num(form.get("di_min", ""), 1, 100, "The DI level a trend needs")
    di_max = _num(form.get("di_max", ""), 0, 100, "The DI level the other side must stay under")
    if di_max >= di_min:
        raise ValueError("The other DI's ceiling has to be under the trending DI's level (e.g. 20 and 25).")
    return AdxRules(_int(form.get("adx_len", ""), 2, 100, "The ADX period"), _int(form.get("atr_len", ""), 1, 100, "The ATR period"),
                    _num(form.get("adx_min", ""), 1, 100, "The ADX level"), di_min, di_max,
                    _num(form.get("atr_mult", ""), 0, 10, "The ATR multiple for the SL"), _sides(form))


@dataclass(frozen=True)
class StrategyForm:
    setup: dict  # the scanner form's rule defaults
    backtest: dict  # the backtest's and Scan now's
    exits: dict
    read_rules: Callable
    stops: dict  # SL choices
    trails: dict
    own_exits: dict  # the strategy's own exit switches: {form name: (label, help)}


MA_SETUP = {"tf": "15m", "fast_kind": "ema", "fast_len": 9, "slow_kind": "ema", "slow_len": 21, "touch": "", "sides": "both",
            "once": True, "second": True}  # a second entry after a stop-out, ticked to start with
STRATEGY_FORMS = {
    # The MA backtest starts positional: daily 10/20 EMA, buys only, pullbacks that touch the fast MA, half booked at
    # 1:2, breakeven at 1R, the rest out on a close beyond the slow MA, ₹1,000 risked per trade.
    "ma": StrategyForm(
        MA_SETUP, {**MA_SETUP, "tf": "1d", "fast_len": 10, "slow_len": 20, "touch": "fast", "sides": "buy"},
        {"stop": "candle", "rr": 2, "book": 50, "trail": "", "breakeven": True, "ma_exit": True, "fill": "open",
         "square_off": False, "square_off_time": "15:15", "risk": 1000},
        _ma_rules, crossover.STOPS, crossover.TRAILS,
        {"ma_exit": ("Full exit on a close beyond the slow MA", "A candle closing below it (above, for a sell) closes everything at that close.")}),
    # ADX trend, as in the video: daily, ADX/DMI and ATR 14, ADX over 25, the trending DI over 25 and the other under
    # 20, SL one ATR past the signal candle; half at 1:2, the rest held until the trend ends or the monthly expiry.
    "adx": StrategyForm(
        {"tf": "1d", "adx_len": 14, "atr_len": 14, "adx_min": 25, "di_min": 25, "di_max": 20, "atr_mult": 1, "sides": "both"},
        {"tf": "1d", "adx_len": 14, "atr_len": 14, "adx_min": 25, "di_min": 25, "di_max": 20, "atr_mult": 1, "sides": "both"},
        {"stop": "candle", "rr": 2, "book": 50, "trail": "", "breakeven": False, "trend_exit": True, "expiry_exit": True,
         "fill": "open", "square_off": False, "square_off_time": "15:15", "risk": 1000},
        _adx_rules, {"candle": "Signal candle low / high, one ATR further"},
        {k: v for k, v in crossover.TRAILS.items() if k != "fast_ma"},
        {"trend_exit": ("Exit when the trend ends", "ADX closes back under its level, or the DIs cross against the trade: what's left closes at that close."),
         "expiry_exit": ("Exit at the monthly expiry", "What's left closes at the close of the month's F&O expiry (last Tuesday; last Thursday before Sep 2025), as an option held to expiry would.")}),
}


def _options(strat: Strategy) -> dict:
    f = STRATEGY_FORMS[strat.key]
    return {"strategy": strat, "universes": universe.UNIVERSES, "ma_kinds": crossover.MA_KINDS, "touches": crossover.TOUCH,
            "sides": crossover.SIDES, "stops": f.stops, "trails": f.trails, "own_exits": f.own_exits, "fills": crossover.FILLS,
            "confirms": ma_scanner.CONFIRMS, "tf_label": ma_backtest.label, "live_limit": ma_scanner.LIVE_LIMIT, "default_days": DEFAULT_DAYS,
            "base": f"/{strat.key}"}


# ---- page --------------------------------------------------------------------------------------

def _mine(strat: Strategy, username: str) -> list[dict]:
    return sorted((s for s in store.list(ma_scanner.COL, user=username) if s.get("strategy", "ma") == strat.key),
                  key=lambda s: s["created_at"], reverse=True)


def _scanner_view(sc: dict) -> dict:
    state = sc.get("state") or {}
    rules = ma_scanner.rules_of(sc)
    return {**sc, "describe": ma_scanner.describe(sc), "label": rules.label, "summary": rules.summary, "waiting": sorted(state.items()),
            "sends_to": [notify.CHANNELS[c] for c in sc.get("channels", [])] + (["webhooks"] if sc.get("webhooks") else [])}


def _setup_of(sc: dict) -> dict:
    """A saved scanner's stocks, candle size and rules, as the setup fields take them."""
    spec = sc["spec"]
    return {"universe_kind": "list" if spec.get("list") else "symbols", "universe_list": spec.get("list") or "nifty50",
            "symbols": ", ".join(spec.get("symbols") or []), "tf": sc["tf"], **asdict(ma_scanner.rules_of(sc))}


NEW_SCANNER = {"stop": "candle", "rr": 2, "name": "", "gap_alerts": True, "confirm": "", "channels": None, "webhooks": [], "webhook_payload": ""}


def _ctx(strat: Strategy, user: dict, tab: str, from_id: str = "", edit_id: str = "") -> dict:
    username = user["username"]
    f = STRATEGY_FORMS[strat.key]
    contacts = notify.load_contacts(username)
    scanners = _mine(strat, username)
    setup, bt_setup, exits = {**UNIVERSE_DEFAULTS, **f.setup}, {**UNIVERSE_DEFAULTS, **f.backtest}, dict(f.exits)
    # "Backtest this": the form starts from a saved scanner's settings.
    if from_id and (sc := next((s for s in scanners if s["id"] == from_id), None)):
        bt_setup.update(_setup_of(sc))
        exits.update(stop=sc.get("stop", "candle"), rr=sc.get("rr") or 2)
        tab = "backtest"
    # "Edit": the scanner form holds a saved scanner's settings and saves back to it. channels None: every ready one.
    editing, form_sc = None, NEW_SCANNER
    if edit_id and (sc := next((s for s in scanners if s["id"] == edit_id), None)):
        editing, form_sc, tab = sc, {**NEW_SCANNER, **sc}, "scanners"
        setup.update(_setup_of(sc))
    now = now_ist()
    today = now.date()
    run = ma_backtest.current(username, strat.key)
    screen = ma_screen.current(username, strat.key)
    return {
        "tab": tab if tab in ("scanners", "screen", "backtest") else ("scanners" if scanners else "backtest"),
        "screen": screen, **(_screen_ctx(screen) if screen and screen.status != "running" else {}),
        "screen_day": ma_screen.last_session(load_settings(), now).isoformat(),
        **_feed_ctx(strat, username, ""),
        "scanners": [_scanner_view(s) for s in scanners], "max_scanners": ma_scanner.MAX_SCANNERS,
        "setup": setup, "editing": editing, "form_sc": form_sc, "bt_setup": bt_setup, "exits": exits, "today": today.isoformat(),
        "start_default": (today - timedelta(days=DEFAULT_DAYS[bt_setup["tf"]])).isoformat(),
        "live_tfs": ma_scanner.TIMEFRAMES, "backtest_tfs": ma_backtest.TIMEFRAMES,
        "channels": [{"key": k, "label": v, "ready": notify.sender_ready(k) and notify.recipient_ready(k, contacts)}
                     for k, v in notify.CHANNELS.items()],
        "broker_ok": brokers.load(username).get("status") == "connected",
        "is_admin": user.get("role") == "superadmin",
        "run": run, **(_run_ctx(run) if run and run.status != "running" else {}),
        **_options(strat),
    }


REFRESH_EVERY = timedelta(minutes=10)  # Kite and NSE both turn away repeated downloads
_refreshed_at = None


def refresh_lists(user: dict):
    """Download Kite's instrument lists and NSE's Nifty lists now (they refresh on their own once a day)."""
    global _refreshed_at
    if user.get("role") != "superadmin":
        return fail("Only the super admin can refresh the stock lists.")
    now = now_ist()
    if _refreshed_at and now - _refreshed_at < REFRESH_EVERY:
        return fail(f"The lists were refreshed at {_refreshed_at:%-I:%M %p}. Try again after {(_refreshed_at + REFRESH_EVERY):%-I:%M %p}.")
    _refreshed_at = now
    try:
        counts = refresh_instruments()
        kite_part = f"Kite: {counts['NSE']:,} NSE, {counts['NFO']:,} NFO"
    except KiteError as e:
        kite_part = f"Kite: {e}"
    nse = universe.refresh_all(now.date())
    return toast(HTMLResponse("", headers={"HX-Reswap": "none"}),
                 f"{kite_part}. " + "; ".join(f"{k}: {v}" for k, v in nse.items()))


def symbols(q: str):
    """Suggestions for the stock picker: NSE stocks and indices matching what has been typed."""
    if not q.strip():
        return JSONResponse([])
    try:
        found = search_instruments("NSE:" + q.strip(), limit=8)
    except KiteError as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    return JSONResponse([{"symbol": i.symbol, "name": _company(i.name), "index": i.is_index} for i in found])


# ---- shared form reading ------------------------------------------------------------------------

def _read_setup(strat: Strategy, form, timeframes: list[str]):
    """(stocks, candle size, rules) from the form. Raises ValueError with something to show."""
    if form.get("universe_kind") == "symbols":
        picked = universe.parse_symbols(form.get("symbols", ""))
        if not picked:
            raise ValueError("Type a stock, or several separated by commas.")
        if len(picked) > MAX_SYMBOLS:
            raise ValueError(f"A list can have up to {MAX_SYMBOLS} stocks.")
        known = instruments()
        unknown = [s for s in picked if s not in known]
        if unknown:
            raise ValueError(f"Not on Kite's NSE list: {', '.join(unknown[:5])}{' …' if len(unknown) > 5 else ''}")
        spec = {"symbols": picked}
    else:
        key = form.get("universe_list", "")
        if key not in universe.UNIVERSES:
            raise ValueError("Pick a list of stocks.")
        spec = {"list": key}
    tf = form.get("tf", "")
    if tf not in timeframes:
        raise ValueError("Pick a candle size.")
    return spec, tf, STRATEGY_FORMS[strat.key].read_rules(form)


def _rr(raw: str) -> float:
    try:
        rr = float(raw or 0)
    except ValueError:
        raise ValueError("The risk : reward should be a number, like 2 for 1:2.") from None
    if not 0 <= rr <= 20:
        raise ValueError("The risk : reward should be between 0 and 20.")
    return rr


def _stop(strat: Strategy, form) -> str:
    stop = form.get("stop", "candle")
    if stop not in STRATEGY_FORMS[strat.key].stops:
        raise ValueError("Pick the SL.")
    return stop


# ---- backtest -----------------------------------------------------------------------------

def _read_backtest(strat: Strategy, form) -> ma_backtest.Params:
    f = STRATEGY_FORMS[strat.key]
    spec, tf, rules = _read_setup(strat, form, ma_backtest.TIMEFRAMES)
    today = now_ist().date()
    try:
        start, end = date.fromisoformat(form.get("start", "")), date.fromisoformat(form.get("end", "") or today.isoformat())
    except ValueError:
        raise ValueError("Give the backtest's first and last day.") from None
    if not start <= end <= today:
        raise ValueError("The first day must come before the last, and the last can't be in the future.")
    stop, trail, fill = _stop(strat, form), form.get("trail", ""), form.get("fill", "open")
    if trail not in f.trails or fill not in crossover.FILLS:
        raise ValueError("Pick the trail and gap options.")
    book = _int(form.get("book", "100"), 0, 100, "The share booked at the target")
    square_off = None
    if form.get("square_off") and tf != "1d":
        try:
            square_off = parse_hhmm(form.get("square_off_time", ""))
        except ValueError:
            raise ValueError("Give the square-off time as hours and minutes, like 15:15.") from None
    risk = None
    if (raw := form.get("risk", "").strip()):
        try:
            risk = float(raw)
        except ValueError:
            raise ValueError("The ₹ risk per trade should be a number.") from None
        if risk <= 0:
            raise ValueError("The ₹ risk per trade should be more than 0.")
    rr = _rr(form.get("rr", "2"))
    if book and not rr:
        raise ValueError("Set a risk : reward for the target, or book 0% there.")
    own = {k: bool(form.get(k)) for k in f.own_exits}
    exits = Exits(stop, rr, book, trail, bool(form.get("breakeven")), own.get("ma_exit", False), fill, square_off,
                  own.get("trend_exit", False), own.get("expiry_exit", False))
    return ma_backtest.Params(spec, tf, start, end, rules, exits, risk, strat.key)


async def backtest(strat: Strategy, request: Request, user: dict):
    form = await request.form()
    try:
        p = _read_backtest(strat, form)
        n = len(universe.resolve(p.spec, now_ist().date()))
        needed = ma_backtest.requests_needed(n, p.tf, p.start, p.end, p.rules)
        if needed > ma_backtest.MAX_REQUESTS:
            raise ValueError(f"That needs about {needed:,} Kite requests ({n} stocks); the limit is {ma_backtest.MAX_REQUESTS:,} "
                             f"(about {ma_backtest.minutes_for(ma_backtest.MAX_REQUESTS)} minutes). Shorten the period, use a longer candle, "
                             f"or a shorter list.")
        if brokers.load(user["username"]).get("status") != "connected":
            raise ValueError("Kite isn't connected. Log in on the Broker page first.")
        run = ma_backtest.start(user["username"], p)
    except ValueError as e:
        return fail(str(e))
    return toast(render(request, "partials/ma_progress.html", {"run": run, **_options(strat)}),
                 f"Backtest started on {run.total} stock{'s' if run.total != 1 else ''}")


def _run_ctx(run: ma_backtest.Run) -> dict:
    p = run.params
    rep = ma_backtest.report(run)
    curve, seen = [], {}
    for pt in rep["curve"]:  # the chart wants one point per time
        seen[ma_backtest.chart_time(run, pt["time"])] = pt["r"]
    curve = [{"time": t, "value": v} for t, v in seen.items()]
    trades = [{"n": n, "symbol": t["symbol"], "side": t["side"], "price": t["price"], "stop": t["stop"], "target": t["target"],
               "entry": ma_backtest.chart_time(run, t["entry_at"]),
               "exits": [{"time": ma_backtest.chart_time(run, f["at"]), "price": f["price"], "reason": f["reason"]} for f in t["exits"]]}
              for n, t in enumerate(rep["shown"])]
    return {"rep": rep, "p": p, "spec_label": universe.describe(p.spec), "tf_name": ma_backtest.label(p.tf),
            "chart_json": {"curve": curve, "trades": trades, "daily": p.tf == "1d"},
            "exit_names": crossover.EXIT_NAMES}


def status(strat: Strategy, request: Request, user: dict):
    run = ma_backtest.current(user["username"], strat.key)
    if not run:
        return HTMLResponse("")
    if run.status == "running":
        return render(request, "partials/ma_progress.html", {"run": run, **_options(strat)})
    return render(request, "partials/ma_result.html", {"run": run, **_run_ctx(run), **_options(strat)})


def csv_download(strat: Strategy, user: dict):
    run = ma_backtest.current(user["username"], strat.key)
    if not run or run.status == "running":
        return Response("No finished backtest", status_code=404)
    name = f"{strat.key}-backtest-{run.params.tf}-{run.params.start}-{run.params.end}.csv"
    return Response(ma_backtest.to_csv(run), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _chart(job, symbol: str, build):
    if not job or job.status == "running" or symbol not in job.symbols:
        return JSONResponse({"error": "That isn't available any more. Run it again."}, status_code=404)
    try:
        return JSONResponse(build(job, symbol))
    except KiteAuthError:
        return JSONResponse({"error": "Kite isn't connected or the session has expired."}, status_code=400)
    except KiteError as e:
        return JSONResponse({"error": f"Couldn't get prices from Kite: {e}"}, status_code=400)


# ---- scan now (end of day) ------------------------------------------------------------------

def _screen_ctx(screen: ma_screen.Screen) -> dict:
    p = screen.params
    return {"sp": p, "screen_spec": universe.describe(p.spec), "screen_tf": ma_backtest.label(p.tf),
            "groups": {k: screen.group(k) for k in ma_screen.GROUPS}, "group_names": ma_screen.group_names(p.strategy)}


def _screen_partial(strat: Strategy, screen: ma_screen.Screen) -> dict:
    return {"screen": screen, **_options(strat), **(_screen_ctx(screen) if screen.status != "running" else {})}


async def screen_start(strat: Strategy, request: Request, user: dict):
    form = await request.form()
    try:
        spec, tf, rules = _read_setup(strat, form, ma_backtest.TIMEFRAMES)
        try:
            day = date.fromisoformat(form.get("day", ""))
        except ValueError:
            raise ValueError("Pick the day to scan as of.") from None
        if day > now_ist().date():
            raise ValueError("The day can't be in the future.")
        stop = _stop(strat, form)
        if brokers.load(user["username"]).get("status") != "connected":
            raise ValueError("Kite isn't connected. Log in on the Broker page first.")
        screen = ma_screen.start(user["username"], ma_screen.Params(spec, tf, day, rules, stop, _rr(form.get("rr", "2")), strat.key))
    except ValueError as e:
        return fail(str(e))
    return toast(render(request, "partials/ma_screen.html", _screen_partial(strat, screen)),
                 f"Scanning {screen.total} stock{'s' if screen.total != 1 else ''} as of {day:%-d %b}")


# ---- the signals feed --------------------------------------------------------------------------

def _feed_ctx(strat: Strategy, username: str, day: str) -> dict:
    now = now_ist()
    days = ma_scanner.feed_days(username, now.date())
    try:
        day = date.fromisoformat(day).isoformat() if day else days[0]
    except ValueError:
        day = days[0]
    if day not in days:  # an older day asked for by date
        days.append(day)
    return {"feed_day": day, "feed_days": days, "feed": ma_scanner.feed(username, day, strat.key),
            "feed_live": day == days[0] and is_market_open(load_settings(), now)}


# ---- live scanners --------------------------------------------------------------------------

def _list(strat: Strategy, request: Request, user: dict):
    return render(request, "partials/ma_scanners.html", _ctx(strat, user, "scanners"))


def _read_scanner(strat: Strategy, form) -> dict:
    """A scanner's settings from its form, as saved. Raises ValueError with something to show."""
    spec, tf, rules = _read_setup(strat, form, ma_scanner.TIMEFRAMES)
    if problem := ma_scanner.limit_problem(len(universe.resolve(spec, now_ist().date())), tf):
        raise ValueError(problem)
    stop = _stop(strat, form)
    rr = _rr(form.get("rr", "2"))
    hooks, error = webhooks.parse_urls(form.get("webhooks", ""))
    if error:
        raise ValueError(error)
    payload, error = webhooks.parse_payload(form.get("webhook_payload", ""))
    if error:
        raise ValueError(error)
    channels = form.getlist("channels")
    name = (form.get("name") or "").strip()[:60] or f"{universe.describe(spec)} {rules.label}"
    return {"name": name, "spec": spec, "tf": tf, "rules": asdict(rules), "stop": stop, "rr": rr,
            "gap_alerts": bool(form.get("gap_alerts")), "confirm": form.get("confirm") if form.get("confirm") in ma_scanner.CONFIRMS else "",
            "channels": [c for c in notify.CHANNELS if c in channels],
            "webhooks": hooks, "webhook_payload": payload}


async def create(strat: Strategy, request: Request, user: dict):
    form = await request.form()
    username = user["username"]
    try:
        if len(_mine(strat, username)) >= ma_scanner.MAX_SCANNERS:
            raise ValueError(f"You can keep up to {ma_scanner.MAX_SCANNERS} {strat.name} scanners. Remove one first.")
        settings = _read_scanner(strat, form)
    except ValueError as e:
        return fail(str(e))
    now = now_ist()
    sc = {"id": new_id(), "user": username, "strategy": strat.key, **settings,
          "status": "active", "created_at": now.isoformat(), "armed_at": now.isoformat(), "state": {}, "fired": []}
    store.put(ma_scanner.COL, sc["id"], sc)
    msg = "Scanner saved. It reads the candles at the next candle close." + ("" if sc["channels"] or sc["webhooks"] else " It has nowhere to send yet.")
    return toast(_list(strat, request, user), msg)


async def update(strat: Strategy, request: Request, scanner_id: str, user: dict):
    if not (sc := _own(scanner_id, user)):
        return fail("That scanner is gone.")
    try:
        settings = _read_scanner(strat, await request.form())
    except ValueError as e:
        return fail(str(e))
    msg = "Scanner updated."
    # Other stocks, candles or rules: what it was waiting on no longer applies, and it starts afresh, as after a pause.
    if any(settings[k] != sc.get(k) for k in ("spec", "tf", "rules")):
        settings.update(armed_at=now_ist().isoformat(), state={}, evaluated_at="")
        msg += " Its waiting setups were dropped; it reads the candles again at the next candle close."
    with ma_scanner.lock(scanner_id):  # not while the quote job is changing what it waits on
        store.update(ma_scanner.COL, scanner_id, settings)
    return toast(_list(strat, request, user), msg)


def _own(scanner_id: str, user: dict) -> dict | None:
    sc = store.get(ma_scanner.COL, scanner_id)
    return sc if sc and sc["user"] == user["username"] else None


def pause(strat: Strategy, request: Request, scanner_id: str, user: dict):
    if not (sc := _own(scanner_id, user)):
        return fail("That scanner is gone.")
    if sc["status"] == "active":
        store.update(ma_scanner.COL, scanner_id, {"status": "paused"})
        return toast(_list(strat, request, user), "Paused")
    # Back on: what happened while it was paused isn't reported.
    store.update(ma_scanner.COL, scanner_id, {"status": "active", "armed_at": now_ist().isoformat(), "state": {}, "evaluated_at": ""})
    return toast(_list(strat, request, user), "Watching again from the next candle close")


def test_message(scanner_id: str, user: dict):
    """Send a sample setup message on the scanner's channels and webhooks."""
    if not (sc := _own(scanner_id, user)):
        return fail("That scanner is gone.")
    side = "sell" if sc["rules"].get("sides") == "sell" else "buy"
    level, candle_stop = (1534.5, 1512.0) if side == "buy" else (1512.0, 1534.5)
    stop, target = ma_scanner.plan(sc, side, level, candle_stop, candle_stop)
    item = {"symbol": "SAMPLE", "side": side, "level": level, "stop": stop, "target": target, "trigger_at": now_ist().isoformat()}
    subject, body = ma_scanner.message(sc, "setup", [item], now_ist())
    results = notify.send(user["username"], sc.get("channels", []), "[Test] " + subject, body)
    if sc.get("webhooks"):
        results["webhook"] = webhooks.send(sc["webhooks"], ma_scanner.hook_body(sc, "setup", [item], now_ist(), test=True))
    sent = [notify.CHANNELS.get(c, "Webhook") for c, r in results.items() if r == "sent"]
    failed = [f"{notify.CHANNELS.get(c, 'Webhook')}: {r}" for c, r in results.items() if r != "sent"]
    if not results:
        return fail("This scanner has no channels or webhooks to send to.")
    if failed:
        return fail("Test: " + "; ".join(failed) + (f" (sent on {', '.join(sent)})" if sent else ""))
    return toast(HTMLResponse("", headers={"HX-Reswap": "none"}), f"Test sent to {', '.join(sent)}")


# ---- the routes, once per strategy ------------------------------------------------------------

def router_for(key: str) -> APIRouter:
    strat = STRATEGIES[key]
    router = APIRouter(prefix=f"/{key}")
    guard = require(key)

    @router.get("")
    def page_(request: Request, user: dict = Depends(guard), tab: str = "", backtest: str = "", edit: str = ""):
        return render(request, "indicator.html", _ctx(strat, user, tab, backtest, edit))

    @router.post("/lists/refresh")
    def refresh_(user: dict = Depends(guard)):
        return refresh_lists(user)

    @router.get("/symbols")
    def symbols_(q: str = "", user: dict = Depends(guard)):
        return symbols(q)

    @router.post("/backtest")
    async def backtest_(request: Request, user: dict = Depends(guard)):
        return await backtest(strat, request, user)

    @router.get("/backtest/status")
    def status_(request: Request, user: dict = Depends(guard)):
        return status(strat, request, user)

    @router.post("/backtest/cancel")
    def cancel_(user: dict = Depends(guard)):
        ma_backtest.cancel(user["username"], key)
        return toast(HTMLResponse("", headers={"HX-Reswap": "none"}), "Stopping after the stock in progress")

    @router.get("/backtest/csv")
    def csv_(user: dict = Depends(guard)):
        return csv_download(strat, user)

    @router.get("/backtest/chart")
    def trade_chart_(symbol: str, user: dict = Depends(guard)):
        return _chart(ma_backtest.current(user["username"], key), symbol, ma_backtest.chart)

    @router.post("/screen")
    async def screen_(request: Request, user: dict = Depends(guard)):
        return await screen_start(strat, request, user)

    @router.get("/screen/status")
    def screen_status_(request: Request, user: dict = Depends(guard)):
        screen = ma_screen.current(user["username"], key)
        return render(request, "partials/ma_screen.html", _screen_partial(strat, screen)) if screen else HTMLResponse("")

    @router.post("/screen/cancel")
    def screen_cancel_(user: dict = Depends(guard)):
        ma_screen.cancel(user["username"], key)
        return toast(HTMLResponse("", headers={"HX-Reswap": "none"}), "Stopping after the stock in progress")

    @router.get("/screen/chart")
    def screen_chart_(symbol: str, user: dict = Depends(guard)):
        return _chart(ma_screen.current(user["username"], key), symbol, ma_screen.chart)

    @router.get("/signals")
    def signals_(request: Request, user: dict = Depends(guard), day: str = ""):
        return render(request, "partials/ma_feed.html", {**_feed_ctx(strat, user["username"], day), **_options(strat)})

    @router.post("/scanners")
    async def create_(request: Request, user: dict = Depends(guard)):
        return await create(strat, request, user)

    @router.post("/scanners/{scanner_id}")
    async def update_(request: Request, scanner_id: str, user: dict = Depends(guard)):
        return await update(strat, request, scanner_id, user)

    @router.post("/scanners/{scanner_id}/pause")
    def pause_(request: Request, scanner_id: str, user: dict = Depends(guard)):
        return pause(strat, request, scanner_id, user)

    @router.post("/scanners/{scanner_id}/delete")
    def delete_(request: Request, scanner_id: str, user: dict = Depends(guard)):
        if _own(scanner_id, user):
            store.delete(ma_scanner.COL, scanner_id)
        return toast(_list(strat, request, user), "Scanner removed")

    @router.post("/scanners/{scanner_id}/test")
    def test_(scanner_id: str, user: dict = Depends(guard)):
        return test_message(scanner_id, user)

    return router


routers = [router_for(k) for k in STRATEGIES]
