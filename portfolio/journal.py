"""
portfolio/journal.py — Append-only audit trail of every trade and equity point.

Unlike state.json (capped at 50 trades) and latest_report.txt (overwritten),
these files grow forever and are never truncated. They are the permanent
record of what the portfolio manager actually executed, with full strategy
attribution so you can audit which Forge strategy produced each trade.

    data/trades.csv          — one row per enter/exit
    data/equity_curve.csv    — one row per cycle (equity snapshot)

Usage: journal.append(pf, cfg)  — call after a run cycle, before save.
"""

import csv
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
TRADES_FILE = DATA_DIR / "trades.csv"
EQUITY_FILE = DATA_DIR / "equity_curve.csv"

TRADES_HEADER = [
    "time", "type", "asset", "side", "size", "price", "cost", "pnl",
    "strategy", "ticker", "interval", "source", "mode",
]


def _ensure_header(path: Path, header: list[str]):
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(header)


def append(pf, cfg: dict, strategy_name: str = "", source: str = ""):
    """Append any new trade-log entries + a fresh equity snapshot to CSV.

    Dedup: entries already present in trades.csv (same time+type+asset) are
    skipped, so re-running a cycle never double-records.
    """
    _ensure_header(TRADES_FILE, TRADES_HEADER)
    _ensure_header(EQUITY_FILE, ["time", "equity", "cash", "position_value"])

    # Known rows from the existing CSV (time|type|asset)
    seen = set()
    if TRADES_FILE.exists():
        with open(TRADES_FILE, newline="") as f:
            for row in csv.DictReader(f):
                seen.add((row.get("time", ""), row.get("type", ""), row.get("asset", "")))

    new_rows = []
    for t in pf.trade_log:
        key = (t.get("time", ""), t.get("type", ""), t.get("asset", ""))
        if key in seen:
            continue
        seen.add(key)
        new_rows.append({
            "time": t.get("time", ""),
            "type": t.get("type", ""),
            "asset": t.get("asset", ""),
            "side": t.get("side", ""),
            "size": t.get("size", 0),
            "price": t.get("price", t.get("exit_price", 0)),
            "cost": t.get("cost", ""),
            "pnl": t.get("pnl", ""),
            "strategy": t.get("strategy", strategy_name),
            "ticker": t.get("ticker", cfg.get("ticker", "")),
            "interval": t.get("interval", cfg.get("interval", "")),
            "source": t.get("source", source),
            "mode": cfg.get("mode", ""),
        })

    if new_rows:
        with open(TRADES_FILE, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TRADES_HEADER)
            writer.writerows(new_rows)
        print(f"  [journal] {len(new_rows)} new trade(s) → {TRADES_FILE}")

    # Equity snapshot every cycle (append-only, one row per run)
    now = datetime.now(timezone.utc).isoformat()
    with open(EQUITY_FILE, "a", newline="") as f:
        csv.writer(f).writerow([
            now, round(pf.equity, 2), round(pf.cash, 2), round(pf.position_value, 2),
        ])


def read_trades(limit: int = 200) -> list[dict]:
    """Read most recent trades, newest first."""
    if not TRADES_FILE.exists():
        return []
    with open(TRADES_FILE, newline="") as f:
        rows = list(csv.DictReader(f))
    return list(reversed(rows[-limit:]))


def read_equity(limit: int = 1000) -> list[dict]:
    """Read equity curve, oldest first."""
    if not EQUITY_FILE.exists():
        return []
    with open(EQUITY_FILE, newline="") as f:
        rows = list(csv.DictReader(f))
    return rows[-limit:]
