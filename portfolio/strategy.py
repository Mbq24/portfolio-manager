"""
portfolio/strategy.py — Run a Forge DSL strategy on live data.

Imports Forge's compute engine (the SAME code the harness validates with)
so the strategy that passed z>=1.5 in the lab computes signals identically
in production.

Produces a signal dict compatible with signal_ingest.latest_signal(), so
run.py can consume Forge strategies without changing its decision/execute
logic:

    {
        "asset": "GLD",
        "action": "buy" | "sell" | "hold",
        "confidence": float,
        "price": float,
        "regime": "trending" | "volatile" | "ranging",
        "source": "forge:<dsl-name>",
        "timestamp": ISO 8601,
    }
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

FORGE_ROOT = (
    Path.home()
    / "Library"
    / "Mobile Documents"
    / "com~apple~CloudDocs"
    / "VisualStudioProjects"
    / "Forge"
)
if str(FORGE_ROOT) not in sys.path:
    sys.path.insert(0, str(FORGE_ROOT))

import yaml  # noqa: E402

from dsl.schema import IndicatorDSL, IndicatorDef, CompoundIndicator, SignalDef  # noqa: E402
from generators.local import compute_indicators  # noqa: E402
from tradingview.data_fetcher import fetch_ohlcv  # noqa: E402


def load_strategy(yaml_path: str | Path) -> IndicatorDSL:
    """Load a Forge DSL definition from a YAML file."""
    path = Path(yaml_path)
    if not path.exists():
        # Fall back to Forge's examples dir
        candidate = FORGE_ROOT / "examples" / path.name
        if candidate.exists():
            path = candidate
        else:
            raise FileNotFoundError(f"Strategy not found: {yaml_path} (also tried {candidate})")

    raw = yaml.safe_load(path.read_text())
    indicators = [
        IndicatorDef(id=i["id"], type=i["type"], params=i.get("params", {}))
        for i in raw.get("indicators", [])
    ]
    compounds = [
        CompoundIndicator(id=c["id"], type=c["type"], params=c.get("params", {}))
        for c in raw.get("compounds", [])
    ]
    signals = {}
    for k, v in (raw.get("signals") or {}).items():
        if isinstance(v, str):
            signals[k] = SignalDef(condition=v)
        elif isinstance(v, dict) and "condition" in v:
            signals[k] = SignalDef(condition=v["condition"])

    return IndicatorDSL(
        name=raw.get("name", path.stem),
        description=raw.get("description", ""),
        timeframe=raw.get("timeframe", "1h"),
        indicators=indicators,
        compounds=compounds,
        patterns=raw.get("patterns", []),
        signals=signals,
    )


def _label_regime(df) -> str:
    """Mirror the harness regime heuristic so live labels match lab labels."""
    c = df["close"]
    diffs = c.diff().dropna()
    trend_strength = abs(diffs.mean()) / diffs.std() if diffs.std() > 0 else 0.0
    atr_pct = (df["high"] - df["low"]).mean() / c.mean()
    if trend_strength > 0.15:
        return "trending"
    if atr_pct > 0.02:
        return "volatile"
    return "ranging"


def compute_signal(
    dsl: IndicatorDSL,
    ticker: str = "GC=F",
    interval: str = "1h",
    period: str = "7d",
    asset: str = "GLD",
) -> dict | None:
    """Compute the strategy on the latest data and return a trade signal.

    Acts on the LAST CLOSED BAR only — that's the honest live semantics
    (no peeking at the forming bar, same as what the harness backtest does).

    Returns None if data can't be fetched or the strategy has no signals.
    """
    df = fetch_ohlcv(ticker, interval=interval, period=period)
    if df is None or df.empty or len(df) < 20:
        print(f"  [strategy] Not enough data for {ticker} ({0 if df is None else len(df)} bars)")
        return None

    result = compute_indicators(df, dsl)

    entry_cols = [c for c in result.columns if c.startswith("signal_")]
    if not entry_cols:
        print("  [strategy] Strategy defines no signals")
        return None

    # Latest closed bar
    last = result.iloc[-1]
    price = float(last["close"])
    ts = datetime.now(timezone.utc).isoformat()

    # Exit takes precedence over entry (safety first)
    action = "hold"
    confidence = 0.5
    if "signal_exit" in result.columns and bool(last["signal_exit"]):
        action = "sell"
        confidence = 0.9
    elif "signal_entry" in result.columns and bool(last["signal_entry"]):
        action = "buy"
        confidence = 0.8

    return {
        "asset": asset,
        "action": action,
        "confidence": confidence,
        "price": round(price, 2),
        "regime": _label_regime(result),
        "source": f"forge:{dsl.name}",
        "timestamp": ts,
        "ticker": ticker,
        "interval": interval,
    }


if __name__ == "__main__":
    # CLI: python -m portfolio.strategy [yaml_path] [ticker] [interval]
    import argparse

    parser = argparse.ArgumentParser(description="Compute a Forge strategy signal")
    parser.add_argument("yaml", nargs="?", default="volatility-squeeze.yaml", help="Strategy YAML (path or examples/ filename)")
    parser.add_argument("--ticker", default="GC=F")
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--period", default="7d")
    parser.add_argument("--asset", default="GLD")
    args = parser.parse_args()

    strategy = load_strategy(args.yaml)
    sig = compute_signal(strategy, args.ticker, args.interval, args.period, args.asset)
    if sig is None:
        print("No signal (data or strategy problem)")
    else:
        print(f"Strategy: {strategy.name}")
        print(f"Signal:   {sig['action'].upper()} | confidence={sig['confidence']:.1f} | regime={sig['regime']}")
        print(f"Price:    ${sig['price']:.2f} ({sig['ticker']} {sig['interval']})")
        print(f"Source:   {sig['source']}")
