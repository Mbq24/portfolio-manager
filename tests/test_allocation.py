"""Exposure caps enforced at entry.

Three instruments at 15% each is 45% gross -- and BTC/ETH at 0.84 daily-return correlation means the
crypto sleeve behaves like one ~30% bet. The per-class cap is what keeps that honest.
"""
from portfolio.allocation import can_enter


def book(equity=10_000.0, total=0.0, by_class=None):
    return {"equity": equity, "total": total, "by_class": by_class or {}}


def test_allows_when_both_caps_have_room():
    b = book(total=2_000.0, by_class={"crypto": 2_000.0})
    ok, why = can_enter(b, "GLD", "metals", 2_000.0, 45.0, 30.0)
    assert ok, why


def test_blocks_when_total_cap_would_break():
    b = book(total=3_000.0)
    ok, why = can_enter(b, "GLD", "metals", 2_000.0, 45.0, 30.0)   # 5000 > 4500
    assert not ok
    assert "total" in why.lower()


def test_blocks_when_class_cap_would_break():
    b = book(total=4_000.0, by_class={"crypto": 2_900.0})
    ok, why = can_enter(b, "ETHUSD", "crypto", 500.0, 45.0, 30.0)  # 3400 > 3000
    assert not ok
    assert "crypto" in why.lower()


def test_exactly_at_cap_is_allowed():
    """Caps are ceilings, not exclusive bounds."""
    b = book(total=2_500.0, by_class={"crypto": 2_500.0})
    ok, why = can_enter(b, "ETHUSD", "crypto", 500.0, 45.0, 30.0)  # 3000 == 3000
    assert ok, why


def test_zero_notional_is_allowed():
    ok, _ = can_enter(book(), "GLD", "metals", 0.0, 45.0, 30.0)
    assert ok


def test_uncapped_class_only_checks_total():
    b = book(total=4_400.0)
    ok, _ = can_enter(b, "XYZ", "", 50.0, 45.0, 30.0)   # 4450 <= 4500, no class
    assert ok
