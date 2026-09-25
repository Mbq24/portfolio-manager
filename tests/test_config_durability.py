"""Config durability: the runtime config lives on the volume, never in the image.

A Railway deploy replaces the image filesystem. Until now `config.json` was committed to
git and COPY'd into the image, so every deploy silently reverted WHAT the loop ran. On
2026-09-25 that flipped a live book from daily-trend-expansion-3of4 (1d) to
vol-expansion-momentum (1h) and the first cycle of the new container closed an open BTC
position with a strategy that was never meant to be running.

The invariant these tests pin: a deploy may replace the code, but it must never be able
to change what the book is trading.
"""
import json

import pytest

from portfolio import config as C


@pytest.fixture
def tmp_paths(tmp_path, monkeypatch):
    """Redirect both config locations into tmp so tests never touch the real volume."""
    runtime = tmp_path / "data" / "config.json"
    default = tmp_path / "config.default.json"
    monkeypatch.setattr(C, "CONFIG_FILE", runtime)
    monkeypatch.setattr(C, "DEFAULT_FILE", default)
    return runtime, default


def _live_three_instrument():
    return {"mode": "paper", "max_total_exposure_pct": 45.0, "max_class_exposure_pct": 30.0,
            "instruments": [
                {"strategy": "daily-trend-expansion-3of4.yaml", "ticker": "BTC-USD",
                 "asset": "BTCUSD", "interval": "1d", "period": "1y", "risk_pct": 15.0,
                 "asset_class": "crypto", "enabled": True},
                {"strategy": "daily-trend-expansion-3of4.yaml", "ticker": "ETH-USD",
                 "asset": "ETHUSD", "interval": "1d", "period": "1y", "risk_pct": 15.0,
                 "asset_class": "crypto", "enabled": True},
                {"strategy": "daily-trend-expansion-3of4.yaml", "ticker": "GLD",
                 "asset": "GLD", "interval": "1d", "period": "1y", "risk_pct": 15.0,
                 "asset_class": "metals", "enabled": True}]}


# --------------------------------------------------------------- where it lives

def test_runtime_config_lives_under_data_the_mounted_volume():
    """Not the repo root: the repo root is the image, and the image is replaceable."""
    assert C.CONFIG_FILE.parent == C.DATA_DIR, (
        "runtime config must be inside the data/ dir (the Railway volume); got %s"
        % C.CONFIG_FILE)
    assert C.DATA_DIR.name == "data"
    assert C.CONFIG_FILE.name == "config.json"


# ------------------------------------------------------------------- seeding

def test_first_boot_seeds_the_volume_from_the_shipped_default(tmp_paths):
    runtime, default = tmp_paths
    default.write_text(json.dumps({
        "strategy": "daily-trend-expansion-3of4.yaml", "ticker": "GLD", "asset": "GLD",
        "interval": "1d", "period": "1y", "risk_pct": 5.0}))

    cfg = C.load()

    assert runtime.exists(), "a fresh volume should be seeded so the loop has a config"
    assert cfg["instruments"][0]["ticker"] == "GLD"
    assert cfg["instruments"][0]["strategy"] == "daily-trend-expansion-3of4.yaml"


def test_existing_volume_config_is_never_overwritten_by_the_default(tmp_paths):
    """THE regression. The deploy (via the shipped default) must lose to the volume."""
    runtime, default = tmp_paths
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text(json.dumps(_live_three_instrument()))
    default.write_text(json.dumps({
        "strategy": "vol-expansion-momentum.yaml", "ticker": "BTC-USD", "asset": "BTCUSD",
        "interval": "1h", "period": "7d", "risk_pct": 3.0}))

    cfg = C.load()

    tickers = [i["ticker"] for i in cfg["instruments"]]
    assert tickers == ["BTC-USD", "ETH-USD", "GLD"], (
        "the shipped default must NOT replace a live multi-instrument book; got %s" % tickers)
    for i in cfg["instruments"]:
        assert i["strategy"] == "daily-trend-expansion-3of4.yaml"
        assert i["interval"] == "1d"


def test_load_with_no_default_falls_back_to_builtin_defaults(tmp_paths):
    cfg = C.load()
    assert len(cfg["instruments"]) == 1


# --------------------------------------------------------------------- saving

def test_save_writes_to_the_volume_and_round_trips(tmp_paths):
    runtime, _ = tmp_paths
    C.save(_live_three_instrument())

    assert runtime.exists(), "save() must write to the volume, not the image"
    back = C.load()
    assert [i["ticker"] for i in back["instruments"]] == ["BTC-USD", "ETH-USD", "GLD"]
    assert back["instruments"][2]["risk_pct"] == 15.0


def test_transient_hold_flag_is_never_persisted(tmp_paths):
    """A hold is a per-cycle control signal. Persisting it would hold forever."""
    runtime, _ = tmp_paths
    cfg = C.normalize(_live_three_instrument())
    cfg[C.HOLD_KEY] = C.HOLD_FORGE_EMPTY

    C.save(cfg)

    on_disk = json.loads(runtime.read_text())
    assert C.HOLD_KEY not in on_disk, (
        "a transient hold flag leaked into the persisted config: %s" % on_disk.get(C.HOLD_KEY))
