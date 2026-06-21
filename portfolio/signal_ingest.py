"""
portfolio/signal_ingest.py — Read VT pipeline output as trade signals.

VT pipeline writes results to:
  ~/VisualStudioProjects/VOLATILITY-TRADER/results/gold_1h_meanrev_backtest.json

This module picks up the latest signal and converts it into a structured
trade decision the portfolio manager can act on.
"""

import json
from pathlib import Path

VT_RESULTS_DIR = Path.home() / "VisualStudioProjects" / "VOLATILITY-TRADER" / "results"


def latest_signal() -> dict | None:
    """Read the most recent VT backtest result and extract a signal.

    Returns a dict with:
      - asset: str (e.g. "GOLD")
      - action: "buy" | "sell" | "hold"
      - confidence: float (0-1)
      - price: float (latest close or entry price)
      - timestamp: str (ISO 8601)
      - regime: str (e.g. "trending", "ranging")
      - source: str ("vt_pipeline")

    Returns None if no valid signal found.
    """
    if not VT_RESULTS_DIR.exists():
        print(f"  VT results dir not found: {VT_RESULTS_DIR}")
        return None

    # Find the latest JSON result file
    json_files = sorted(VT_RESULTS_DIR.glob("*backtest*.json"))
    if not json_files:
        print(f"  No VT backtest results found in {VT_RESULTS_DIR}")
        return None

    latest = json_files[-1]
    try:
        data = json.loads(latest.read_text())
    except (json.JSONDecodeError, IOError) as e:
        print(f"  Failed to parse {latest}: {e}")
        return None

    # Extract signal from VT output structure
    # VT pipeline outputs flat metrics with fold_details array
    if "avg_win_rate" not in data and "fold_details" not in data:
        print(f"  No trade data in VT result")
        return None

    # Get aggregate signal metrics
    avg_confidence = data.get("avg_win_rate", 0.0)
    mean_accuracy = data.get("mean_accuracy", 0.0)

    # Regime / market state (not currently exported by VT, default to unknown)
    regime = "unknown"

    # No price data in VT output — use 0, will need market data feed later
    price = 0.0

    # Determine action based on validation gate and metrics (without price gate)
    # Price is set to 0 by VT — run.py overrides it with live feed before acting
    validation_passed = data.get("validation_passed", False)
    action = "hold"
    if validation_passed and mean_accuracy > 0.50 and avg_confidence > 0.50:
        action = "buy"
    elif not validation_passed:
        action = "hold"  # gate blocked it — don't trade

    signal = {
        "asset": "GOLD",
        "action": action,
        "confidence": round(avg_confidence, 4),
        "price": price,
        "accuracy": round(mean_accuracy, 4),
        "validation_passed": validation_passed,
        "regime": regime,
        "source": f"vt_pipeline:{latest.stem}",
        "timestamp": data.get("timestamp", str(latest.stat().st_mtime)),
    }

    print(f"  VT signal: {signal['action']} "
          f"(validation={validation_passed}, accuracy={mean_accuracy:.1%}, "
          f"confidence={avg_confidence:.1%})")
    return signal
