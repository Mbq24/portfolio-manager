#!/usr/bin/env python3
"""Inspect Forge server portfolio state vs local."""
import json
import urllib.request

BASE = "https://forge-production-0c60.up.railway.app"

def get(path):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return json.loads(r.read().decode())

state = get("/api/portfolio/state").get("state", {})
print("state keys:", list(state.keys()))
print("updated_at:", state.get("updated_at"))
print("config:", state.get("config"))
print("strategy_name:", state.get("strategy_name"))
pf = state.get("portfolio") or {}
print("portfolio equity:", pf.get("equity"), "cash:", pf.get("cash"))

cfg = get("/api/portfolio/config")
print("desired config:", cfg)
