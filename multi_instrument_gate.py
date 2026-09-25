#!/usr/bin/env python3
"""
Multi-instrument gate for the live Daily Trend Expansion 3of4 strategy.

WHY: the strategy takes ~13 trades/year on ONE instrument, so the live record needs years to
confirm or refute the edge. Only more independent observations shorten that. But two things must
be checked before adding instruments to a live loop:

  1. Does the rule hold on the new instruments AT ALL (out-of-sample, net of costs)?
     An in-sample win is not evidence. Adopt only what clears break-even AND survives a holdout.
  2. Are the return streams independent? BTC/ETH are highly correlated, so adding a correlated
     instrument multiplies the TRADE COUNT without multiplying the INFORMATION. The honest
     metric is independent observations, not raw trade count.

Gate (all must pass for an instrument to be adopted):
  - break-even bps/side comfortably above realistic retail cost (~10-25 bps/side)
  - positive net return on BOTH the full window and the anchored holdout
  - enough trades to be meaningful (>~10 in the holdout)

Run with Forge's venv:  <forge>/venv/bin/python multi_instrument_gate.py
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

FORGE = Path("/Users/mark/Library/Mobile Documents/com~apple~CloudDocs/VisualStudioProjects/Forge")
sys.path.insert(0, str(FORGE))
sys.path.insert(0, str(FORGE / "tradingview"))

from dsl.schema import IndicatorDSL            # noqa: E402
from generators.local import compute_indicators  # noqa: E402
from generators.backtest import run_backtest     # noqa: E402
from data_fetcher import fetch_ohlcv             # noqa: E402

COST_BPS = 10.0          # per side, applied to entry and exit
WINDOW = "5y"
HOLDOUT_FRAC = 1.0 / 3.0  # last third = holdout (rule-based, so "train" is only a time split)

CANDIDATES = [
    ("BTC-USD", "crypto  (incumbent)"),
    ("ETH-USD", "crypto  (already gated in the 3of4 study)"),
    ("SOL-USD", "crypto  (higher vol -> more entries)"),
    ("GC=F",    "gold futures  (non-correlated diversifier)"),
    ("GLD",     "gold ETF"),
    ("SPY",     "US large-cap equity index"),
    ("QQQ",     "US tech equity index"),
]

STRATEGY = FORGE / "examples" / "daily-trend-expansion-3of4.yaml"


def load(ticker, start=None, end=None, period=WINDOW):
    df = fetch_ohlcv(ticker, interval="1d", period=period, start=start, end=end,
                     allow_synthetic=False)
    if df is None or len(df) < 120:
        raise RuntimeError("insufficient bars: %s" % (0 if df is None else len(df)))
    return df


def drop_forming(df):
    """Last daily bar may be the in-progress candle. Drop it if it is today (UTC)."""
    idx = pd.DatetimeIndex(df.index)
    today = pd.Timestamp.now(tz="UTC").normalize()
    if idx.tz is None:
        today = today.tz_localize(None)
    if idx[-1] >= today:
        return df.iloc[:-1]
    return df


def evaluate(dsl, df):
    """Return (gross_metrics, net_metrics) for one dataframe."""
    d = drop_forming(df.copy())
    d = compute_indicators(d, dsl)
    if "signal_entry" not in d.columns:
        raise RuntimeError("no signal_entry column produced")
    gross = run_backtest(d, cost_bps_per_side=0.0)
    net = run_backtest(d, cost_bps_per_side=COST_BPS)
    return gross, net


def M(res, name, default=None):
    """run_backtest returns a BacktestResult object, not a dict."""
    if res is None:
        return default
    if isinstance(res, dict):
        return res.get(name, default)
    return getattr(res, name, default)


def main():
    dsl = IndicatorDSL.from_yaml_file(str(STRATEGY))
    print("strategy: %s   |  interval 1d  |  costs %.0f bps/side" % (dsl.name, COST_BPS))
    print("=" * 96)

    rows = []
    rets = {}
    for ticker, note in CANDIDATES:
        try:
            df = load(ticker)
        except Exception as e:
            print("%-10s  FETCH FAILED: %s" % (ticker, str(e)[:70]))
            continue

        try:
            g, n = evaluate(dsl, df)
        except Exception as e:
            print("%-10s  EVAL FAILED: %s" % (ticker, str(e)[:70]))
            continue

        # anchored holdout: last third of the window
        cut = int(len(df) * (1.0 - HOLDOUT_FRAC))
        df_h = df.iloc[cut:]
        try:
            gh, nh = evaluate(dsl, df_h)
        except Exception as e:
            gh = nh = None

        # break-even bps/side from gross return and trade count
        n_trades = M(g, "total_trades", 0) or 0
        gross_pct = M(g, "total_return_pct", 0.0) or 0.0
        be_bps = (gross_pct * 50.0 / n_trades) if n_trades else None

        # daily returns for the correlation matrix
        s = df["close"].pct_change().dropna()
        di = pd.DatetimeIndex(s.index)
        s.index = di.tz_localize(None).normalize() if di.tz is not None else di.normalize()
        s = s[~s.index.duplicated(keep="last")]
        rets[ticker] = s

        bh = 100.0 * (df_h["close"].iloc[-1] / df_h["close"].iloc[0] - 1.0) if len(df_h) > 1 else None
        h_trades = M(nh, "total_trades", None)
        h_net = M(nh, "total_return_pct", None)

        rows.append({
            "ticker": ticker, "note": note, "bars": len(df),
            "range": "%s -> %s" % (str(pd.DatetimeIndex(df.index)[0])[:10],
                                   str(pd.DatetimeIndex(df.index)[-1])[:10]),
            "trades": n_trades, "win": round(M(g, "win_rate", 0.0) or 0.0, 1),
            "gross": round(gross_pct, 2),
            "net": round(M(n, "total_return_pct", 0.0) or 0.0, 2),
            "be_bps": None if be_bps is None else round(be_bps, 1),
            "maxdd": round(M(n, "max_drawdown_pct", 0.0) or 0.0, 1),
            "h_trades": h_trades,
            "h_net": None if h_net is None else round(h_net, 2),
            "h_bh": None if bh is None else round(bh, 2),
        })

    print("\n%-9s %6s %7s %7s %8s %8s %8s %9s %9s %8s"
          % ("ticker", "bars", "trades", "win%", "gross%", "net%", "BE(bps)", "hold_trd", "hold_net%", "hold_b&h"))
    print("-" * 96)
    for r in rows:
        print("%-9s %6d %7d %7.1f %8.2f %8.2f %8s %9s %9s %8s"
              % (r["ticker"], r["bars"], r["trades"], r["win"], r["gross"], r["net"],
                 r["be_bps"], r["h_trades"], r["h_net"], r["h_bh"]))

    print("\n=== notes / window ===")
    for r in rows:
        print("  %-9s %-46s %s" % (r["ticker"], r["note"], r["range"]))

    # ---- independence: correlation of daily returns -------------------------
    print("\n=== correlation of daily returns (the independence check) ===")
    print("  Correlated instruments multiply TRADES, not INFORMATION.")
    if len(rets) >= 2:
        corr = pd.DataFrame(rets).dropna(how="all").corr()
        keys = list(corr.columns)
        print("           " + "".join("%9s" % k[:8] for k in keys))
        for k in keys:
            print("  %-9s" % k + "".join("%9.2f" % corr.loc[k, c] for c in keys))

        # average pairwise correlation among the passing set
        arr = corr.values
        off = arr[np.triu_indices(len(keys), 1)]
        print("\n  mean pairwise correlation across candidates: %.2f" % float(np.nanmean(off)))

    # ---- gate verdict ------------------------------------------------------
    print("\n=== GATE ===")
    passed = []
    for r in rows:
        reasons = []
        if r["be_bps"] is None or r["be_bps"] < 25:
            reasons.append("BE %.1f bps <= 25 bps floor" % (r["be_bps"] or 0))
        if r["net"] is None or r["net"] <= 0:
            reasons.append("full-window net %.2f%% <= 0" % (r["net"] or 0))
        if r["h_net"] is None or r["h_net"] <= 0:
            reasons.append("holdout net %.2f%% <= 0" % (r["h_net"] or 0))
        if (r["h_trades"] or 0) < 10:
            reasons.append("only %s holdout trades" % r["h_trades"])
        if reasons:
            print("  FAIL  %-9s  %s" % (r["ticker"], "; ".join(reasons)))
        else:
            passed.append(r["ticker"])
            print("  PASS  %-9s  BE %.0f bps, net %.1f%%, holdout %.1f%% on %d trades"
                  % (r["ticker"], r["be_bps"], r["net"], r["h_net"], r["h_trades"]))

    json.dump({"rows": rows, "passed": passed},
              open("/Users/mark/construction_research/../multi_instrument_gate.json", "w"), indent=2)
    print("\nADOPTED: %s" % (", ".join(passed) if passed else "none"))
    print("wrote /Users/mark/multi_instrument_gate.json")


if __name__ == "__main__":
    main()
