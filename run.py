#!/usr/bin/env python3
"""
Portfolio Manager — main loop.

Routes trades through Alpaca broker (paper or live).
Use --paper to fall back to the internal paper executor for testing.

Usage:
    python run.py                  # one cycle: ingest signal → Alpaca trade → report
    python run.py --paper          # use internal paper executor instead of Alpaca
    python run.py --summary        # just show current state
    python run.py --reset          # reset portfolio to $10,000
    python run.py --alpaca         # show Alpaca account status
    python run.py --prices         # show live prices
    python run.py --broker         # sync Alpaca positions → portfolio state
"""

import argparse
import sys
from portfolio.state import PortfolioState
from portfolio.signal_ingest import latest_signal
from portfolio.executor import execute_trade
from portfolio.reporter import generate_report, save_report
from portfolio.prices import PriceFeed
from portfolio.broker import AlpacaBroker

ASSET = "GLD"  # ETF traded via Alpaca


def alpaca_buy(broker, price, cash):
    """Place a buy order through Alpaca. Returns (order, error)."""
    risk_amount = cash * 0.01  # 1% of cash per trade
    qty = max(1, int(risk_amount / price))  # whole shares only
    print(f"  Alpaca: buying {qty} {ASSET} @ market (${price:.2f})")
    order = broker.market_order(ASSET, qty, "buy")
    if "error" in order:
        return None, order["error"]
    return order, None


def alpaca_sell(broker):
    """Close a position through Alpaca. Returns (result, error)."""
    print(f"  Alpaca: closing {ASSET} position")
    result = broker.close_position(ASSET)
    if isinstance(result, dict) and "error" in result:
        return None, result["error"]
    return result, None


def main():
    parser = argparse.ArgumentParser(description="Portfolio Manager")
    parser.add_argument("--summary", action="store_true", help="Show current state only")
    parser.add_argument("--reset", action="store_true", help="Reset portfolio to $10,000")
    parser.add_argument("--prices", action="store_true", help="Show latest prices only")
    parser.add_argument("--alpaca", action="store_true", help="Show Alpaca account status")
    parser.add_argument("--broker", action="store_true", help="Sync Alpaca positions → portfolio state")
    parser.add_argument("--paper", action="store_true", help="Use internal paper executor instead of Alpaca")
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

    # Alpaca account check
    broker = AlpacaBroker(paper=True)
    if args.alpaca:
        print("=" * 60)
        print("  ALPACA BROKER STATUS")
        print("=" * 60)
        print(broker.account_summary())
        print()
        pos = broker.positions()
        if pos:
            print(f"  Open positions ({len(pos)}):")
            for p in pos:
                print(f"    {p['symbol']:6s} {int(float(p['qty'])):>4} shares  "
                      f"P&L: ${float(p['unrealized_pl']):>+7.2f}")
        else:
            print("  No open positions.")
        return

    # Broker sync: import Alpaca positions into local state
    if args.broker:
        pf = PortfolioState.load()
        alpaca_positions = broker.positions()
        print(f"  Alpaca has {len(alpaca_positions)} open positions")
        for ap in alpaca_positions:
            sym = ap["symbol"]
            qty = abs(int(float(ap["qty"])))
            side = "long" if float(ap["qty"]) > 0 else "short"
            entry = float(ap.get("avg_entry_price", 0))
            current = float(ap.get("current_price", 0))
            # Check if already tracked locally
            if not any(p.asset == sym for p in pf.positions):
                pf.enter_position(sym, side, qty, entry)
                print(f"  Imported {sym}: {side} {qty} @ ${entry:.2f}")
            else:
                print(f"  {sym} already tracked locally")
        pf.save()
        return

    pf = PortfolioState.load()

    if args.summary:
        print(generate_report(pf))
        return

    # Full cycle: ingest signal → decide → execute → report
    mode = "PAPER" if args.paper else "ALPACA"
    print("=" * 60)
    print(f"  PORTFOLIO MANAGER — RUN CYCLE  [{mode}]")
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

    # Use live price from feed — GLD ETF for Alpaca orders, gold spot for context
    gld_price = prices.get("GLD", 0)
    gold_spot = prices.get("C:XAUUSD", 0)
    if gld_price > 0:
        signal["price"] = gld_price  # use GLD ETF for position sizing
    elif gold_spot > 0:
        signal["price"] = gold_spot  # fall back to spot

    print(f"  Signal: {signal['action']} | Confidence: {signal['confidence']:.1%} | Price: ${signal['price']:.2f}")
    print()

    # Step 2: Decide whether to trade
    print("  [2/3] Evaluating trade...")
    if signal["action"] == "hold":
        print("  VT says HOLD. No action taken.")

    elif signal["action"] == "buy":
        if any(p.asset == ASSET for p in pf.positions):
            print(f"  Already in {ASSET} position. Holding.")
        elif args.paper:
            # Paper execution (fallback / testing)
            execution = execute_trade("buy", signal["price"], cash=pf.cash)
            pf.enter_position(ASSET, execution["side"], execution["size"],
                              execution["fill_price"])
            print(f"  [PAPER] ENTERED {ASSET} {execution['side']} "
                  f"{execution['size']:.4f} @ ${execution['fill_price']:.2f} "
                  f"(cost ${execution['cost']:.2f})")
        else:
            # Alpaca execution
            order, err = alpaca_buy(broker, signal["price"], pf.cash)
            if err:
                print(f"  Alpaca order failed: {err}")
                print("  No trade placed. Use --paper to test with paper executor.")
            elif order:
                fill_price = float(order.get("filled_avg_price") or signal["price"])
                filled_qty = int(float(order.get("filled_qty") or order.get("qty", 0)))
                if filled_qty > 0:
                    pf.enter_position(ASSET, "long", filled_qty, fill_price)
                    print(f"  [ALPACA] ENTERED {ASSET} long {filled_qty} "
                          f"@ ${fill_price:.2f} (order: {order.get('id', '?')[:8]})")
                else:
                    print(f"  Order placed but not yet filled. "
                          f"Check Alpaca for order {order.get('id', '?')[:8]}")

    elif signal["action"] == "sell":
        if not any(p.asset == ASSET for p in pf.positions):
            print(f"  No {ASSET} position to exit.")
        elif args.paper:
            # Paper exit
            pnl = pf.exit_position(ASSET, signal["price"])
            tag = "PROFIT" if pnl > 0 else "LOSS"
            print(f"  [PAPER] EXITED {ASSET} → {tag} ${abs(pnl):.2f}")
        else:
            # Alpaca exit
            result, err = alpaca_sell(broker)
            if err:
                print(f"  Alpaca close failed: {err}")
            else:
                # Sync exit price from Alpaca if available, else use current price
                exit_price = signal["price"]
                pnl = pf.exit_position(ASSET, exit_price)
                tag = "PROFIT" if pnl > 0 else "LOSS"
                print(f"  [ALPACA] EXITED {ASSET} → {tag} ${abs(pnl):.2f}")
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
