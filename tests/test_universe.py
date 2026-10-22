from datetime import date

import httpx

from app import universe
from app.kite import Instrument
from app.store import store


def test_parse_symbols_and_describe():
    assert universe.parse_symbols("infy, tcs\nNSE:SBIN,, infy\n nifty  50 ") == ["INFY", "TCS", "SBIN", "NIFTY 50"]
    assert universe.describe({"list": "nifty200"}) == "Nifty 200"
    assert universe.describe({"symbols": ["INFY"]}) == "INFY"
    assert universe.describe({"symbols": ["A", "B", "C", "D"]}) == "A, B and 2 more"


def test_fno_stocks_come_from_nfo_futures(monkeypatch):
    insts = {
        "INFY": Instrument("INFY", "INFOSYS", 1, False),
        "NIFTY 50": Instrument("NIFTY 50", "NIFTY 50", 2, True),
        "NFO:INFY26OCTFUT": Instrument("INFY26OCTFUT", "INFY", 3, False, "NFO", "FUT", "2026-10-27", "INFY"),
        "NFO:INFY26NOVFUT": Instrument("INFY26NOVFUT", "INFY", 4, False, "NFO", "FUT", "2026-11-24", "INFY"),
        "NFO:NIFTY26OCTFUT": Instrument("NIFTY26OCTFUT", "NIFTY", 5, False, "NFO", "FUT", "2026-10-27", "NIFTY"),
        "NFO:TCS26OCT3000CE": Instrument("TCS26OCT3000CE", "TCS", 6, False, "NFO", "CE", "2026-10-27", "TCS", 3000),
    }
    monkeypatch.setattr(universe, "instruments", lambda: insts)
    assert universe.fno_stocks() == ["INFY"]
    assert universe.resolve({"list": "fno"}, date(2026, 10, 22)) == ["INFY"]


def test_index_lists_fall_back_to_the_built_in_copy(monkeypatch):
    def down(*a, **k):
        raise httpx.ConnectError("no")
    monkeypatch.setattr(universe.httpx, "get", down)
    store.delete("settings", "nifty500")
    got = universe.symbols("nifty500", date(2026, 10, 22))
    assert len(got) >= 490 and "RELIANCE" in got and not any(s.startswith("DUMMY") for s in got)
    assert universe.symbols("nifty50", date(2026, 10, 22)) == universe.NIFTY50
