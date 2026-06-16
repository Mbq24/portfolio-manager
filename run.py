#!/usr/bin/env python3
"""
Portfolio Manager — main loop.

Usage:
    python run.py                  # one cycle: ingest signal → paper trade → report
    python run.py --summary        # just show current state
    python run.py --reset          # reset portfolio to $10,000
"""

import argparse
import sys
from portfolio.state import PortfolioState
from portfolio.signal_ingest import latest_signal
from portfolio.executor import execute_trade
from portfolio.reporter import generate_report, save_report
from portfolio.prices import PriceFeed


def main():
    parser = argparse.ArgumentParser(description="Portfolio Manager")
    parser.add_argument("--summary", action="store_true", help="Show current state only")
    parser.add_argument("--reset", action="store_true", help="Reset portfolio to $10,000")
    parser.add_argument("--prices", action="store_true", help="Show latest prices only")
    args = parser.parse_args()

    # Load or reset portfolio
    if args.reset:
        pf = PortfolioState()
        pf.save()
        print("Portfolio reset to $10,000")
        return

    # Price feed
    feed = PriceFeed()
    if args.prices:
        print("=" * 60)
        print("  MARKET PRICES")
        print("=" * 60)
        prices = feed.get_prices(["SPY", "QQQ", "GLD", "C:XAUUSD"])
        print(feed.summary())
        return

    pf = PortfolioState.load()

    if args.summary:
        print(generate_report(pf))
        return

    # Full cycle: ingest signal → decide → execute → report
    print("=" * 60)
    print("  PORTFOLIO MANAGER — RUN CYCLE")
    print("=" * 60)

    # Fetch current prices first
    print()
    print("  Current prices:")
    prices = feed.get_prices(["SPY", "GLD", "C:XAUUSD"])
    print(feed.summary())
    print()

    # Mark existing positions with latest prices
    pf.mark_prices(prices)
    print(f"  Equity: ${pf.equity:.2f} | Cash: ${pf.cash:.2f}")
    print()

    # Get signal from VT
    print("  [1/3] Reading VT signal...")
    signal = latest_signal()
    if signal is None:
        print("  No VT signal available. Holding current positions.")
        print(generate_report(pf))
        return

    # Use live price from feed instead of VT's $0
    gold_price = prices.get("C:XAUUSD", prices.get("GLD", 0))
    if gold_price > 0:
        signal["price"] = gold_price

    print(f"  Signal: {signal['action']} | Confidence: {signal['confidence']:.1%} | Price: ${signal['price']:.2f}")
    print()

    # Step 2: Decide whether to trade
    print("  [2/3] Evaluating trade...")
    if signal["action"] == "hold":
        print("  VT says HOLD. No action taken.")
    elif signal["action"] == "buy":
        if any(p.asset == "GOLD" for p in pf.positions):
            print("  Already in GOLD position. Holding.")
        else:
            execution = execute_trade("buy", signal["price"], cash=pf.cash)
            pf.enter_position("GOLD", execution["side"], execution["size"],
                              execution["fill_price"])
            print(f"  ENTERED GOLD {execution['side']} "
                  f"{execution['size']:.4f} @ ${execution['fill_price']:.2f} "
                  f"(cost ${execution['cost']:.2f})")
    elif signal["action"] == "sell":
        if any(p.asset == "GOLD" for p in pf.positions):
            pnl = pf.exit_position("GOLD", signal["price"])
            tag = "PROFIT" if pnl > 0 else "LOSS"
            print(f"  EXITED GOLD → {tag} ${abs(pnl):.2f}")
        else:
            print("  No GOLD position to exit.")
    print()

    # Step 3: Report
    print("  [3/3] Generating report...")
    report = generate_report(pf)
    pf.save()
    save_report(report)
    print()
    print(report)


if __name__ == "__main__":
    main()
