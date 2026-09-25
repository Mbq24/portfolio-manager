"""
portfolio/config.py — Runtime configuration for the portfolio manager.

This is the single source of truth for WHAT is being executed: which Forge
strategy, on which instrument(s), at which timeframe, in which mode (paper vs
live).

WHERE the config lives matters as much as what it says. The runtime config is stored on
the mounted volume (data/config.json), NOT in the image. Railway replaces the image
filesystem on every deploy, so a config baked into the image is silently reverted by a
deploy. On 2026-09-25 exactly that happened: the committed image config was
vol-expansion-momentum (1h) while the book was meant to be running
daily-trend-expansion-3of4 (1d), and the new container's first cycle closed an open BTC
position with a strategy that was never supposed to be running.

    data/config.json        runtime config — on the volume, survives redeploys
    config.default.json     shipped seed, used ONLY when the volume has no config yet

Overrides: PM_CONFIG_FILE, PM_DATA_DIR, PM_CONFIG_DEFAULT_FILE.

Multi-instrument: the loop runs every enabled entry in `instruments`. A config
with no `instruments` key is still valid — `normalize()` derives a one-element
list from the legacy flat keys, which makes "delete the instruments key" a
complete rollback to single-instrument behaviour.

    python run.py            # uses the runtime config
    python run.py --paper    # same config, force internal paper executor
"""

import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# The volume. On Railway this is mounted at /app/data.
DATA_DIR = Path(os.environ.get("PM_DATA_DIR") or (REPO_ROOT / "data"))

# Runtime config — deliberately NOT the repo root (see module docstring).
CONFIG_FILE = Path(os.environ.get("PM_CONFIG_FILE") or (DATA_DIR / "config.json"))

# Seed shipped in the image; consumed only on the first boot of an empty volume.
DEFAULT_FILE = Path(os.environ.get("PM_CONFIG_DEFAULT_FILE")
                    or (REPO_ROOT / "config.default.json"))

# Transient, per-cycle control keys. NEVER persisted — a hold that reached disk would
# hold every subsequent cycle and silently stop the loop.
HOLD_KEY = "hold_cycle"
HOLD_FORGE_EMPTY = "forge-desired-config-empty"
HOLD_FORGE_UNREACHABLE = "forge-unreachable"
TRANSIENT_KEYS = (HOLD_KEY,)

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
    # Keys beginning with "_" are comments/metadata for humans (e.g. the seed file's
    # "_comment"). They must never reach the effective config, the API response, or the
    # persisted file.
    cfg = {k: v for k, v in cfg.items()
           if not (isinstance(k, str) and k.startswith("_"))}
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


def _seed_if_missing() -> None:
    """Populate the volume from the shipped default on first boot, and only then.

    An existing runtime config is authoritative and is never overwritten. That is the
    point of this function existing: a deploy replaces the code, it must not be able to
    change what the book is trading.
    """
    if CONFIG_FILE.exists():
        return
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if DEFAULT_FILE.exists():
            CONFIG_FILE.write_text(DEFAULT_FILE.read_text())
            print(f"  [config] seeded {CONFIG_FILE} from {DEFAULT_FILE.name}")
        else:
            CONFIG_FILE.write_text(json.dumps(DEFAULTS, indent=2))
            print(f"  [config] seeded {CONFIG_FILE} from built-in DEFAULTS")
    except OSError as e:
        print(f"  [config] could not seed {CONFIG_FILE}: {e}")


def load() -> dict:
    """Load the runtime config merged over defaults.

    Seeds the volume from the shipped default if the volume has no config yet.
    Missing keys fall back to defaults; an unparseable config falls back to defaults
    and says so rather than dying.
    """
    _seed_if_missing()
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text()))
        except (json.JSONDecodeError, IOError) as e:
            print(f"  [config] Failed to parse {CONFIG_FILE}: {e} — using defaults")
    return normalize(cfg)


def save(cfg: dict):
    """Persist config (merged over defaults) to the runtime volume.

    Transient per-cycle flags are stripped so a hold can never be written down.
    """
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in cfg.items() if k not in TRANSIENT_KEYS})
    normalized = normalize(merged)
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(normalized, indent=2))
    except OSError as e:
        print(f"  [config] Failed to save {CONFIG_FILE}: {e}")
        return
    print(f"  [config] Saved → {CONFIG_FILE}")


def describe(cfg: dict) -> str:
    """Human-readable summary of what's configured to run."""
    lines = [f"  mode     : {cfg.get('mode', 'paper')}",
             f"  caps     : total {cfg.get('max_total_exposure_pct')}% / "
             f"class {cfg.get('max_class_exposure_pct')}%",
             f"  forge    : {'push to ' + cfg.get('forge_url', '?') if cfg.get('forge_push') else 'disabled'}",
             f"  config   : {CONFIG_FILE}",
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
    pulls them here, persists them to the runtime config (so the volume
    always reflects what will run), and returns the merged config.

    NO AUTHORITY, NO ACTION. If Forge returns no desired config, or cannot be reached,
    the returned config is flagged with `hold_cycle` and the caller must take no entries
    and no exits this cycle.

    That is deliberately NOT "fall back to the last-known-good local config". A wiped or
    unreachable control room is indistinguishable from a stale local file, and a stale
    local file already cost a real position once (2026-09-25). For a strategy that trades
    a handful of times a year, holding a cycle is free.

    Returns the merged config, possibly flagged with `hold_cycle`.
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
    except Exception as e:
        held = normalize(cfg)
        held[HOLD_KEY] = HOLD_FORGE_UNREACHABLE
        print(f"  [config] Forge unreachable ({e}) — HOLDING this cycle: no entries, "
              f"no exits (no authority on what should be running)")
        return held

    remote = data.get("config") or {}
    if not remote:
        held = normalize(cfg)
        held[HOLD_KEY] = HOLD_FORGE_EMPTY
        print("  [config] Forge has no desired config — HOLDING this cycle: no entries, "
              "no exits (a deploy can wipe it; a stale local file must not trade)")
        return held

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


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--show":
        print(describe(load()))
    elif len(sys.argv) > 1 and sys.argv[1] == "--reset":
        save(dict(DEFAULTS))
        print("  [config] Reset to defaults")
    else:
        print("Usage: python -m portfolio.config --show | --reset")
