#!/usr/bin/env bash
# signal_hook.sh — Called by cron. Runs one portfolio manager cycle.
# WHAT runs is defined in config.json (strategy, ticker, interval, mode).
# Edit config.json to change strategy / instrument / timeframe.
set -euo pipefail

ROOT="/Users/mark/DBot/portfolio-manager"
PYTHON="$ROOT/.venv/bin/python"

cd "$ROOT"
echo "=== FORGE SIGNAL HOOK ==="
echo "Triggered: $(date)"
echo ""

$PYTHON run.py 2>&1

echo ""
echo "=== HOOK COMPLETE ==="
