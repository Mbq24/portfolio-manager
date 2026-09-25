"""Position sizing takes an explicit dollar budget.

Why this exists: the old path sized as `cash * risk_pct / 100`. With one position cash == equity
so the difference is invisible, but with three instruments the third would be sized off cash already
spent by the first two. Budget is the basis; cash is only a ceiling.
"""
import pytest

from portfolio.executor import size_for_budget


def test_size_is_budget_divided_by_price():
    assert size_for_budget(100.0, 1000.0) == 10.0


def test_cash_is_a_ceiling_not_the_basis():
    assert size_for_budget(100.0, 1000.0, available_cash=400.0) == 4.0


def test_cash_above_budget_does_not_inflate_size():
    assert size_for_budget(100.0, 500.0, available_cash=9999.0) == 5.0


def test_negative_budget_clamps_to_zero():
    assert size_for_budget(100.0, -50.0) == 0.0


def test_rejects_nonpositive_price():
    with pytest.raises(ValueError):
        size_for_budget(0.0, 1000.0)
