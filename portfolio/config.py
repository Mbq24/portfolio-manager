"""
portfolio/config.py — Runtime configuration for the portfolio manager.

This is the single source of truth for WHAT is being executed: which Forge
strategy, on which instrument(s), at which timeframe, in which mode (paper vs
live). Edit this file to change what runs.

Multi-instrument: the loop runs every enabled entry in `instruments`. A config
with no `instruments` key is still valid — `normalize()` derives a one-element
list from the legacy flat keys, which makes "delete the instruments key" a
complete rollback to single-instrument behaviour.

    python run.py            # uses config.json
    python run.py --paper    # same config, force internal paper executor
"""

import json
from pathlib import Path

CONFIG_FILE = Path(__file__).parent.parent / "config.json"

DEFAULTS = {
    # Which Forge strategy to execute (filename in Forge/examples/ or absolute path)
    "strategy": "volatility-squeeze.yaml",
    # Instrument + timeframe — MUST be the same asset you trade (signal == execution)
    "ticker": "GLD",
    "interval": "1h",
    "period": "7d",
    # Asset symbol traded through the broker (GLD ETF on Alpaca)
    "asset": "GLD",
    # Asset class for the per-class exposure cap (crypto / metals / equity / ...)
    "asset_class": "",
    # Position sizing: % of EQUITY exposed per instrument (notional). 1.0 = 1%.
    # This strategy has NO stop loss (signal-based exits), so this is an
    # exposure cap, not a stop-risk number.
    "risk_pct": 1.0,
    # Exposure caps, enforced at entry across ALL instruments (see portfolio/allocation.py).
    # 45% total is 3x the old single-instrument gross; 30% per class exists because BTC and
    # ETH are 0.84 correlated and would otherwise stack into one oversized crypto bet.
    "max_total_exposure_pct": 45.0,
    "max_class_exposure_pct": 30.0,
    # When a position is open, check the exit signal across this many recent
    # bars so a transient exit isn't missed (yfinance data lags; the signal
    # may have fired mid-window and relaxed by the time we poll).
    "exit_lookback_bars": 6,
    # "paper" uses the internal executor; anything else goes through the broker
    "mode": "paper",
    # Push state snapshot to Forge so the control-room visual stays in sync
    "forge_push": True,
    "forge_url": "https://forge-production-0c60.up.railway.app",
}

# Keys an instrument may inherit from the top level when it omits them.
INSTRUMENT_KEYS = ("strategy", "ticker", "interval", "period", "asset",
                   "risk_pct", "asset_class")


def normalize(cfg: dict) -> dict:
    """Guarantee an `instruments` list, derived from the legacy flat keys if absent.

    Every instrument is filled out completely, so downstream code never has to guess at a
    missing field. Top-level values act as defaults that an individual instrument overrides.
    """
    cfg = dict(cfg)
    shared = {k: cfg[k] for k in INSTRUMENT_KEYS if k in cfg}
    raw = cfg.get("instruments") or []

    instruments = []
    if raw:
        for spec in raw:
            merged = dict(shared)                       # inherit top-level defaults
            merged.update({k: v for k, v in spec.items() if v is not None})
            merged["enabled"] = bool(merged.get("enabled", True))
            merged["risk_pct"] = float(merged.get("risk_pct", 1.0))
            merged["asset_class"] = merged.get("asset_class") or ""
            instruments.append(merged)
    else:
        one = dict(shared)
        one.setdefault("strategy", "")
        one.setdefault("ticker", "")
        one.setdefault("interval", "1h")
        one.setdefault("period", "7d")
        one.setdefault("asset", "")
        one["risk_pct"] = float(one.get("risk_pct", 1.0))
        one["asset_class"] = one.get("asset_class") or ""
        one["enabled"] = True
        instruments.append(one)

    cfg["instruments"] = instruments
    cfg.setdefault("max_total_exposure_pct", 45.0)
    cfg.setdefault("max_class_exposure_pct", 30.0)
    return cfg


def enabled_instruments(cfg: dict) -> list:
    """The instruments this cycle will act on."""
    return [i for i in normalize(cfg)["instruments"] if i.get("enabled")]


def load() -> dict:
    """Load config.json merged over defaults. Missing keys fall back to defaults."""
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text()))
        except (json.JSONDecodeError, IOError) as e:
            print(f"  [config] Failed to parse {CONFIG_FILE}: {e} — using defaults")
    return normalize(cfg)


def save(cfg: dict):
    """Persist config (merged over defaults) to config.json."""
    merged = dict(DEFAULTS)
    merged.update(cfg)
    normalized = normalize(merged)
    CONFIG_FILE.write_text(json.dumps(normalized, indent=2))
    print(f"  [config] Saved → {CONFIG_FILE}")


def describe(cfg: dict) -> str:
    """Human-readable summary of what's configured to run."""
    lines = [f"  mode     : {cfg['mode']}",
             f"  caps     : total {cfg.get('max_total_exposure_pct')}% / "
             f"class {cfg.get('max_class_exposure_pct')}%",
             f"  forge    : {'push to ' + cfg['forge_url'] if cfg['forge_push'] else 'disabled'}",
             f"  instruments ({len(enabled_instruments(cfg))} enabled):"]
    for i in normalize(cfg)["instruments"]:
        flag = "on " if i.get("enabled") else "OFF"
        lines.append(
            f"    [{flag}] {i['ticker']:<9} {i['interval']:<3} {i['period']:<4} "
            f"risk {i['risk_pct']:>5.1f}%  class={i['asset_class'] or '-':<7} {i['strategy']}")
    return "\n".join(lines)


def sync_from_forge(cfg: dict) -> dict:
    """Pull the desired config from Forge and apply it locally.

    Forge is the control room: edits made in the web UI land in
    Forge's /api/portfolio/config, and the next portfolio-manager cycle
    pulls them here, persists them to config.json (so the local file
    always reflects what will run), and returns the merged config.

    Returns the merged config. On any failure, returns the local config
    unchanged (running the last-known-good config is safer than stopping).
    """
    if not cfg.get("forge_push"):
        return normalize(cfg)

    import json as _json
    import urllib.request

    url = cfg["forge_url"].rstrip("/") + "/api/portfolio/config"
    try:
        req = urllib.request.Request(url, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode())
        remote = data.get("config") or {}
        if not remote:
            return normalize(cfg)

        merged = dict(DEFAULTS)
        merged.update(cfg)      # local file wins for non-remote keys
        merged.update(remote)   # remote (Forge) wins for strategy/ticker/instruments/etc.
        # A remote config that carries `instruments` replaces the list wholesale; one that
        # only carries legacy flat keys leaves the local list intact (so a single-strategy
        # UI save cannot silently collapse a multi-instrument book).
        if "instruments" not in remote:
            merged["instruments"] = cfg.get("instruments")
        merged = normalize(merged)
        save(merged)
        names = ", ".join(i["ticker"] for i in enabled_instruments(merged))
        print(f"  [config] Applied remote config from Forge: "
              f"{remote.get('strategy', '(list)')} | {names} ({merged.get('mode', '?')})")
        return merged
    except Exception as e:
        print(f"  [config] Forge config sync failed ({e}) — using local config")
        return normalize(cfg)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--show":
        print(describe(load()))
    elif len(sys.argv) > 1 and sys.argv[1] == "--reset":
        save(dict(DEFAULTS))
        print("  [config] Reset to defaults")
    else:
        print("Usage: python -m portfolio.config --show | --reset")
