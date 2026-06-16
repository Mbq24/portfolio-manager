"""
portfolio/reporter.py — Portfolio summaries and equity curve.
"""

from pathlib import Path
from .state import PortfolioState

DATA_DIR = Path(__file__).parent.parent / "data"
REPORT_FILE = DATA_DIR / "latest_report.txt"


def generate_report(pf: PortfolioState) -> str:
    """Generate a full portfolio report string."""
    lines = []
    lines.append(pf.summary())
    lines.append("")

    if pf.positions:
        lines.append("Open Positions:")
        lines.append("-" * 60)
        for p in pf.positions:
            lines.append(
                f"  {p.asset:6s} {p.side:5s} {p.size:8.2f} @ ${p.entry_price:>8.2f} "
                f"→ ${p.current_price:>8.2f}  P&L: ${p.unrealized_pnl:>+8.2f}"
            )

    if pf.trade_log:
        recent = pf.trade_log[-5:]
        lines.append("")
        lines.append("Recent Trades:")
        lines.append("-" * 60)
        for t in reversed(recent):
            ttype = t.get("type", "?")
            if ttype == "enter":
                lines.append(
                    f"  ENTER {t['asset']} {t['side']} {t['size']:.2f} @ ${t['price']:.2f}"
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
