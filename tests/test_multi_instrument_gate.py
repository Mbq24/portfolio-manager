"""The gate must reject instruments that lose to simply holding them.

Why this exists: the first version of this gate had five criteria and none of them
compared the strategy to buy-and-hold. It admitted QQQ with a holdout return of +17.0%
while holding QQQ returned +38.4% (return/drawdown 0.78 vs 2.02). A trend rule that
underperforms ownership of the same asset is not a source of edge — it is an expensive
way to hold it. These tests pin the hurdle, the ratio-stability guard, and the
independence clustering so that regression is impossible.

The QQQ numbers below are the real ones measured 2026-10-23.
"""
import numpy as np
import pandas as pd
import pytest

from multi_instrument_gate import (
    CORE,
    WIDE,
    Result,
    beats_buy_and_hold,
    correlation_families,
    evaluate_gate,
    near_threshold_singletons,
    normalize_universe,
    pooled_stats,
    ret_dd,
)


def result(**kw):
    """A result that passes every criterion; override any field per test."""
    base: dict = dict(
        ticker="X", group="crypto", tradable=True,
        bars=1200, trades=60, be_bps=80.0, gross=100.0, net=50.0,
        h_trades=20, h_net=30.0, h_dd=25.0, h_bh=-10.0, h_bh_dd=55.0,
    )
    base.update(kw)
    return Result(**base)  # type: ignore[arg-type]


# ── the hurdle -------------------------------------------------------------

def test_baseline_passes_everything():
    ok, reasons, bh_ok, _ = evaluate_gate(result())
    assert ok, reasons
    assert bh_ok


def test_rejects_qqq_which_lost_to_holding_the_index():
    """Real 2026-10-23 numbers: holdout +17.0% vs B&H +38.4%, ret/DD 0.78 vs 2.02."""
    r = result(ticker="QQQ", group="us_equity", trades=58, be_bps=30.4, net=23.63,
               h_trades=19, h_net=17.0, h_dd=21.6, h_bh=38.4, h_bh_dd=19.0)
    ok, reasons, bh_ok, bh_reason = evaluate_gate(r)
    assert not ok
    assert not bh_ok
    assert any("vs B&H" in x for x in reasons)
    assert "ret/DD" in bh_reason


def test_accepts_lower_return_when_risk_adjusted_is_better():
    """The policy decision: 20% net at 10% DD (2.0) beats 30% net at 20% DD (1.5).

    A risk-managed rule is expected to give up raw upside. Comparing raw return would
    reject the very thing the rule is for; scaling exposure moves both legs, which is
    why the ratio is the comparable quantity.
    """
    r = result(h_net=20.0, h_dd=10.0, h_bh=30.0, h_bh_dd=20.0)
    ok, _, bh_ok, _ = evaluate_gate(r, hurdle="risk_adjusted")
    assert bh_ok and ok


def test_hurdle_both_demands_raw_return_superiority_too():
    """Same numbers, stricter policy — must now fail on the raw-return leg."""
    r = result(h_net=20.0, h_dd=10.0, h_bh=30.0, h_bh_dd=20.0)
    ok, reasons, bh_ok, bh_reason = evaluate_gate(r, hurdle="both")
    assert not bh_ok and not ok
    assert "<=" in bh_reason


def test_hurdle_none_restores_pre_hurdle_behaviour():
    r = result(ticker="QQQ", h_net=17.0, h_dd=21.6, h_bh=38.4, h_bh_dd=19.0)
    ok, _, bh_ok, _ = evaluate_gate(r, hurdle="none")
    assert bh_ok and ok


def test_missing_buy_and_hold_comparison_fails_closed():
    r = result(h_bh=None)
    ok, reason = beats_buy_and_hold(r)
    assert not ok
    assert "no holdout" in reason


# ── ratio stability --------------------------------------------------------

def test_ratio_is_none_when_drawdown_is_zero():
    assert ret_dd(10.0, 0.0) is None
    assert ret_dd(10.0, None) is None
    assert ret_dd(None, 5.0) is None


def test_tiny_drawdown_does_not_manufacture_a_giant_ratio():
    """A 5% gain on a 0.2% drawdown scores 25.0 — noise, not skill.

    When either drawdown is under the floor the hurdle falls back to requiring a
    higher raw return, so an instrument that barely traded cannot pass on a
    near-zero denominator.
    """
    r = result(h_net=5.0, h_dd=0.2, h_bh=2.0, h_bh_dd=20.0)
    ok, reason = beats_buy_and_hold(r, min_dd_for_ratio=1.0)
    assert ok, reason
    assert "unstable" in reason


def test_unstable_ratio_still_rejects_when_out_returned():
    r = result(h_net=1.0, h_dd=0.2, h_bh=2.0, h_bh_dd=20.0)
    ok, reason = beats_buy_and_hold(r, min_dd_for_ratio=1.0)
    assert not ok
    assert "unstable" in reason


# ── the other criteria -----------------------------------------------------

def test_rejects_below_break_even_floor():
    ok, reasons, _, _ = evaluate_gate(result(be_bps=20.7))   # real SPY number
    assert not ok
    assert any("BE" in x for x in reasons)


def test_rejects_insufficient_holdout_trades():
    ok, reasons, _, _ = evaluate_gate(result(h_trades=4))
    assert not ok
    assert any("holdout trades" in x for x in reasons)


def test_rejects_negative_holdout():
    ok, reasons, _, _ = evaluate_gate(result(h_net=-4.35))   # real GDX number
    assert not ok
    assert any("holdout net" in x for x in reasons)


def test_rejects_no_trades():
    ok, reasons, _, _ = evaluate_gate(result(trades=0, be_bps=None))
    assert not ok
    assert any("no trades" in x for x in reasons)


def test_untradable_instrument_can_never_be_adopted():
    """GC=F is data-only — the same asset as GLD at 0.91, but not fillable."""
    ok, reasons, _, _ = evaluate_gate(result(ticker="GC=F", tradable=False))
    assert not ok
    assert any("not tradable" in x for x in reasons)


# ── universe wiring --------------------------------------------------------

def test_universe_normalisation_keeps_two_tuple_entries_tradable():
    out = normalize_universe([("BTC-USD", "crypto"), ("GC=F", "metals", False)])
    assert out == [("BTC-USD", "crypto", True), ("GC=F", "metals", False)]


def test_core_carries_gold_futures_as_untradable_reference():
    tickers = {t: tradable for t, _g, tradable in normalize_universe(CORE)}
    assert tickers["GC=F"] is False
    assert tickers["GLD"] is True


def test_wide_universe_is_a_superset_of_core():
    core = {t for t, *_ in normalize_universe(CORE)}
    wide = {t for t, *_ in normalize_universe(WIDE)}
    assert core <= wide


# ── independence -----------------------------------------------------------

def test_correlated_instruments_collapse_into_one_family():
    """|rho| counts: a perfectly inverse pair is the same observation, not a new one."""
    rng = np.random.RandomState(0)
    n = 500
    base = rng.randn(n)
    corr = pd.DataFrame({
        "A": base + 0.05 * rng.randn(n),
        "B": base + 0.05 * rng.randn(n),
        "C": -base + 0.05 * rng.randn(n),
        "D": rng.randn(n),
    }).corr()
    labels = correlation_families(corr, threshold=0.75)
    assert set(labels["A"]) == {"A", "B", "C"}
    assert labels["D"] == ("D",)


def test_uncorrelated_instruments_stay_separate():
    rng = np.random.RandomState(1)
    n = 500
    corr = pd.DataFrame({k: rng.randn(n) for k in "ABC"}).corr()
    labels = correlation_families(corr, threshold=0.75)
    assert labels["A"] == ("A",)
    assert labels["B"] == ("B",)
    assert labels["C"] == ("C",)


def test_near_threshold_singleton_is_flagged():
    """SOL sits at 0.750 against BTC/ETH — the real 2026-10-23 measurement.

    At a 0.75 cut it becomes its own "family", which would inflate the independent
    family count. It is flagged instead of merged, because merging is a judgement call.
    """
    keys = ["BTC-USD", "ETH-USD", "SOL-USD"]
    corr = pd.DataFrame(
        [[1.00, 0.84, 0.75],
         [0.84, 1.00, 0.75],
         [0.75, 0.75, 1.00]],
        index=keys, columns=keys)
    out = near_threshold_singletons(corr, [("BTC-USD", "ETH-USD"), ("SOL-USD",)],
                                    threshold=0.75)
    assert len(out) == 1
    assert out[0]["instrument"] == "SOL-USD"
    assert out[0]["closest"] in ("BTC-USD", "ETH-USD")
    assert out[0]["correlation"] == 0.75


def test_clearly_independent_singleton_is_not_flagged():
    keys = ["BTC-USD", "GLD"]
    corr = pd.DataFrame([[1.00, 0.15], [0.15, 1.00]], index=keys, columns=keys)
    assert near_threshold_singletons(corr, [("GLD",)], threshold=0.75) == []


def test_multi_member_families_are_never_flagged():
    keys = ["SPY", "QQQ"]
    corr = pd.DataFrame([[1.00, 0.95], [0.95, 1.00]], index=keys, columns=keys)
    assert near_threshold_singletons(corr, [("SPY", "QQQ")], threshold=0.75) == []


# ── pooled expectancy ------------------------------------------------------

def test_pooled_stats_basic_arithmetic():
    s = pooled_stats([10.0, -5.0, 10.0, -5.0])
    assert s["trades"] == 4
    assert s["win_rate"] == 50.0
    assert s["avg_trade_pct"] == 2.5
    assert s["profit_factor"] == 2.0          # 20 won / 10 lost


def test_pooled_stats_all_winners_has_no_profit_factor():
    s = pooled_stats([1.0, 2.0, 3.0])
    assert s["profit_factor"] is None         # undefined without losses


def test_pooled_stats_empty_returns_nothing():
    assert pooled_stats([]) == {}


def test_pooled_stats_needs_a_sample_for_a_t_stat():
    assert pooled_stats([5.0, -5.0])["t_stat"] is None
    assert pooled_stats([1.0, 2.0, 3.0, 4.0])["t_stat"] is not None
