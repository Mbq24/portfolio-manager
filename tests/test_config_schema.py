"""Config schema: an `instruments` list, migrated from the legacy flat keys.

The legacy path must keep working untouched — it is the rollback plan.
"""
from portfolio.config import normalize


def test_legacy_flat_config_becomes_one_instrument():
    cfg = {"strategy": "a.yaml", "ticker": "BTC-USD", "interval": "1d",
           "period": "1y", "asset": "BTCUSD", "risk_pct": 15.0}
    out = normalize(cfg)
    assert len(out["instruments"]) == 1
    i = out["instruments"][0]
    assert i["ticker"] == "BTC-USD"
    assert i["asset"] == "BTCUSD"
    assert i["strategy"] == "a.yaml"
    assert i["enabled"] is True
    assert i["risk_pct"] == 15.0


def test_explicit_instruments_list_is_preserved_in_order():
    cfg = {"instruments": [
        {"strategy": "a.yaml", "ticker": "BTC-USD", "asset": "BTCUSD",
         "interval": "1d", "period": "1y", "risk_pct": 15.0, "asset_class": "crypto"},
        {"strategy": "a.yaml", "ticker": "GLD", "asset": "GLD",
         "interval": "1d", "period": "1y", "risk_pct": 10.0, "asset_class": "metals"},
    ]}
    out = normalize(cfg)
    assert [i["ticker"] for i in out["instruments"]] == ["BTC-USD", "GLD"]
    assert out["instruments"][1]["asset_class"] == "metals"


def test_instrument_inherits_shared_defaults():
    """An instrument may omit interval/period/strategy and inherit the top-level value."""
    cfg = {"strategy": "shared.yaml", "interval": "1d", "period": "1y", "risk_pct": 15.0,
           "instruments": [{"ticker": "BTC-USD", "asset": "BTCUSD", "asset_class": "crypto"}]}
    i = normalize(cfg)["instruments"][0]
    assert i["strategy"] == "shared.yaml"
    assert i["interval"] == "1d"
    assert i["period"] == "1y"
    assert i["risk_pct"] == 15.0


def test_caps_default_and_are_not_overwritten():
    out = normalize({"ticker": "GLD", "asset": "GLD"})
    assert out["max_total_exposure_pct"] == 45.0
    assert out["max_class_exposure_pct"] == 30.0

    kept = normalize({"ticker": "GLD", "asset": "GLD", "max_total_exposure_pct": 20.0})
    assert kept["max_total_exposure_pct"] == 20.0


def test_single_instrument_is_the_legacy_rollback_shape():
    """Dropping the instruments key must reproduce today's exact behaviour."""
    legacy = {"strategy": "daily-trend-expansion-3of4.yaml", "ticker": "BTC-USD",
              "asset": "BTCUSD", "interval": "1d", "period": "1y", "risk_pct": 15.0}
    out = normalize(legacy)
    i = out["instruments"][0]
    assert (i["strategy"], i["ticker"], i["asset"], i["interval"], i["period"],
            i["risk_pct"]) == ("daily-trend-expansion-3of4.yaml", "BTC-USD", "BTCUSD",
                               "1d", "1y", 15.0)
