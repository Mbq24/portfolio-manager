"""
portfolio/reporter.py — Portfolio summaries and equity curve.
"""

from pathlib import Path
from .state import PortfolioState

DATA_DIR = Path(__file__).parent.parent / "data"
REPORT_FILE = DATA_DIR / "latest_report.txt"


def _fmt_size(size: float) -> str:
    """Format position size with enough precision for small crypto quantities."""
    if size == 0:
        return "0"
    if size >= 100:
        return f"{size:.2f}"
    if size >= 1:
        return f"{size:.4f}"
    return f"{size:.6f}"


def generate_report(pf: PortfolioState) -> str:
    """Generate a full portfolio report string."""
    lines = []
    lines.append(pf.summary())
    lines.append("")

    if pf.positions:
        lines.append("Open Positions:")
        lines.append("-" * 60)
        for p in pf.positions:
            cost = p.size * p.entry_price
            lines.append(
                f"  {p.asset:6s} {p.side:5s} {_fmt_size(p.size):>10s} @ ${p.entry_price:>10.2f} "
                f"(≈${cost:>10.2f})  → ${p.current_price:>10.2f}  P&L: ${p.unrealized_pnl:>+8.2f}"
            )

    if pf.trade_log:
        recent = pf.trade_log[-5:]
        lines.append("")
        lines.append("Recent Trades:")
        lines.append("-" * 60)
        for t in reversed(recent):
            ttype = t.get("type", "?")
            if ttype == "enter":
                cost = t.get("cost")
                cost_str = f" (≈${cost:.2f})" if cost else ""
                lines.append(
                    f"  ENTER {t['asset']} {t['side']} {_fmt_size(t['size'])} "
                    f"@ ${t['price']:.2f}{cost_str}"
                )
            elif ttype == "exit":
                pnl = t.get("pnl", 0)
                tag = "✅" if pnl > 0 else "❌"
                lines.append(
                    f"  {tag} EXIT {t['asset']} {pnl:>+7.2f} "
                    f"({t['entry_price']:.2f} → {t['exit_price']:.2f})"
                )

    return "\n".join(lines)


def save_report(report: str):
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(report)
    print(f"  Report saved → {REPORT_FILE}")
