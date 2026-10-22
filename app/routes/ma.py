"""MA crossover: saved live scanners, and backtests over a stock, a list or an index."""

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app import brokers, crossover, fractals, ma_backtest, ma_scanner, notify, universe, webhooks
from app.crossover import Exits, Rules
from app.kite import KiteAuthError, KiteError, instruments
from app.market import now_ist, parse_hhmm
from app.security import require
from app.store import new_id, store
from app.web import fail, render, toast

router = APIRouter(prefix="/ma")
guard = require("ma")

MAX_SYMBOLS = 500
# How far back a backtest starts by default, per candle size.
DEFAULT_DAYS = {"3m": 30, "5m": 60, "10m": 90, "15m": 120, "30m": 180, "1h": 365, "1d": 730}
SETUP_DEFAULTS = {"universe_kind": "list", "universe_list": "nifty50", "symbols": "", "tf": "15m", "fast_kind": "ema",
                  "fast_len": 9, "slow_kind": "ema", "slow_len": 21, "touch": "", "sides": "both"}
# The backtest starts positional: daily 10/20 EMA, buys only, pullbacks that touch the fast MA, breakeven at 1R,
# out on a close beyond the slow MA, ₹1,000 risked per trade.
BACKTEST_DEFAULTS = {**SETUP_DEFAULTS, "tf": "1d", "fast_len": 10, "slow_len": 20, "touch": "fast", "sides": "buy"}
EXIT_DEFAULTS = {"stop": "candle", "rr": 2, "book": 100, "trail": "", "breakeven": True, "ma_exit": True, "fill": "open",
                 "square_off": False, "square_off_time": "15:15", "risk": 1000}


def _options() -> dict:
    return {"universes": universe.UNIVERSES, "ma_kinds": crossover.MA_KINDS, "touches": crossover.TOUCH,
            "sides": crossover.SIDES, "stops": crossover.STOPS, "trails": crossover.TRAILS, "fills": crossover.FILLS,
            "tf_label": fractals.label, "live_limit": ma_scanner.LIVE_LIMIT, "default_days": DEFAULT_DAYS}


def _scanner_view(sc: dict) -> dict:
    state = sc.get("state") or {}
    return {**sc, "describe": ma_scanner.describe(sc), "waiting": sorted(state.items()),
            "sends_to": [notify.CHANNELS[c] for c in sc.get("channels", [])] + (["webhooks"] if sc.get("webhooks") else [])}


def _ctx(user: dict, tab: str, from_id: str = "") -> dict:
    username = user["username"]
    contacts = notify.load_contacts(username)
    scanners = sorted(store.list(ma_scanner.COL, user=username), key=lambda s: s["created_at"], reverse=True)
    bt_setup, exits = dict(BACKTEST_DEFAULTS), dict(EXIT_DEFAULTS)
    # "Backtest this": the form starts from a saved scanner's settings.
    if from_id and (sc := next((s for s in scanners if s["id"] == from_id), None)):
        spec = sc["spec"]
        bt_setup.update(universe_kind="list" if spec.get("list") else "symbols", universe_list=spec.get("list") or "nifty50",
                     symbols=", ".join(spec.get("symbols") or []), tf=sc["tf"], **sc["rules"])
        exits.update(stop=sc.get("stop", "candle"), rr=sc.get("rr") or 2)
        tab = "backtest"
    today = now_ist().date()
    run = ma_backtest.current(username)
    return {
        "tab": tab if tab in ("scanners", "backtest") else ("scanners" if scanners else "backtest"),
        "scanners": [_scanner_view(s) for s in scanners], "max_scanners": ma_scanner.MAX_SCANNERS,
        "setup": dict(SETUP_DEFAULTS), "bt_setup": bt_setup, "exits": exits, "today": today.isoformat(),
        "start_default": (today - timedelta(days=DEFAULT_DAYS[bt_setup["tf"]])).isoformat(),
        "live_tfs": ma_scanner.TIMEFRAMES, "backtest_tfs": ma_backtest.TIMEFRAMES,
        "channels": [{"key": k, "label": v, "ready": notify.sender_ready(k) and notify.recipient_ready(k, contacts)}
                     for k, v in notify.CHANNELS.items()],
        "broker_ok": brokers.load(username).get("status") == "connected",
        "run": run, **(_run_ctx(run) if run and run.status != "running" else {}),
        **_options(),
    }


@router.get("")
def page(request: Request, user: dict = Depends(guard), tab: str = "", backtest: str = ""):
    return render(request, "ma.html", _ctx(user, tab, backtest))


# ---- shared form reading ------------------------------------------------------------------------

def _int(raw: str, low: int, high: int, what: str) -> int:
    try:
        n = int(raw)
    except ValueError:
        raise ValueError(f"{what} should be a whole number.") from None
    if not low <= n <= high:
        raise ValueError(f"{what} should be between {low} and {high}.")
    return n


def _read_setup(form, timeframes: list[str]) -> tuple[dict, str, Rules]:
    """(stocks, candle size, rules) from the form. Raises ValueError with something to show."""
    if form.get("universe_kind") == "symbols":
        symbols = universe.parse_symbols(form.get("symbols", ""))
        if not symbols:
            raise ValueError("Type a stock, or several separated by commas.")
        if len(symbols) > MAX_SYMBOLS:
            raise ValueError(f"A list can have up to {MAX_SYMBOLS} stocks.")
        known = instruments()
        unknown = [s for s in symbols if s not in known]
        if unknown:
            raise ValueError(f"Not on Kite's NSE list: {', '.join(unknown[:5])}{' …' if len(unknown) > 5 else ''}")
        spec = {"symbols": symbols}
    else:
        key = form.get("universe_list", "")
        if key not in universe.UNIVERSES:
            raise ValueError("Pick a list of stocks.")
        spec = {"list": key}
    tf = form.get("tf", "")
    if tf not in timeframes:
        raise ValueError("Pick a candle size.")
    fast_kind, slow_kind = form.get("fast_kind"), form.get("slow_kind")
    if fast_kind not in crossover.MA_KINDS or slow_kind not in crossover.MA_KINDS:
        raise ValueError("Pick EMA or SMA for each moving average.")
    fast_len = _int(form.get("fast_len", ""), 1, 400, "The fast period")
    slow_len = _int(form.get("slow_len", ""), 2, 400, "The slow period")
    if fast_len >= slow_len:
        raise ValueError("The fast moving average needs a shorter period than the slow one.")
    touch, sides = form.get("touch", ""), form.get("sides", "both")
    if touch not in crossover.TOUCH or sides not in crossover.SIDES:
        raise ValueError("Pick the pullback rule and which side to trade.")
    return spec, tf, Rules(fast_kind, fast_len, slow_kind, slow_len, touch, sides)


def _rr(raw: str) -> float:
    try:
        rr = float(raw or 0)
    except ValueError:
        raise ValueError("The risk : reward should be a number, like 2 for 1:2.") from None
    if not 0 <= rr <= 20:
        raise ValueError("The risk : reward should be between 0 and 20.")
    return rr


# ---- backtest -----------------------------------------------------------------------------

def _read_backtest(form) -> ma_backtest.Params:
    spec, tf, rules = _read_setup(form, ma_backtest.TIMEFRAMES)
    today = now_ist().date()
    try:
        start, end = date.fromisoformat(form.get("start", "")), date.fromisoformat(form.get("end", "") or today.isoformat())
    except ValueError:
        raise ValueError("Give the backtest's first and last day.") from None
    if not start <= end <= today:
        raise ValueError("The first day must come before the last, and the last can't be in the future.")
    stop, trail, fill = form.get("stop", "candle"), form.get("trail", ""), form.get("fill", "open")
    if stop not in crossover.STOPS or trail not in crossover.TRAILS or fill not in crossover.FILLS:
        raise ValueError("Pick the SL, trail and gap options.")
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
    exits = Exits(stop, rr, book, trail, bool(form.get("breakeven")), bool(form.get("ma_exit")), fill, square_off)
    return ma_backtest.Params(spec, tf, start, end, rules, exits, risk)


@router.post("/backtest")
async def backtest(request: Request, user: dict = Depends(guard)):
    form = await request.form()
    try:
        p = _read_backtest(form)
        n = len(universe.resolve(p.spec, now_ist().date()))
        needed = ma_backtest.requests_needed(n, p.tf, p.start, p.end, p.rules)
        if needed > ma_backtest.MAX_REQUESTS:
            raise ValueError(f"That needs about {needed:,} Kite requests ({n} stocks); the limit is {ma_backtest.MAX_REQUESTS:,} "
                             f"(about 9 minutes). Shorten the period, use a longer candle, or a shorter list.")
        if brokers.load(user["username"]).get("status") != "connected":
            raise ValueError("Kite isn't connected. Log in on the Broker page first.")
        run = ma_backtest.start(user["username"], p)
    except ValueError as e:
        return fail(str(e))
    return toast(render(request, "partials/ma_progress.html", {"run": run}), f"Backtest started on {run.total} stock{'s' if run.total != 1 else ''}")


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
    return {"rep": rep, "p": p, "spec_label": universe.describe(p.spec), "tf_name": fractals.label(p.tf),
            "chart_json": {"curve": curve, "trades": trades, "daily": p.tf == "1d"},
            "exit_names": crossover.EXIT_NAMES}


@router.get("/backtest/status")
def status(request: Request, user: dict = Depends(guard)):
    run = ma_backtest.current(user["username"])
    if not run:
        return HTMLResponse("")
    if run.status == "running":
        return render(request, "partials/ma_progress.html", {"run": run})
    return render(request, "partials/ma_result.html", {"run": run, **_run_ctx(run), **_options()})


@router.post("/backtest/cancel")
def cancel(request: Request, user: dict = Depends(guard)):
    ma_backtest.cancel(user["username"])
    return toast(HTMLResponse("", headers={"HX-Reswap": "none"}), "Stopping after the stock in progress")


@router.get("/backtest/csv")
def csv_download(user: dict = Depends(guard)):
    run = ma_backtest.current(user["username"])
    if not run or run.status == "running":
        return Response("No finished backtest", status_code=404)
    name = f"ma-backtest-{run.params.tf}-{run.params.start}-{run.params.end}.csv"
    return Response(ma_backtest.to_csv(run), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/backtest/chart")
def trade_chart(symbol: str, user: dict = Depends(guard)):
    run = ma_backtest.current(user["username"])
    if not run or run.status == "running" or symbol not in run.symbols:
        return JSONResponse({"error": "That backtest isn't available any more. Run it again."}, status_code=404)
    try:
        return JSONResponse(ma_backtest.chart(run, symbol))
    except KiteAuthError:
        return JSONResponse({"error": "Kite isn't connected or the session has expired."}, status_code=400)
    except KiteError as e:
        return JSONResponse({"error": f"Couldn't get prices from Kite: {e}"}, status_code=400)


# ---- live scanners --------------------------------------------------------------------------

def _list(request: Request, user: dict):
    return render(request, "partials/ma_scanners.html", _ctx(user, "scanners"))


@router.post("/scanners")
async def create(request: Request, user: dict = Depends(guard)):
    form = await request.form()
    username = user["username"]
    try:
        if len(store.list(ma_scanner.COL, user=username)) >= ma_scanner.MAX_SCANNERS:
            raise ValueError(f"You can keep up to {ma_scanner.MAX_SCANNERS} scanners. Remove one first.")
        spec, tf, rules = _read_setup(form, ma_scanner.TIMEFRAMES)
        if problem := ma_scanner.limit_problem(len(universe.resolve(spec, now_ist().date())), tf):
            raise ValueError(problem)
        stop = form.get("stop", "candle")
        if stop not in crossover.STOPS:
            raise ValueError("Pick the SL to show in messages.")
        rr = _rr(form.get("rr", "2"))
        hooks, error = webhooks.parse_urls(form.get("webhooks", ""))
        if error:
            raise ValueError(error)
        payload, error = webhooks.parse_payload(form.get("webhook_payload", ""))
        if error:
            raise ValueError(error)
    except ValueError as e:
        return fail(str(e))
    channels = form.getlist("channels")
    name = (form.get("name") or "").strip()[:60] or f"{universe.describe(spec)} {rules.label}"
    now = now_ist()
    sc = {"id": new_id(), "user": username, "name": name, "spec": spec, "tf": tf, "rules": rules.__dict__.copy(),
          "stop": stop, "rr": rr, "gap_alerts": bool(form.get("gap_alerts")),
          "channels": [c for c in notify.CHANNELS if c in channels], "webhooks": hooks, "webhook_payload": payload,
          "status": "active", "created_at": now.isoformat(), "armed_at": now.isoformat(), "state": {}, "fired": []}
    store.put(ma_scanner.COL, sc["id"], sc)
    msg = "Scanner saved. It reads the candles at the next candle close." + ("" if sc["channels"] or hooks else " It has nowhere to send yet.")
    return toast(_list(request, user), msg)


def _own(scanner_id: str, user: dict) -> dict | None:
    sc = store.get(ma_scanner.COL, scanner_id)
    return sc if sc and sc["user"] == user["username"] else None


@router.post("/scanners/{scanner_id}/pause")
def pause(request: Request, scanner_id: str, user: dict = Depends(guard)):
    if not (sc := _own(scanner_id, user)):
        return fail("That scanner is gone.")
    if sc["status"] == "active":
        store.update(ma_scanner.COL, scanner_id, {"status": "paused"})
        return toast(_list(request, user), "Paused")
    # Back on: what happened while it was paused isn't reported.
    store.update(ma_scanner.COL, scanner_id, {"status": "active", "armed_at": now_ist().isoformat(), "state": {}, "evaluated_at": ""})
    return toast(_list(request, user), "Watching again from the next candle close")


@router.post("/scanners/{scanner_id}/delete")
def delete(request: Request, scanner_id: str, user: dict = Depends(guard)):
    if _own(scanner_id, user):
        store.delete(ma_scanner.COL, scanner_id)
    return toast(_list(request, user), "Scanner removed")


@router.post("/scanners/{scanner_id}/test")
def test_message(scanner_id: str, user: dict = Depends(guard)):
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
