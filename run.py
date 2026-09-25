#!/usr/bin/env python3
"""
Portfolio Manager — main loop.

Routes trades through Alpaca broker (paper or live). The WHAT (strategy,
instruments, timeframe) comes from config.json; CLI flags override for
one-off runs. Every cycle appends to the permanent audit journal and
pushes a state snapshot to Forge for the control-room visual.

Multi-instrument: every enabled entry in `cfg["instruments"]` is evaluated in
the same cycle against ONE shared PortfolioState. Positions are always resolved
by `asset`, never by index or "the first position" — an exit signal for one
instrument must never touch another instrument's position.

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
from portfolio.executor import execute_trade, size_for_budget
from portfolio.allocation import book_snapshot, can_enter
from portfolio.reporter import generate_report, save_report
from portfolio.prices import PriceFeed
from portfolio.broker import AlpacaBroker
from portfolio.config import (load as load_config, describe as describe_config,
                              sync_from_forge, enabled_instruments, normalize)
from portfolio.journal import append as journal_append, read_trades, read_equity


def push_to_forge(cfg: dict, pf: PortfolioState, strategy_name: str, source: str):
    """Push a state snapshot to Forge so the control-room visual stays in sync."""
    if not cfg.get("forge_push"):
        return
    url = cfg["forge_url"].rstrip("/") + "/api/portfolio/state"
    payload = {
        "config": {k: cfg.get(k) for k in ("mode", "max_total_exposure_pct",
                                           "max_class_exposure_pct")},
        "instruments": enabled_instruments(cfg),
        "strategy_name": strategy_name,
        "source": source,
        "updated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(),
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


def evaluate_instrument(inst: dict, cfg: dict, pf: PortfolioState, prices: dict,
                        asset_classes: dict):
    """Run one instrument's signal → decision → execution. Returns (acted, strategy_name, source).

    Never raises on a per-instrument failure: one dead ticker must not stop the other
    instruments in the same cycle.
    """
    inst_cfg = {**cfg, **inst}
    asset = inst_cfg["asset"]
    ticker = inst_cfg["ticker"]
    strategy_name = ""
    source = ""

    pos = next((p for p in pf.positions if p.asset == asset), None)
    in_position = pos is not None

    print(f"  ── {ticker} ({asset}) class={inst_cfg.get('asset_class') or '-'}"
          f"{'  [IN POSITION]' if in_position else ''}")

    try:
        strategy = load_strategy(inst_cfg["strategy"])
        strategy_name = strategy.name
        source = f"forge:{strategy.name}"
        signal = compute_signal(
            strategy, ticker, inst_cfg["interval"], inst_cfg["period"], asset,
            exit_lookback_bars=int(cfg.get("exit_lookback_bars", 6)) if in_position else 0,
            entry_time=pos.entry_time if in_position else "",
        )
    except Exception as e:
        print(f"     strategy failed: {e}")
        return False, strategy_name, source

    if signal is None:
        print("     no signal available — holding")
        return False, strategy_name, source

    price = prices.get(ticker, 0)
    if price > 0:
        signal["price"] = price
    print(f"     signal: {signal['action'].upper()} @ ${signal['price']:.2f} "
          f"(conf {signal['confidence']:.1%})")

    meta = {"strategy": strategy_name, "ticker": ticker,
            "interval": inst_cfg["interval"], "source": source}

    if signal["action"] == "hold":
        print(f"     {strategy_name} says HOLD")
        return False, strategy_name, source

    # ---------------------------------------------------------------- BUY
    if signal["action"] == "buy":
        if in_position:
            print(f"     already long {asset} — holding")
            return False, strategy_name, source

        budget = pf.equity * float(inst_cfg["risk_pct"]) / 100.0
        book = book_snapshot(pf, asset_classes)
        ok, why = can_enter(book, asset, inst_cfg.get("asset_class", ""), budget,
                            cfg["max_total_exposure_pct"], cfg["max_class_exposure_pct"])
        if not ok:
            print(f"     BLOCKED entry: {why}")
            return False, strategy_name, source

        if cfg["mode"] == "paper":
            size = size_for_budget(signal["price"], budget, available_cash=pf.cash)
            if size <= 0:
                print("     sized to zero — no action")
                return False, strategy_name, source
            execution = execute_trade("buy", signal["price"], size=size)
            pf.enter_position(asset, execution["side"], execution["size"],
                              execution["fill_price"], meta=meta)
            print(f"     [PAPER] ENTERED {asset} {execution['side']} "
                  f"{execution['size']:.6f} @ ${execution['fill_price']:.2f} "
                  f"(budget ${budget:.2f}, cost ${execution['cost']:.2f})")
            return True, strategy_name, source

        order, err = alpaca_buy(broker=None, price=signal["price"], notional=budget, asset=asset)
        if err or not order:
            print(f"     Alpaca order failed: {err or 'no order returned'}")
            return False, strategy_name, source
        fill_price = float(order.get("filled_avg_price") or signal["price"])
        filled_qty = int(float(order.get("filled_qty") or order.get("qty", 0)))
        if filled_qty > 0:
            pf.enter_position(asset, "long", filled_qty, fill_price, meta=meta)
            print(f"     [ALPACA] ENTERED {asset} long {filled_qty} @ ${fill_price:.2f}")
            return True, strategy_name, source
        print(f"     order placed, not yet filled ({str(order.get('id', '?'))[:8]})")
        return False, strategy_name, source

    # --------------------------------------------------------------- SELL
    if signal["action"] == "sell":
        if not in_position:
            print(f"     no {asset} position to exit")
            return False, strategy_name, source

        # Sanity guard: refuse to exit at a price absurdly far from entry. A >30% move in one
        # cycle is not a real market move — it means the price feed is contaminated (synthetic
        # fallback, bad ticker, stale data). Exit on garbage prices books fake P&L.
        if pos.entry_price > 0:
            deviation = abs(signal["price"] - pos.entry_price) / pos.entry_price
            if deviation > 0.30:
                print(f"     ⚠ SANITY GUARD: ${signal['price']:.2f} is {deviation:.1%} from "
                      f"entry ${pos.entry_price:.2f} — refusing exit (contaminated feed?)")
                return False, strategy_name, source

        if cfg["mode"] == "paper":
            pnl = pf.exit_position(asset, signal["price"], meta=meta)
            tag = "PROFIT" if pnl > 0 else "LOSS"
            print(f"     [PAPER] EXITED {asset} → {tag} ${abs(pnl):.2f}")
            return True, strategy_name, source

        result, err = alpaca_sell(asset)
        if err:
            print(f"     Alpaca close failed: {err}")
            return False, strategy_name, source
        pnl = pf.exit_position(asset, signal["price"], meta=meta)
        tag = "PROFIT" if pnl > 0 else "LOSS"
        print(f"     [ALPACA] EXITED {asset} → {tag} ${abs(pnl):.2f}")
        return True, strategy_name, source

    return False, strategy_name, source


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
    parser.add_argument("--risk-pct", type=float, default=None, help="Override position size % (e.g. 2.0 = 2% exposure per trade)")
    args = parser.parse_args()

    # Apply CLI overrides. With multiple instruments a ticker/asset override only makes sense
    # for a single-instrument run, so it collapses the book to one instrument.
    overrides = {k: getattr(args, k) for k in ("strategy", "ticker", "interval", "period", "asset")
                 if getattr(args, k)}
    if overrides:
        if len(enabled_instruments(cfg)) > 1 and ("ticker" in overrides or "asset" in overrides):
            print("  [config] ticker/asset override given with multiple instruments — "
                  "running the first instrument only.")
            first = enabled_instruments(cfg)[0]
            cfg = normalize({**cfg, "instruments": [{**first, **overrides, "enabled": True}]})
        else:
            for k, v in overrides.items():
                cfg[k] = v
            cfg = normalize(cfg)
    if args.risk_pct is not None:
        cfg["risk_pct"] = args.risk_pct
        for i in cfg["instruments"]:
            i["risk_pct"] = args.risk_pct
    if args.paper:
        cfg["mode"] = "paper"

    insts = enabled_instruments(cfg)
    strategy_label = ", ".join(sorted({i["strategy"] for i in insts}))

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
        prices = feed.get_prices(sorted({i["ticker"] for i in insts} | {"SPY"}))
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

    # Full cycle: for each instrument → ingest signal → decide → execute → report
    mode = cfg["mode"].upper()
    print("=" * 60)
    print(f"  PORTFOLIO MANAGER — RUN CYCLE  [{mode}]")
    print("=" * 60)
    print(describe_config(cfg))

    # Fetch prices for every instrument's ticker in one call.
    tickers = sorted({i["ticker"] for i in insts} | {"SPY"})
    print()
    print(f"  Current prices ({len(tickers)}):")
    prices = feed.get_prices(tickers)
    print(feed.summary())
    print()

    pf.mark_prices(prices)
    asset_classes = {i["asset"]: i.get("asset_class", "") for i in insts}
    book = book_snapshot(pf, asset_classes)
    print(f"  Equity: ${pf.equity:.2f} | Cash: ${pf.cash:.2f} | "
          f"Exposure: ${book['total']:.2f} "
          f"({100.0 * book['total'] / max(pf.equity, 1e-9):.1f}% of equity, "
          f"cap {cfg['max_total_exposure_pct']}% / class {cfg['max_class_exposure_pct']}%)")
    by_class = ", ".join(f"{k} ${v:.2f}" for k, v in book["by_class"].items()) or "none"
    print(f"  By class: {by_class}")
    print()

    print(f"  Evaluating {len(insts)} instrument(s)...")
    acted_on = []
    names = set()
    source = ""
    for inst in insts:
        acted, sname, src = evaluate_instrument(inst, cfg, pf, prices, asset_classes)
        if sname:
            names.add(sname)
            source = src
        if acted:
            acted_on.append(inst["ticker"])
        print()

    # Report + audit trail + Forge sync (once per cycle, not once per instrument)
    print("  Generating report...")
    report = generate_report(pf)
    pf.save()
    save_report(report)
    journal_append(pf, cfg, strategy_label, source)
    push_to_forge(cfg, pf, ", ".join(sorted(names)), source)
    print()
    print(report)
    if acted_on:
        print(f"\n  Trades executed this cycle: {', '.join(acted_on)}")
    else:
        print("\n  No trades executed this cycle.")


def alpaca_buy(broker=None, price=0.0, notional=0.0, asset=""):
    """Place a notional (dollar-amount) buy order. Returns (order, error).

    Notional rather than whole-share qty, so high-priced assets like BTC size exactly — a
    whole-share qty would round a $100 risk up to 1 BTC (~$65k) and blow the account.
    """
    b = broker or AlpacaBroker(paper=True)
    print(f"  Alpaca: buying ${notional:.2f} of {asset} @ market (${price:.2f})")
    order = b.market_order(asset, side="buy", notional=round(notional, 2))
    if isinstance(order, dict) and "error" in order:
        return None, order["error"]
    return order, None


def alpaca_sell(asset):
    """Close a position through Alpaca. Returns (result, error)."""
    print(f"  Alpaca: closing {asset} position")
    b = AlpacaBroker(paper=True)
    result = b.close_position(asset)
    if isinstance(result, dict) and "error" in result:
        return None, result["error"]
    return result, None


if __name__ == "__main__":
    main()
