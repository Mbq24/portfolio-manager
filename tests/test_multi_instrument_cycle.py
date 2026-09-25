"""Multi-instrument cycle: instruments must not interfere with each other.

These are the highest-value tests in this change. The pre-entry-exit bug of Sep 2026 cost a real
trade because a fallback silently changed semantics; the multi-asset form of that bug is one
instrument's signal touching another instrument's position. Test it, don't assume it.
"""
import types

import run
from portfolio.state import PortfolioState

STRAT = "daily-trend-expansion-3of4.yaml"

BTC = {"strategy": STRAT, "ticker": "BTC-USD", "asset": "BTCUSD", "interval": "1d",
       "period": "1y", "risk_pct": 15.0, "asset_class": "crypto", "enabled": True}
ETH = {"strategy": STRAT, "ticker": "ETH-USD", "asset": "ETHUSD", "interval": "1d",
       "period": "1y", "risk_pct": 15.0, "asset_class": "crypto", "enabled": True}
GLD = {"strategy": STRAT, "ticker": "GLD", "asset": "GLD", "interval": "1d",
       "period": "1y", "risk_pct": 15.0, "asset_class": "metals", "enabled": True}

CFG = {"mode": "paper", "exit_lookback_bars": 6,
       "max_total_exposure_pct": 45.0, "max_class_exposure_pct": 30.0}

CLASSES = {"BTCUSD": "crypto", "ETHUSD": "crypto", "GLD": "metals"}


def wire(monkeypatch, actions):
    """actions: {asset: (action, price)}"""
    monkeypatch.setattr(run, "load_strategy",
                        lambda p: types.SimpleNamespace(name="Daily Trend Expansion 3of4"))

    def fake_compute(strategy, ticker, interval, period, asset,
                     exit_lookback_bars=0, entry_time=""):
        action, price = actions[asset]
        return {"action": action, "confidence": 0.9, "price": price}

    monkeypatch.setattr(run, "compute_signal", fake_compute)


def test_exit_for_one_asset_does_not_close_another(monkeypatch):
    pf = PortfolioState()
    pf.enter_position("BTCUSD", "long", 0.01, 80_000.0, meta={"ticker": "BTC-USD"})
    pf.enter_position("ETHUSD", "long", 0.2, 3_000.0, meta={"ticker": "ETH-USD"})
    wire(monkeypatch, {"BTCUSD": ("sell", 82_000.0), "ETHUSD": ("hold", 3_100.0)})

    run.evaluate_instrument(BTC, CFG, pf, {"BTC-USD": 82_000.0, "ETH-USD": 3_100.0}, CLASSES)

    assert not any(p.asset == "BTCUSD" for p in pf.positions), "BTC should be closed"
    assert any(p.asset == "ETHUSD" for p in pf.positions), "ETH must be untouched"


def test_entry_for_one_asset_not_blocked_by_another_being_long(monkeypatch):
    pf = PortfolioState()
    pf.enter_position("BTCUSD", "long", 0.01, 80_000.0, meta={"ticker": "BTC-USD"})
    wire(monkeypatch, {"BTCUSD": ("hold", 80_000.0), "ETHUSD": ("buy", 3_000.0)})

    acted, _, _ = run.evaluate_instrument(
        ETH, CFG, pf, {"BTC-USD": 80_000.0, "ETH-USD": 3_000.0}, CLASSES)

    assert acted
    assert any(p.asset == "ETHUSD" for p in pf.positions)


def test_same_asset_never_double_enters(monkeypatch):
    pf = PortfolioState()
    pf.enter_position("BTCUSD", "long", 0.01, 80_000.0, meta={"ticker": "BTC-USD"})
    wire(monkeypatch, {"BTCUSD": ("buy", 80_000.0)})

    acted, _, _ = run.evaluate_instrument(
        BTC, CFG, pf, {"BTC-USD": 80_000.0}, CLASSES)

    assert not acted
    assert len([p for p in pf.positions if p.asset == "BTCUSD"]) == 1


def test_exit_signal_with_no_position_is_a_noop(monkeypatch):
    pf = PortfolioState()
    wire(monkeypatch, {"GLD": ("sell", 400.0)})

    acted, _, _ = run.evaluate_instrument(GLD, CFG, pf, {"GLD": 400.0}, CLASSES)

    assert not acted
    assert pf.positions == []


def test_class_cap_blocks_the_third_crypto_sized_entry(monkeypatch):
    """BTC already 29% of a $10k book; ETH's 15% budget would push crypto past 30%."""
    pf = PortfolioState()
    pf.enter_position("BTCUSD", "long", 0.03625, 80_000.0, meta={"ticker": "BTC-USD"})
    pf.mark_prices({"BTC-USD": 80_000.0})           # 0.03625 * 80k = $2,900 = 29%
    wire(monkeypatch, {"ETHUSD": ("buy", 3_000.0)})

    acted, _, _ = run.evaluate_instrument(
        ETH, CFG, pf, {"BTC-USD": 80_000.0, "ETH-USD": 3_000.0}, CLASSES)

    assert not acted, "entry should be blocked by the 30% class cap"
    assert not any(p.asset == "ETHUSD" for p in pf.positions)


def test_gold_is_not_blocked_by_the_crypto_class_cap(monkeypatch):
    pf = PortfolioState()
    pf.enter_position("BTCUSD", "long", 0.03625, 80_000.0, meta={"ticker": "BTC-USD"})
    pf.mark_prices({"BTC-USD": 80_000.0})
    wire(monkeypatch, {"GLD": ("buy", 400.0)})

    acted, _, _ = run.evaluate_instrument(GLD, CFG, pf, {"GLD": 400.0}, CLASSES)

    assert acted, "metals is a different class and has its own budget"


def test_sanity_guard_blocks_an_absurd_exit_price(monkeypatch):
    pf = PortfolioState()
    pf.enter_position("BTCUSD", "long", 0.01, 80_000.0, meta={"ticker": "BTC-USD"})
    wire(monkeypatch, {"BTCUSD": ("sell", 20_000.0)})   # -75% in one cycle = contaminated feed

    acted, _, _ = run.evaluate_instrument(
        BTC, CFG, pf, {"BTC-USD": 20_000.0}, CLASSES)

    assert not acted
    assert any(p.asset == "BTCUSD" for p in pf.positions), "must hold, not book fake P&L"


def test_strategy_failure_returns_cleanly_without_touching_state(monkeypatch):
    """One broken instrument must not stop the cycle or corrupt state."""
    pf = PortfolioState()
    pf.enter_position("BTCUSD", "long", 0.01, 80_000.0, meta={"ticker": "BTC-USD"})

    def boom(p):
        raise RuntimeError("vendor yaml missing")

    monkeypatch.setattr(run, "load_strategy", boom)

    acted, name, _ = run.evaluate_instrument(
        BTC, CFG, pf, {"BTC-USD": 80_000.0}, CLASSES)

    assert not acted
    assert name == ""
    assert any(p.asset == "BTCUSD" for p in pf.positions)


def test_paper_entry_sizes_off_equity_not_depleted_cash(monkeypatch):
    """The latent bug: instrument N must not be sized off cash spent by instrument N-1.

    BTC is sized at 16% of the book so the 45% total cap leaves room for GLD -- otherwise this
    test trips the (correct) cap instead of exercising the sizing path.
    """
    pf = PortfolioState()
    pf.enter_position("BTCUSD", "long", 0.02, 80_000.0, meta={"ticker": "BTC-USD"})
    pf.mark_prices({"BTC-USD": 80_000.0})
    equity_before = pf.equity          # $10,000
    cash_before = pf.cash              # $8,400  (equity-based size differs from cash-based)
    wire(monkeypatch, {"GLD": ("buy", 400.0)})

    run.evaluate_instrument(GLD, CFG, pf, {"GLD": 400.0}, CLASSES)

    gld = next(p for p in pf.positions if p.asset == "GLD")
    expected_size = (equity_before * 0.15) / gld.entry_price
    cash_based_size = (cash_before * 0.15) / gld.entry_price
    assert abs(gld.size - expected_size) / expected_size < 0.01, (
        "size should be 15%% of EQUITY ($%.2f) -> %.4f units, not of remaining cash "
        "($%.2f) -> %.4f units" % (equity_before, expected_size, cash_before, cash_based_size))
    assert gld.size > cash_based_size, "must differ from the old cash-based sizing"
