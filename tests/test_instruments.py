"""Instrument and index lists are fetched once a day, kept, and not retried on every request when the source refuses."""

import os
import time
from datetime import date

import httpx
import pytest

from app import kite, universe
from app.store import store

NSE_CSV = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange\n"
           "408065,1594,INFY,INFOSYS,0,,0,0.05,1,EQ,NSE,NSE\n")


class Resp:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=httpx.Request("GET", "http://x"), response=None)


@pytest.fixture
def lists(monkeypatch, tmp_path):
    calls = []
    answer = {"status": 200}
    monkeypatch.setattr(kite, "INSTRUMENTS_DIR", tmp_path)
    monkeypatch.setattr(kite, "EXCHANGES", ("NSE",))
    monkeypatch.setattr(kite, "_by_exchange", {})
    monkeypatch.setattr(kite, "_instruments", {})
    monkeypatch.setattr(kite, "_tried_at", -kite._RETRY_AFTER)
    monkeypatch.setattr(kite, "_failed_at", -kite._RETRY_AFTER)
    with kite._refreshing:  # let any background refresh started by an earlier test finish first
        pass
    monkeypatch.setattr(kite.httpx, "get", lambda url, **kw: calls.append(url) or Resp(answer["status"], NSE_CSV))
    return calls, answer, tmp_path


def test_downloaded_once_then_read_from_disk_until_the_next_publish(lists, monkeypatch):
    calls, answer, folder = lists
    assert "INFY" in kite.instruments() and len(calls) == 1 and (folder / "NSE.csv").exists()
    # A restart: the lists are read back from disk, not downloaded.
    monkeypatch.setattr(kite, "_instruments", {})
    monkeypatch.setattr(kite, "_by_exchange", {})
    assert "INFY" in kite.instruments() and len(calls) == 1
    # Kite has published a newer list since the copy was saved: it is downloaded again.
    old = kite.published_since() - 60
    os.utime(folder / "NSE.csv", (old, old))
    monkeypatch.setattr(kite, "_instruments", {})
    assert "INFY" in kite.instruments() and len(calls) == 2


def test_a_refusal_is_not_retried_on_every_request(lists, monkeypatch):
    calls, answer, folder = lists
    answer["status"] = 429
    with pytest.raises(kite.KiteError, match="few minutes"):
        kite.instruments()
    with pytest.raises(kite.KiteError, match="few minutes"):
        kite.instruments()  # a search keystroke a moment later doesn't ask Kite again
    assert len(calls) == 1
    # After the wait it tries again.
    monkeypatch.setattr(kite, "_failed_at", time.monotonic() - kite._RETRY_AFTER)
    answer["status"] = 200
    assert "INFY" in kite.instruments() and len(calls) == 2


def test_a_refusal_falls_back_to_the_last_copy_on_disk(lists, monkeypatch):
    calls, answer, folder = lists
    (folder / "NSE.csv").write_text(NSE_CSV)
    old = kite.published_since() - 3600
    os.utime(folder / "NSE.csv", (old, old))
    answer["status"] = 429
    assert "INFY" in kite.instruments() and kite._stale  # yesterday's list, and it will try again later
    answer["status"] = 200
    assert kite.refresh_instruments() == {"NSE": 1} and not kite._stale and len(calls) == 2


def test_nse_index_lists_are_asked_for_at_most_once_a_day(monkeypatch):
    asked = []
    monkeypatch.setattr(universe.httpx, "get", lambda *a, **k: asked.append(a) or (_ for _ in ()).throw(httpx.ConnectError("no")))
    store.delete("settings", "nifty100")
    today = date(2026, 10, 22)
    first = universe.symbols("nifty100", today)
    assert first == universe.built_in("nifty100") and len(asked) == 1
    universe.symbols("nifty100", today)
    universe.symbols("nifty100", today)
    assert len(asked) == 1  # not again today
    universe.symbols("nifty100", date(2026, 10, 23))
    assert len(asked) == 2  # tomorrow it tries again
    assert universe.refresh_all(today)["Nifty 100"] == "NSE didn't answer; kept the last list"


def test_rate_limit_spaces_calls_and_pauses():
    limit = kite.RateLimit(0.05)
    start = time.monotonic()
    for _ in range(4):
        limit.wait()
    assert time.monotonic() - start >= 0.15  # three gaps between four calls
    limit.pause(0.1)
    start = time.monotonic()
    limit.wait()
    assert time.monotonic() - start >= 0.09


def test_too_many_requests_is_retried_after_a_pause(monkeypatch):
    answers = [Resp(429), Resp(429), Resp(200)]
    for r in answers:
        r.json = lambda r=r: {"status": "success", "data": {"candles": []}} if r.status_code == 200 else {"status": "error"}
    monkeypatch.setattr(kite.httpx, "get", lambda *a, **k: answers.pop(0))
    client = kite.KiteClient("enctoken", enctoken="x")
    assert client.candles_range(1, "1d", date(2026, 10, 1), date(2026, 10, 2)) == [] and answers == []
    answers[:] = [Resp(429)] * (kite.TOO_MANY_RETRIES + 1)
    for r in answers:
        r.json = lambda: {"status": "error"}
    with pytest.raises(kite.KiteError, match="too many requests"):
        client.profile()
