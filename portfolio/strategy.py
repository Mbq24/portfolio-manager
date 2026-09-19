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

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

# Forge code lives at the iCloud path on the dev Mac, or at FORGE_ROOT in the
# deployed container (Docker image bundles the needed Forge modules there).
FORGE_ROOT = Path(os.environ.get("FORGE_ROOT", str(
    Path.home()
    / "Library"
    / "Mobile Documents"
    / "com~apple~CloudDocs"
    / "VisualStudioProjects"
    / "Forge"
)))
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


_INTERVAL_MINUTES = {
    "1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30,
    "60m": 60, "1h": 60, "90m": 90,
    "1d": 1440, "5d": 7200, "1wk": 10080, "1w": 10080,
}


def _drop_forming_bar(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """Drop the still-forming (incomplete) last bar, if present.

    yfinance returns the CURRENT in-progress period as the final row, with its
    "close" equal to the live price at fetch time. Treating that partial candle
    as a settled close lets intra-hour noise trigger trades the completed bar
    would not have produced (Sep 3 2026: Aug-31 12:05 UTC entry fired on the
    forming 12:00 bar, which closed ~$575 lower an hour later). Signals must be
    computed on closed bars only — same semantics as the backtest/harness.

    Bars are indexed by period START; a bar is closed when start + interval
    <= now. We keep rows whose start is no later than (now - interval).
    """
    freq_min = _INTERVAL_MINUTES.get(interval)
    if freq_min is None:
        return df
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=freq_min)
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    closed = df.loc[idx <= cutoff]
    # Clock-skew / data-lag guard: never gut the frame. If the drop removed
    # nothing or would leave too few bars, keep the original data.
    if len(closed) == len(df) or len(closed) < 20:
        return df
    return closed


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
    exit_lookback_bars: int = 0,
    entry_time: str = "",
) -> dict | None:
    """Compute the strategy on the latest data and return a trade signal.

    Entry acts on the LAST CLOSED BAR only — honest live semantics (no
    peeking at the forming bar, same as the backtest).

    Exit checks the last `exit_lookback_bars` closed bars (default 0 = only
    the latest bar). A transient exit condition that fired mid-window but has
    since relaxed should still close the position — the old behavior only
    looked at the final bar and could hold through an exit that already
    happened (the Aug-10 case: spread > 0.005 for 4 hours, never exited).

    Returns None if data can't be fetched or the strategy has no signals.
    """
    df = fetch_ohlcv(ticker, interval=interval, period=period, allow_synthetic=False)
    if df is None or df.empty or len(df) < 20:
        print(f"  [strategy] Not enough data for {ticker} ({0 if df is None else len(df)} bars)")
        return None

    # yfinance returns the still-forming bar as the last row; its "close" is
    # the live price. Drop it so signals are computed on completed candles
    # only (the behavior this function's docstring already promises).
    df = _drop_forming_bar(df, interval)

    result = compute_indicators(df, dsl)

    entry_cols = [c for c in result.columns if c.startswith("signal_")]
    if not entry_cols:
        print("  [strategy] Strategy defines no signals")
        return None

    # Latest closed bar
    last = result.iloc[-1]
    price = float(last["close"])
    ts = datetime.now(timezone.utc).isoformat()

    # Exit takes precedence over entry (safety first).
    # If we know when the position was entered, check whether the exit
    # condition has fired on ANY bar since entry — a transient exit that
    # already happened must close the position (the Aug-10 case: spread >
    # 0.005 for 4 hours, never exited because only the latest bar was
    # checked). Fall back to a fixed lookback window when entry_time is
    # unknown.
    action = "hold"
    confidence = 0.5
    has_exit_col = "signal_exit" in result.columns
    has_entry_col = "signal_entry" in result.columns
    if has_exit_col:
        if entry_time:
            # Only bars at/after the entry may close the position.
            #
            # Bug found 2026-09-19 on the first live 3of4 entry: daily indexes are
            # tz-NAIVE while entry_time is tz-AWARE, so `index >= t0` raised
            # TypeError and the bare `except` below silently fell back to a fixed
            # 6-bar lookback — which reaches back BEFORE the entry. The position
            # opened at 81,479.99 and was closed 27 seconds later at 81,473.08 by
            # an exit signal from a bar that closed six days earlier. Normalise
            # both sides to UTC-aware, and never exit on pre-entry bars.
            try:
                idx = pd.DatetimeIndex(result.index)
                idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
                t0 = pd.Timestamp(entry_time)
                t0 = t0.tz_localize("UTC") if t0.tzinfo is None else t0.tz_convert("UTC")
                since_entry = pd.Series(idx >= t0, index=result.index)
                if bool(since_entry.any()) and bool(
                        result.loc[since_entry, "signal_exit"].fillna(0).astype(bool).any()):
                    action = "sell"
                    confidence = 0.9
            except Exception as e:
                # A malformed entry_time must not resurrect the stale-signal bug,
                # but we still want to honour a freshly-fired exit: check only the
                # latest closed bar rather than the whole lookback window.
                print(f"  [strategy] entry_time unusable ({e}); "
                      f"checking only the latest closed bar for an exit")
                if bool(result["signal_exit"].tail(1).fillna(0).astype(bool).any()):
                    action = "sell"
                    confidence = 0.9
        else:
            window = result["signal_exit"].tail(max(exit_lookback_bars, 1))
            if bool(window.fillna(0).astype(bool).any()):
                action = "sell"
                confidence = 0.9
    if action != "sell" and has_entry_col and bool(last["signal_entry"]):
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
