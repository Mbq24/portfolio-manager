"""
portfolio/allocation.py — exposure caps enforced at entry.

Three instruments at 15% each is 45% gross exposure. But BTC and ETH have 0.84 daily-return
correlation, so the crypto sleeve behaves like a single ~30% bet — a total cap alone would
understate the real concentration. Hence two caps:

    max_total_exposure_pct  — across the whole book
    max_class_exposure_pct  — within one asset class (crypto / metals / ...)

Caps are CEILINGS: exactly-at-cap is allowed, only exceeding is refused.

Usage:
    book = book_snapshot(pf, {i["asset"]: i.get("asset_class", "") for i in enabled})
    ok, why = can_enter(book, asset, asset_class, notional, max_total_pct, max_class_pct)
"""


def book_snapshot(pf, asset_classes: dict) -> dict:
    """Current exposure, keyed by asset class.

    Positions are keyed by broker `asset` (e.g. BTCUSD), so `asset_classes` must be keyed the
    same way — not by yfinance ticker.
    """
    by_class: dict[str, float] = {}
    total = 0.0
    for pos in pf.positions:
        value = pos.size * pos.current_price
        total += value
        cls = asset_classes.get(pos.asset, "")
        if cls:
            by_class[cls] = by_class.get(cls, 0.0) + value
    return {"equity": float(pf.equity), "total": total, "by_class": by_class}


def can_enter(book: dict, asset: str, asset_class: str, notional: float,
              max_total_pct: float, max_class_pct: float) -> tuple[bool, str]:
    """Would adding `notional` of `asset` breach either exposure cap?

    Returns (allowed, reason). An empty/zero notional is a no-op and always allowed, so callers
    can gate unconditionally without special-casing "already sized to zero".
    """
    equity = float(book.get("equity") or 0.0)
    notional = float(notional or 0.0)
    if notional <= 0 or equity <= 0:
        return True, "no-op"

    total_now = float(book.get("total") or 0.0)
    total_cap = equity * float(max_total_pct) / 100.0
    if total_now + notional > total_cap:
        return False, ("total exposure would be $%.2f > cap $%.2f (%.0f%% of $%.2f equity)"
                       % (total_now + notional, total_cap, max_total_pct, equity))

    if asset_class:
        class_now = float((book.get("by_class") or {}).get(asset_class, 0.0))
        class_cap = equity * float(max_class_pct) / 100.0
        if class_now + notional > class_cap:
            return False, ("%s exposure would be $%.2f > cap $%.2f (%.0f%% of $%.2f equity)"
                           % (asset_class, class_now + notional, class_cap,
                              max_class_pct, equity))

    return True, "ok"
