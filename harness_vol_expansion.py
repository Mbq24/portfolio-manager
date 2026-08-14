#!/usr/bin/env python3
"""Re-validate vol-expansion-momentum on BTC-USD 1h across windows.

Runs the comparison harness (buy&hold + random-entry baselines, z-score)
on the CURRENT committed strategy YAML. Uses Forge's engine + fetcher.
"""
import json
import sys
from pathlib import Path

FORGE = Path("/Users/mark/Library/Mobile Documents/com~apple~CloudDocs/VisualStudioProjects/Forge")
sys.path.insert(0, str(FORGE))

from dsl.schema import IndicatorDSL
from generators.harness import run_comparison

dsl = IndicatorDSL.from_yaml_file(str(FORGE / "examples" / "vol-expansion-momentum.yaml"))
entry_cond = dsl.signals.get("entry").condition if dsl.signals.get("entry") else "?"
print(f"Strategy: {dsl.name} | entry: {entry_cond}")

for period in ("7d", "1mo", "3mo"):
    print(f"\n{'=' * 60}\nBTC-USD 1h {period}\n{'=' * 60}")
    try:
        result = run_comparison([dsl], ["BTC-USD"], "1h", period, random_iters=60)
        row = result["rows"][0]
        for k in ("ticker", "interval", "period", "regime", "bars",
                  "total_trades", "win_rate", "total_return_pct",
                  "bh_return_pct", "random_mean", "random_std",
                  "z_score", "verdict", "error"):
            if k in row:
                print(f"  {k:18s}: {row[k]}")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"  ERROR: {e}")
