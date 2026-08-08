#!/usr/bin/env bash
# vt_signal_hook.sh — Called by cron after the VT pipeline completes.
# Triggers a portfolio manager cycle with the latest signal.
#
# Source priority:
#   1. Forge strategy (--strategy) — the lab-validated execution path
#   2. VT pipeline (no --strategy) — legacy path, kept for compatibility
set -euo pipefail

ROOT="/Users/mark/DBot/portfolio-manager"
PYTHON="$ROOT/.venv/bin/python"

cd "$ROOT"
echo "=== FORGE SIGNAL HOOK ==="
echo "Triggered: $(date)"
echo ""

# Run one portfolio cycle with the Forge Volatility Squeeze strategy on GLD
# (the tradable asset — signals are computed on the SAME instrument we trade)
$PYTHON run.py --paper --strategy volatility-squeeze.yaml --ticker GLD --interval 1h --period 7d 2>&1

echo ""
echo "=== HOOK COMPLETE ==="
