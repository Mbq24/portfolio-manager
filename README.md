# Portfolio Manager

Paper trade manager that turns VT pipeline signals into tracked
portfolio positions, P&L, and equity curves. You own it.

## Quick start

```bash
# Show current portfolio state
python run.py --summary

# Run one full cycle (ingest VT signal → trade → report)
python run.py

# Reset to $10,000 paper capital
python run.py --reset
```

## Data flow

```
VT pipeline (every 6h)
    │
    ▼
results/gold_1h_meanrev_backtest.json
    │
    ▼ (vt_signal_hook.sh)
run.py
    │
    ├── portfolio/state.py      — positions, cash, P&L
    ├── portfolio/signal_ingest.py — read VT output
    ├── portfolio/executor.py   — paper fills, spread, commission
    └── portfolio/reporter.py   — summaries
    │
    ▼
data/state.json  (persisted)
data/latest_report.txt
```

## Cost model

| Item | Value |
|------|-------|
| Spread | 0.5 pips |
| Slippage | 0–2 pips |
| Commission | $3.50/lot |
| Risk per trade | 1% of cash |
