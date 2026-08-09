#!/usr/bin/env python3
"""
Portfolio Manager — main loop.

Routes trades through Alpaca broker (paper or live). The WHAT (strategy,
instrument, timeframe) comes from config.json; CLI flags override for
one-off runs. Every cycle appends to the permanent audit journal and
pushes a state snapshot to Forge for the control-room visual.

Usage:
    python run.py                      # one cycle from config.json
    python run.py --paper              # force internal paper executor
    python run.py --strategy macd-trend-rider.yaml --ticker GLD --interval 4h
    python run.py --summary            # just show current state
    python run.py --reset              # reset portfolio to $10,000
    python run.py --alpaca             # show Alpaca account status
    python run.py --prices             # show live prices
    python run.py --broker             # sync Alpaca positions → portfolio state
"""

import argparse
import json
import sys
import urllib.request

from portfolio.state import PortfolioState
from portfolio.signal_ingest import latest_signal
from portfolio.strategy import load_strategy, compute_signal
from portfolio.executor import execute_trade
from portfolio.reporter import generate_report, save_report
from portfolio.prices import PriceFeed
from portfolio.broker import AlpacaBroker
from portfolio.config import load as load_config, describe as describe_config, sync_from_forge
from portfolio.journal import append as journal_append, read_trades, read_equity


def push_to_forge(cfg: dict, pf: PortfolioState, strategy_name: str, source: str):
    """Push a state snapshot to Forge so the control-room visual stays in sync."""
    if not cfg.get("forge_push"):
        return
    url = cfg["forge_url"].rstrip("/") + "/api/portfolio/state"
    payload = {
        "config": {k: cfg.get(k) for k in ("strategy", "ticker", "interval", "period", "asset", "mode")},
        "strategy_name": strategy_name,
        "source": source,
        "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "portfolio": pf.to_dict(),
        "trades": read_trades(200),
        "equity_curve": read_equity(1000),
    }
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"  [forge] state pushed → {url} ({resp.status})")
    except Exception as e:
        print(f"  [forge] push failed: {e}")


def main():
    cfg = load_config()

    # Forge is the control room: pull the desired config from Forge first.
    # Edits made in the Forge UI are applied here, this cycle.
    cfg = sync_from_forge(cfg)

    parser = argparse.ArgumentParser(description="Portfolio Manager")
    parser.add_argument("--summary", action="store_true", help="Show current state only")
    parser.add_argument("--reset", action="store_true", help="Reset portfolio to $10,000")
    parser.add_argument("--prices", action="store_true", help="Show latest prices only")
    parser.add_argument("--alpaca", action="store_true", help="Show Alpaca account status")
    parser.add_argument("--broker", action="store_true", help="Sync Alpaca positions → portfolio state")
    parser.add_argument("--paper", action="store_true", help="Use internal paper executor instead of Alpaca")
    parser.add_argument("--config", action="store_true", help="Show effective configuration and exit")
    parser.add_argument("--strategy", default=None, help="Override strategy YAML (path or examples/ filename)")
    parser.add_argument("--ticker", default=None, help="Override data ticker (must match traded asset)")
    parser.add_argument("--interval", default=None, help="Override data interval")
    parser.add_argument("--period", default=None, help="Override lookback period")
    parser.add_argument("--asset", default=None, help="Override traded asset symbol")
    args = parser.parse_args()

    # Apply CLI overrides on top of config
    for key in ("strategy", "ticker", "interval", "period", "asset"):
        val = getattr(args, key)
        if val:
            cfg[key] = val
    if args.paper:
        cfg["mode"] = "paper"

    ASSET = cfg["asset"]
    strategy_name = cfg.get("strategy", "")

    if args.config:
        print("=" * 60)
        print("  EFFECTIVE CONFIGURATION")
        print("=" * 60)
        print(describe_config(cfg))
        return

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
        prices = feed.get_prices(["SPY", "QQQ", ASSET, "C:XAUUSD"])
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
    mode = cfg["mode"].upper()
    print("=" * 60)
    print(f"  PORTFOLIO MANAGER — RUN CYCLE  [{mode}]")
    print("=" * 60)
    print(describe_config(cfg))

    # Fetch current prices first — the FEED uses the yfinance ticker
    # (cfg["ticker"], e.g. BTC-USD). cfg["asset"] (e.g. BTCUSD) is the
    # Alpaca broker symbol and is NOT a valid yfinance ticker.
    print()
    print("  Current prices:")
    prices = feed.get_prices(["SPY", cfg["ticker"]])
    print(feed.summary())
    print()

    # Mark existing positions with latest prices
    pf.mark_prices(prices)
    print(f"  Equity: ${pf.equity:.2f} | Cash: ${pf.cash:.2f}")
    print()

    # Get signal — from a Forge strategy (config) or the VT pipeline
    print("  [1/3] Reading signal...")
    signal = None
    source = ""
    use_forge = bool(cfg.get("strategy"))
    if use_forge:
        try:
            strategy = load_strategy(cfg["strategy"])
            strategy_name = strategy.name
            source = f"forge:{strategy.name}"
            print(f"  Strategy: {strategy.name} ({cfg['ticker']} {cfg['interval']})")
            signal = compute_signal(strategy, cfg["ticker"], cfg["interval"], cfg["period"], ASSET)
        except Exception as e:
            print(f"  Forge strategy failed: {e}")
            signal = None
    else:
        signal = latest_signal()
        source = signal.get("source", "vt_pipeline") if signal else ""
    if signal is None:
        print("  No signal available. Holding current positions.")
        journal_append(pf, cfg, strategy_name, source)
        push_to_forge(cfg, pf, strategy_name, source)
        print(generate_report(pf))
        return

    # Use live price from feed — keyed by the yfinance ticker. The strategy's
    # own computed price (same ticker) is already correct; the feed value is
    # just the freshest close, applied when available.
    asset_price = prices.get(cfg["ticker"], 0)
    if asset_price > 0:
        signal["price"] = asset_price

    print(f"  Signal: {signal['action']} | Confidence: {signal['confidence']:.1%} | Price: ${signal['price']:.2f}")
    print()

    # Meta stamped on every trade entry for the audit trail
    meta = {
        "strategy": strategy_name,
        "ticker": cfg.get("ticker", ""),
        "interval": cfg.get("interval", ""),
        "source": source,
    }

    # Step 2: Decide whether to trade
    print("  [2/3] Evaluating trade...")
    if signal["action"] == "hold":
        print(f"  {strategy_name or 'VT'} says HOLD. No action taken.")

    elif signal["action"] == "buy":
        if any(p.asset == ASSET for p in pf.positions):
            print(f"  Already in {ASSET} position. Holding.")
        elif cfg["mode"] == "paper":
            # Paper execution (fallback / testing)
            execution = execute_trade("buy", signal["price"], cash=pf.cash)
            pf.enter_position(ASSET, execution["side"], execution["size"],
                              execution["fill_price"], meta=meta)
            print(f"  [PAPER] ENTERED {ASSET} {execution['side']} "
                  f"{execution['size']:.4f} @ ${execution['fill_price']:.2f} "
                  f"(cost ${execution['cost']:.2f})")
        else:
            # Alpaca execution
            order, err = alpaca_buy(broker, signal["price"], pf.cash, ASSET)
            if err:
                print(f"  Alpaca order failed: {err}")
                print("  No trade placed. Use --paper to test with paper executor.")
            elif order:
                fill_price = float(order.get("filled_avg_price") or signal["price"])
                filled_qty = int(float(order.get("filled_qty") or order.get("qty", 0)))
                if filled_qty > 0:
                    pf.enter_position(ASSET, "long", filled_qty, fill_price, meta=meta)
                    print(f"  [ALPACA] ENTERED {ASSET} long {filled_qty} "
                          f"@ ${fill_price:.2f} (order: {order.get('id', '?')[:8]})")
                else:
                    print(f"  Order placed but not yet filled. "
                          f"Check Alpaca for order {order.get('id', '?')[:8]}")

    elif signal["action"] == "sell":
        if not any(p.asset == ASSET for p in pf.positions):
            print(f"  No {ASSET} position to exit.")
        elif cfg["mode"] == "paper":
            # Paper exit
            pnl = pf.exit_position(ASSET, signal["price"], meta=meta)
            tag = "PROFIT" if pnl > 0 else "LOSS"
            print(f"  [PAPER] EXITED {ASSET} → {tag} ${abs(pnl):.2f}")
        else:
            # Alpaca exit
            result, err = alpaca_sell(broker, ASSET)
            if err:
                print(f"  Alpaca close failed: {err}")
            else:
                exit_price = signal["price"]
                pnl = pf.exit_position(ASSET, exit_price, meta=meta)
                tag = "PROFIT" if pnl > 0 else "LOSS"
                print(f"  [ALPACA] EXITED {ASSET} → {tag} ${abs(pnl):.2f}")
    print()

    # Step 3: Report + audit trail + Forge sync
    print("  [3/3] Generating report...")
    report = generate_report(pf)
    pf.save()
    save_report(report)
    journal_append(pf, cfg, strategy_name, source)
    push_to_forge(cfg, pf, strategy_name, source)
    print()
    print(report)


def alpaca_buy(broker, price, cash, asset):
    """Place a buy order through Alpaca. Returns (order, error).

    Uses a notional (dollar-amount) order so high-priced assets like BTC
    size exactly — a whole-share qty would round $100 risk up to 1 BTC
    (~$65k) and blow the account.
    """
    risk_amount = round(cash * 0.01, 2)  # 1% of cash per trade
    print(f"  Alpaca: buying ${risk_amount:.2f} of {asset} @ market (${price:.2f})")
    order = broker.market_order(asset, side="buy", notional=risk_amount)
    if "error" in order:
        return None, order["error"]
    return order, None


def alpaca_sell(broker, asset):
    """Close a position through Alpaca. Returns (result, error)."""
    print(f"  Alpaca: closing {asset} position")
    result = broker.close_position(asset)
    if isinstance(result, dict) and "error" in result:
        return None, result["error"]
    return result, None


if __name__ == "__main__":
    main()
