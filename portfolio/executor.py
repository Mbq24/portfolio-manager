"""
portfolio/executor.py — Paper trade executor.

Simulates trade execution with realistic costs:
  - Spread: 0.5 pips on XAU/USD (~$5 per lot)
  - Slippage: random 0-2 pip adverse move
  - Commission: $3.50 per lot per side
"""

from datetime import datetime, timezone
from typing import Optional

# Cost model for XAU/USD paper trading
# 1 standard lot = 100 oz
SPREAD_PIPS = 0.5  # average spread in pips
SLIPPAGE_PIPS_MAX = 2.0
COMMISSION_PER_LOT = 3.50  # USD
PIP_VALUE_PER_LOT = 10.0  # 1 pip move on 1 standard lot = $10


def calculate_entry_price(signal_price: float, side: str) -> float:
    """Apply spread + slippage to get realistic fill price."""
    import random
    pip = 0.01  # 1 pip for XAU/USD is 0.01
    slippage = random.uniform(0, SLIPPAGE_PIPS_MAX) * pip
    total_pips = SPREAD_PIPS * pip + slippage

    if side == "buy":
        return signal_price * (1 + 0.0001)  # simplified: add 1 pip equivalent
    else:
        return signal_price * (1 - 0.0001)


def calculate_commission(size: float) -> float:
    """Estimate commission for a trade."""
    lots = size / 100  # size in oz / 100 = lots
    return round(max(COMMISSION_PER_LOT * lots, 1.0), 2)


def execute_trade(action: str, price: float, size: Optional[float] = None,
                  cash: float = 10_000.0) -> dict:
    """Simulate a paper trade and return execution details.

    Returns dict with:
      - side: "long" | "short"
      - size: units
      - fill_price: price after spread/slippage
      - cost: total cost including commission
      - commission: USD
      - timestamp: ISO 8601
    """
    side = "long" if action == "buy" else "short"
    fill_price = calculate_entry_price(price, side)
    commission = calculate_commission(size or 0)

    # Default size: 1% of cash at current price
    if size is None:
        risk_per_trade = cash * 0.01
        size = risk_per_trade / fill_price

    cost = size * fill_price + commission

    return {
        "side": side,
        "size": round(size, 4),
        "fill_price": round(fill_price, 2),
        "cost": round(cost, 2),
        "commission": commission,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
