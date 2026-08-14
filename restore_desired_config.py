#!/usr/bin/env python3
"""Restore intended desired config on Forge (vol-expansion-momentum)."""
import json
import urllib.request

BASE = "https://forge-production-0c60.up.railway.app"
DESIRED = {
    "strategy": "vol-expansion-momentum.yaml",
    "ticker": "BTC-USD",
    "interval": "1h",
    "period": "7d",
    "asset": "BTCUSD",
    "mode": "paper",
    "risk_pct": 3.0,
}

req = urllib.request.Request(
    BASE + "/api/portfolio/config",
    data=json.dumps(DESIRED).encode(),
    headers={"Content-Type": "application/json"},
    method="PUT",
)
with urllib.request.urlopen(req, timeout=20) as resp:
    print("status:", resp.status)
    print("body:", resp.read().decode())
