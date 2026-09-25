"""A cycle with no authoritative config must HOLD, not run the local file.

Why this exists: pm pulls its desired config from Forge (the control room) every cycle.
The old behaviour on an empty/absent remote was "run the local config" — described as
last-known-good. It is not safe here. On 2026-09-25 Forge's config was wiped by a deploy,
pm fell back to a stale local file, and the first cycle closed a live BTC position with
vol-expansion-momentum 1h instead of daily-trend-expansion-3of4 1d.

For a strategy that trades ~6 times a year, holding one cycle costs nothing. Trading the
wrong strategy costs a real position. So: no authority -> no action, loudly.
"""
import json
import sys
import types
import urllib.request

import pytest

import run as R
from portfolio import config as C


class _Resp:
    """Minimal stand-in for the urlopen context manager."""

    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _three_instrument_cfg():
    return C.normalize({"mode": "paper", "max_total_exposure_pct": 45.0,
                        "max_class_exposure_pct": 30.0,
                        "forge_push": True,
                        "forge_url": "https://forge.example",
                        "instruments": [
                            {"strategy": "daily-trend-expansion-3of4.yaml", "ticker": "BTC-USD",
                             "asset": "BTCUSD", "interval": "1d", "period": "1y",
                             "risk_pct": 15.0, "asset_class": "crypto", "enabled": True},
                            {"strategy": "daily-trend-expansion-3of4.yaml", "ticker": "ETH-USD",
                             "asset": "ETHUSD", "interval": "1d", "period": "1y",
                             "risk_pct": 15.0, "asset_class": "crypto", "enabled": True},
                            {"strategy": "daily-trend-expansion-3of4.yaml", "ticker": "GLD",
                             "asset": "GLD", "interval": "1d", "period": "1y",
                             "risk_pct": 15.0, "asset_class": "metals", "enabled": True}]})


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    runtime = tmp_path / "data" / "config.json"
    monkeypatch.setattr(C, "CONFIG_FILE", runtime)
    monkeypatch.setattr(C, "DEFAULT_FILE", tmp_path / "config.default.json")
    return runtime


# ------------------------------------------------------- empty / absent remote

def test_empty_forge_config_holds_the_cycle(sandbox, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _Resp({"config": None}))
    out = C.sync_from_forge(_three_instrument_cfg())

    assert out.get(C.HOLD_KEY) == C.HOLD_FORGE_EMPTY
    assert [i["ticker"] for i in out["instruments"]] == ["BTC-USD", "ETH-USD", "GLD"], (
        "holding must not mutate the book")


def test_hold_flag_is_not_written_to_disk(sandbox, monkeypatch):
    """If the hold persisted, every later cycle would inherit it and the loop would stop."""
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _Resp({"config": None}))
    C.sync_from_forge(_three_instrument_cfg())

    assert not sandbox.exists() or C.HOLD_KEY not in sandbox.read_text()


# ---------------------------------------------------------- unreachable remote

def test_unreachable_forge_holds_rather_than_trusting_the_local_file(sandbox, monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    out = C.sync_from_forge(_three_instrument_cfg())

    assert out.get(C.HOLD_KEY) == C.HOLD_FORGE_UNREACHABLE


def test_push_disabled_never_holds(sandbox, monkeypatch):
    """forge_push=false is an explicit local-only mode; nothing to hold for."""
    def boom(*a, **k):
        raise AssertionError("should not call Forge when forge_push is off")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    cfg = _three_instrument_cfg()
    cfg["forge_push"] = False

    assert C.HOLD_KEY not in C.sync_from_forge(cfg)


# ------------------------------------------------------------- healthy remote

def test_remote_instruments_list_replaces_the_local_book(sandbox, monkeypatch):
    remote = {"instruments": [{"strategy": "daily-trend-expansion-3of4.yaml",
                               "ticker": "GLD", "asset": "GLD", "interval": "1d",
                               "period": "1y", "risk_pct": 10.0, "asset_class": "metals",
                               "enabled": True}],
              "mode": "paper", "max_total_exposure_pct": 45.0, "max_class_exposure_pct": 30.0}
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp({"config": remote}))

    out = C.sync_from_forge(_three_instrument_cfg())

    assert [i["ticker"] for i in out["instruments"]] == ["GLD"]
    assert C.HOLD_KEY not in out


# ------------------------------------------- the whole cycle, held (integration)

def test_held_cycle_takes_no_entries_and_no_exits(sandbox, monkeypatch):
    """A held cycle must not evaluate a single instrument — not even to exit."""
    from portfolio.state import PortfolioState

    pf = PortfolioState()
    pf.enter_position("BTCUSD", "long", 0.0184, 81_424.90, meta={"ticker": "BTC-USD"})

    cfg = _three_instrument_cfg()
    cfg[C.HOLD_KEY] = C.HOLD_FORGE_EMPTY

    monkeypatch.setattr(R, "load_config", lambda: cfg)
    monkeypatch.setattr(R, "sync_from_forge", lambda c: dict(c))
    monkeypatch.setattr(R.PortfolioState, "load", staticmethod(lambda: pf))
    monkeypatch.setattr(R.PortfolioState, "save", lambda self: None)

    class FakeFeed:
        def get_prices(self, tickers):
            return {"BTC-USD": 84_000.0, "ETH-USD": 2_700.0, "GLD": 391.0, "SPY": 600.0}

        def summary(self):
            return "  (fake feed)"

    monkeypatch.setattr(R, "PriceFeed", FakeFeed)

    def must_not_run(*a, **k):
        raise AssertionError("a held cycle evaluated an instrument")

    monkeypatch.setattr(R, "load_strategy", must_not_run)
    monkeypatch.setattr(R, "compute_signal", must_not_run)

    captured = {}
    monkeypatch.setattr(R, "push_to_forge",
                        lambda c, p, n, s, hold_reason="": captured.update({"hold": hold_reason}))
    monkeypatch.setattr(R, "generate_report", lambda p: "report")
    monkeypatch.setattr(R, "save_report", lambda r: None)
    monkeypatch.setattr(R, "journal_append", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["run.py"])

    R.main()

    assert any(p.asset == "BTCUSD" for p in pf.positions), "the open position must survive"
    assert not any(p.asset in ("ETHUSD", "GLD") for p in pf.positions), "no new entries"
    assert captured.get("hold"), "a held cycle must be visible in the pushed state"


def test_normal_cycle_still_evaluates_and_pushes_no_hold(sandbox, monkeypatch):
    """The hold path must not have swallowed the normal path."""
    from portfolio.state import PortfolioState

    pf = PortfolioState()
    cfg = _three_instrument_cfg()

    monkeypatch.setattr(R, "load_config", lambda: cfg)
    monkeypatch.setattr(R, "sync_from_forge", lambda c: dict(c))
    monkeypatch.setattr(R.PortfolioState, "load", staticmethod(lambda: pf))
    monkeypatch.setattr(R.PortfolioState, "save", lambda self: None)

    class FakeFeed:
        def get_prices(self, tickers):
            return {"BTC-USD": 84_000.0, "ETH-USD": 2_700.0, "GLD": 391.0, "SPY": 600.0}

        def summary(self):
            return "  (fake feed)"

    monkeypatch.setattr(R, "PriceFeed", FakeFeed)
    monkeypatch.setattr(R, "load_strategy",
                        lambda p: types.SimpleNamespace(name="Daily Trend Expansion 3of4"))
    monkeypatch.setattr(R, "compute_signal",
                        lambda *a, **k: {"action": "hold", "confidence": 0.5, "price": 84_000.0})

    captured = {}
    monkeypatch.setattr(R, "push_to_forge",
                        lambda c, p, n, s, hold_reason="": captured.update({"hold": hold_reason, "name": n}))
    monkeypatch.setattr(R, "generate_report", lambda p: "report")
    monkeypatch.setattr(R, "save_report", lambda r: None)
    monkeypatch.setattr(R, "journal_append", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["run.py"])

    R.main()

    assert not captured.get("hold"), "a normal cycle must not report a hold"
    assert captured.get("name") == "Daily Trend Expansion 3of4"
