"""
portfolio/config.py — Runtime configuration for the portfolio manager.

This is the single source of truth for WHAT is being executed:
which Forge strategy, on which instrument, at which timeframe, in
which mode (paper vs live). Edit this file to change what runs.

    python run.py            # uses config.json
    python run.py --paper    # same config, force internal paper executor
"""

import json
from pathlib import Path

CONFIG_FILE = Path(__file__).parent.parent / "config.json"

DEFAULTS = {
    # Which Forge strategy to execute (filename in Forge/examples/ or absolute path)
    "strategy": "volatility-squeeze.yaml",
    # Instrument + timeframe — MUST be the same asset you trade (signal == execution)
    "ticker": "GLD",
    "interval": "1h",
    "period": "7d",
    # Asset symbol traded through the broker (GLD ETF on Alpaca)
    "asset": "GLD",
    # "paper" uses the internal executor; anything else goes through the broker
    "mode": "paper",
    # Push state snapshot to Forge so the control-room visual stays in sync
    "forge_push": True,
    "forge_url": "https://forge-production-0c60.up.railway.app",
}


def load() -> dict:
    """Load config.json merged over defaults. Missing keys fall back to defaults."""
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text()))
        except (json.JSONDecodeError, IOError) as e:
            print(f"  [config] Failed to parse {CONFIG_FILE}: {e} — using defaults")
    return cfg


def save(cfg: dict):
    """Persist config (merged over defaults) to config.json."""
    merged = dict(DEFAULTS)
    merged.update(cfg)
    CONFIG_FILE.write_text(json.dumps(merged, indent=2))
    print(f"  [config] Saved → {CONFIG_FILE}")


def describe(cfg: dict) -> str:
    """Human-readable summary of what's configured to run."""
    return (
        f"  strategy : {cfg['strategy']}\n"
        f"  ticker   : {cfg['ticker']} ({cfg['interval']}, {cfg['period']})\n"
        f"  asset    : {cfg['asset']}\n"
        f"  mode     : {cfg['mode']}\n"
        f"  forge    : {'push to ' + cfg['forge_url'] if cfg['forge_push'] else 'disabled'}"
    )


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--show":
        print(describe(load()))
    elif len(sys.argv) > 1 and sys.argv[1] == "--reset":
        save(dict(DEFAULTS))
        print("  [config] Reset to defaults")
    else:
        print("Usage: python -m portfolio.config --show | --reset")
