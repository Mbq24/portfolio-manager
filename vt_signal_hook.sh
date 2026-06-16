#!/usr/bin/env bash
# vt_signal_hook.sh — Called by VT cron after pipeline completes.
# Triggers a portfolio manager cycle with the latest VT signal.
set -euo pipefail

ROOT="/Users/mark/DBot/portfolio-manager"
PYTHON="/Users/mark/ONTOLOGY/venv311/bin/python3.11"

cd "$ROOT"
echo "=== VT SIGNAL HOOK ==="
echo "Triggered: $(date)"
echo ""

# Run one portfolio cycle
$PYTHON run.py 2>&1

echo ""
echo "=== HOOK COMPLETE ==="
