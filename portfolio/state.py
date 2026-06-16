"""
portfolio/state.py — Core portfolio state management.

Tracks positions, cash balance, P&L, and trade history.
Persisted to JSON so it survives restarts.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).parent.parent / "data"
STATE_FILE = DATA_DIR / "state.json"


class Position:
    """An open position."""
    asset: str
    side: str  # "long" or "short"
    size: float
    entry_price: float
    entry_time: str  # ISO 8601
    current_price: float
    unrealized_pnl: float

    def __init__(self, asset: str, side: str, size: float, entry_price: float):
        self.asset = asset
        self.side = side
        self.size = size
        self.entry_price = entry_price
        self.entry_time = datetime.now(timezone.utc).isoformat()
        self.current_price = entry_price
        self.unrealized_pnl = 0.0

    def to_dict(self) -> dict:
        return {
            "asset": self.asset,
            "side": self.side,
            "size": self.size,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time,
            "current_price": self.current_price,
            "unrealized_pnl": round(self.unrealized_pnl, 2),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        pos = cls(d["asset"], d["side"], d["size"], d["entry_price"])
        pos.entry_time = d["entry_time"]
        pos.current_price = d["current_price"]
        pos.unrealized_pnl = d["unrealized_pnl"]
        return pos


class PortfolioState:
    """The full portfolio — positions, cash, trade log, P&L."""

    def __init__(self):
        self.cash: float = 10_000.0  # starting paper capital
        self.positions: list[Position] = []
        self.trade_log: list[dict] = []
        self.equity_curve: list[dict] = []  # {time, equity}
        self.total_trades: int = 0
        self.wins: int = 0
        self.losses: int = 0

    @property
    def position_value(self) -> float:
        return sum(p.size * p.current_price for p in self.positions)

    @property
    def equity(self) -> float:
        return self.cash + self.position_value

    @property
    def win_rate(self) -> float:
        closed = self.wins + self.losses
        return self.wins / closed if closed > 0 else 0.0

    def enter_position(self, asset: str, side: str, size: float, price: float):
        cost = size * price
        if cost > self.cash:
            raise ValueError(f"Not enough cash: ${self.cash:.2f} < ${cost:.2f}")
        self.cash -= cost
        pos = Position(asset, side, size, price)
        self.positions.append(pos)
        self.total_trades += 1
        self.trade_log.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "type": "enter",
            "asset": asset,
            "side": side,
            "size": size,
            "price": price,
            "cost": round(cost, 2),
        })

    def exit_position(self, asset: str, price: float):
        for i, pos in enumerate(self.positions):
            if pos.asset == asset:
                proceeds = pos.size * price
                pnl = proceeds - (pos.size * pos.entry_price)
                self.cash += proceeds
                self.trade_log.append({
                    "time": datetime.now(timezone.utc).isoformat(),
                    "type": "exit",
                    "asset": asset,
                    "side": pos.side,
                    "size": pos.size,
                    "entry_price": pos.entry_price,
                    "exit_price": price,
                    "pnl": round(pnl, 2),
                })
                if pnl > 0:
                    self.wins += 1
                else:
                    self.losses += 1
                self.positions.pop(i)
                self._record_equity()
                return pnl
        raise ValueError(f"No open position for {asset}")

    def mark_prices(self, prices: dict[str, float]):
        """Update current prices for unrealized P&L."""
        for pos in self.positions:
            if pos.asset in prices:
                pos.current_price = prices[pos.asset]
                pos.unrealized_pnl = round(
                    (pos.current_price - pos.entry_price) * pos.size, 2
                )

    def _record_equity(self):
        self.equity_curve.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "equity": round(self.equity, 2),
        })

    def to_dict(self) -> dict:
        return {
            "cash": round(self.cash, 2),
            "equity": round(self.equity, 2),
            "position_value": round(self.position_value, 2),
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "positions": [p.to_dict() for p in self.positions],
            "trade_log": self.trade_log[-50:],  # last 50 trades
            "equity_curve": self.equity_curve[-500:],  # last 500 points
        }

    def save(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(self.to_dict(), indent=2))
        print(f"  Portfolio saved → {STATE_FILE}")

    @classmethod
    def load(cls) -> "PortfolioState":
        if not STATE_FILE.exists():
            print(f"  No state file found, starting fresh with $10,000")
            return cls()
        data = json.loads(STATE_FILE.read_text())
        pf = cls()
        pf.cash = data.get("cash", 10_000.0)
        pf.positions = [Position.from_dict(p) for p in data.get("positions", [])]
        pf.trade_log = data.get("trade_log", [])
        pf.equity_curve = data.get("equity_curve", [])
        pf.total_trades = data.get("total_trades", 0)
        pf.wins = data.get("wins", 0)
        pf.losses = data.get("losses", 0)
        return pf

    def summary(self) -> str:
        lines = [
            "╔══════════════════════════════════════╗",
            "║        PORTFOLIO SUMMARY             ║",
            "╠══════════════════════════════════════╣",
            f"  Cash:           ${self.cash:>10.2f}",
            f"  Position Value: ${self.position_value:>10.2f}",
            f"  Total Equity:   ${self.equity:>10.2f}",
            f"  Open Positions: {len(self.positions)}",
            f"  Total Trades:   {self.total_trades}",
            f"  Wins / Losses:  {self.wins} / {self.losses}",
            f"  Win Rate:       {self.win_rate:.1%}",
            "╚══════════════════════════════════════╝",
        ]
        return "\n".join(lines)
