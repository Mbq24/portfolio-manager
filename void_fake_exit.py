#!/usr/bin/env python3
"""
void_fake_exit.py — Correct the Aug 23 synthetic-data exit.

The exit at $50,955.15 was executed on fabricated (synthetic-fallback) data
while DNS was down. Real BTC was ~$77-79k. This restores the account to the
truthful state: the position is re-opened at its real entry, cash is restored,
and the journal gets a VOID marker (history preserved, correction recorded).

Restores:
  - cash:      back to pre-exit value (removes fake proceeds)
  - position:  BTCUSD long 0.0039 @ $77,345.73 (the real entry)
  - losses:    back to 1 (removes the fabricated loss)
Appends to trades.csv a VOID row documenting the correction.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from portfolio.state import PortfolioState, Position  # noqa: E402
from portfolio.prices import PriceFeed  # noqa: E402

DATA_DIR = Path(__file__).parent / "data"
TRADES_CSV = DATA_DIR / "trades.csv"
EQUITY_CSV = DATA_DIR / "equity_curve.csv"

ENTRY_PRICE = 77345.73
SIZE = 0.0039
FAKE_EXIT_PRICE = 50955.14978560619
ASSET = "BTCUSD"
TICKER = "BTC-USD"
ENTRY_TIME = "2026-08-23T18:14:00.146439+00:00"
ENTRY_CSV_ROW = ("2026-08-23T18:14:00.146439+00:00,enter,BTCUSD,long,0.0039,"
                 "77345.73,301.65,,Vol Expansion Momentum,BTC-USD,1h,"
                 "forge:Vol Expansion Momentum,paper")


def main():
    pf = PortfolioState.load()

    # 1. Sanity-check we're in the expected state
    assert len(pf.positions) == 0, f"Expected flat, found {len(pf.positions)} positions"
    assert pf.cash == 9895.92, f"Expected cash 9895.92, found {pf.cash}"
    assert pf.losses == 2, f"Expected 2 losses, found {pf.losses}"
    fake_exits = [t for t in pf.trade_log
                  if t.get("type") == "exit" and abs(t.get("pnl", 0) - (-102.92)) < 0.01]
    assert fake_exits, "Fake exit not found in trade_log"

    # 2. Reverse the fake exit proceeds
    fake_proceeds = SIZE * FAKE_EXIT_PRICE
    pf.cash = round(pf.cash - fake_proceeds, 2)

    # 3. Drop the fake exit from the in-memory trade log (history stays in CSV)
    pf.trade_log = [t for t in pf.trade_log
                    if not (t.get("type") == "exit"
                            and abs(t.get("pnl", 0) - (-102.92)) < 0.01)]

    # 4. Re-open the position at its real entry
    pos = Position(ASSET, "long", SIZE, ENTRY_PRICE, ticker=TICKER)
    pos.entry_time = ENTRY_TIME
    # Mark at live price if available (next hourly cycle re-marks anyway)
    try:
        feed = PriceFeed()
        live = feed.get_price(TICKER)
        if live > 0:
            pos.current_price = live
            pos.unrealized_pnl = round((live - ENTRY_PRICE) * SIZE, 2)
    except Exception as e:
        print(f"  (live mark failed, using entry price: {e})")
    pf.positions.append(pos)

    # 5. Fix counters: the fabricated loss is void
    pf.losses -= 1

    # 6. Append a VOID equity point documenting the correction
    pf.equity_curve.append({
        "time": datetime.now(timezone.utc).isoformat(),
        "equity": round(pf.equity, 2),
    })

    pf.save()

    # 7. Append VOID marker to the append-only journal
    void_row = (f"{datetime.now(timezone.utc).isoformat()},VOID,BTCUSD,long,"
                f"{SIZE},,,+102.92,Vol Expansion Momentum,BTC-USD,1h,"
                f"forge:void-synthetic-exit,paper")
    with open(TRADES_CSV, "a") as f:
        f.write(void_row + "\n")
    with open(EQUITY_CSV, "a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()},{round(pf.equity, 2)},{round(pf.equity, 2)},0\n")

    print("=" * 60)
    print("  VOID COMPLETE — synthetic exit corrected")
    print("=" * 60)
    print(f"  cash restored:      ${pf.cash:.2f}")
    print(f"  position re-opened: {SIZE} {ASSET} @ ${ENTRY_PRICE:.2f}")
    print(f"  unrealized P&L:     ${pos.unrealized_pnl:+.2f} (marked at ${pos.current_price:.2f})")
    print(f"  equity:             ${pf.equity:.2f}")
    print(f"  wins/losses:        {pf.wins} / {pf.losses}")
    print()
    print(pf.summary())
    print()
    print("  VOID row appended to trades.csv (history preserved, correction marked)")


if __name__ == "__main__":
    main()
