"""Journal attribution: per-instrument rows, one equity snapshot per cycle."""
import csv

import portfolio.journal as journal
from portfolio.state import PortfolioState


def redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(journal, "DATA_DIR", tmp_path)
    monkeypatch.setattr(journal, "TRADES_FILE", tmp_path / "trades.csv")
    monkeypatch.setattr(journal, "EQUITY_FILE", tmp_path / "equity_curve.csv")


def test_each_instrument_keeps_its_own_attribution(monkeypatch, tmp_path):
    redirect(monkeypatch, tmp_path)
    pf = PortfolioState()
    pf.enter_position("BTCUSD", "long", 0.01, 80_000.0,
                      meta={"strategy": "daily-3of4", "ticker": "BTC-USD",
                            "interval": "1d", "source": "forge:daily-3of4"})
    pf.enter_position("GLD", "long", 4.0, 400.0,
                      meta={"strategy": "daily-3of4", "ticker": "GLD",
                            "interval": "1d", "source": "forge:daily-3of4"})

    journal.append(pf, {"mode": "paper", "ticker": "BTC-USD"}, "daily-3of4", "forge:daily-3of4")

    rows = list(csv.DictReader(open(tmp_path / "trades.csv", newline="")))
    assert len(rows) == 2
    by_asset = {r["asset"]: r for r in rows}
    assert by_asset["BTCUSD"]["ticker"] == "BTC-USD"
    assert by_asset["GLD"]["ticker"] == "GLD"


def test_one_equity_row_per_cycle_not_per_instrument(monkeypatch, tmp_path):
    redirect(monkeypatch, tmp_path)
    pf = PortfolioState()
    pf.enter_position("BTCUSD", "long", 0.01, 80_000.0, meta={"ticker": "BTC-USD"})
    pf.enter_position("GLD", "long", 4.0, 400.0, meta={"ticker": "GLD"})

    journal.append(pf, {"mode": "paper"}, "s", "forge:s")

    equity_rows = list(csv.DictReader(open(tmp_path / "equity_curve.csv", newline="")))
    assert len(equity_rows) == 1


def test_reappend_does_not_duplicate_trades(monkeypatch, tmp_path):
    redirect(monkeypatch, tmp_path)
    pf = PortfolioState()
    pf.enter_position("BTCUSD", "long", 0.01, 80_000.0, meta={"ticker": "BTC-USD"})

    journal.append(pf, {"mode": "paper"}, "s", "forge:s")
    journal.append(pf, {"mode": "paper"}, "s", "forge:s")

    rows = list(csv.DictReader(open(tmp_path / "trades.csv", newline="")))
    assert len(rows) == 1, "dedup by (time,type,asset) must survive a re-run"
