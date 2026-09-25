# Multi-Instrument Expansion — Implementation Plan

**Goal:** let the portfolio-manager run the same Forge strategy on more than one instrument, so the
live record accumulates observations fast enough to confirm or refute the edge in months instead of years.

**Architecture:** `PortfolioState` already supports N positions (`positions` is a list; `position_value`
sums it; `exit_position(asset)` is keyed by asset; `mark_prices` iterates). The single-asset assumption
lives entirely in `run.py`'s cycle and in the config schema. So this is a **loop + schema + contract**
change, not a state-model rewrite. Capital allocation is the one genuinely new piece of logic, enforced
at entry via exposure caps.

**Tech Stack:** Python 3.11 (pm), Flask (portfolio_server.py), Forge DSL/harness (bundled at build time),
Railway for deployment.

---

## 0. Why this is worth doing — the gate result (2026-09-25)

`python multi_instrument_gate.py` — strategy `daily-trend-expansion-3of4`, interval 1d, 5y window,
net of 10 bps/side. Returns are the SUM of per-trade returns, not compounded.

| ticker | trades | win% | gross% | net% | BE bps | holdout trades | holdout net% | holdout B&H% |
|---|---|---|---|---|---|---|---|---|
| BTC-USD | 65 | 36.9 | 109.00 | 95.77 | 83.8 | 19 | +33.32 | −19.61 |
| ETH-USD | 67 | 26.9 | 67.53 | 54.03 | 50.4 | 24 | +58.32 | −19.16 |
| SOL-USD | 60 | 41.7 | 366.34 | 353.64 | 305.3 | 23 | +37.55 | −53.86 |
| GC=F | 51 | 41.2 | 58.51 | 48.21 | 57.4 | 17 | +16.57 | +55.47 |
| GLD | 50 | 42.0 | 65.43 | 55.32 | 65.4 | 15 | +25.43 | +54.82 |
| SPY | 58 | 39.7 | 24.04 | 12.44 | **20.7** | 22 | +3.52 | +30.51 |
| QQQ | 61 | 32.8 | 30.42 | 18.17 | **24.9** | 19 | +16.63 | +45.35 |

Correlation of daily returns:

```
          BTC   ETH   SOL   GC=F   GLD   SPY   QQQ
BTC      1.00  0.84  0.73   0.11  0.14  0.42  0.42
ETH      0.84  1.00  0.74   0.10  0.13  0.45  0.45
SOL      0.73  0.74  1.00   0.07  0.10  0.39  0.39
GC=F     0.11  0.10  0.07   1.00  0.91  0.10  0.10
GLD      0.14  0.13  0.10   0.91  1.00  0.15  0.15
SPY      0.42  0.45  0.39   0.10  0.15  1.00  0.95
QQQ      0.42  0.45  0.39   0.10  0.15  0.95  1.00
```

**Decisions from the data:**

- **Adopt BTC-USD (incumbent), ETH-USD, GLD.**
- **Reject SPY and QQQ** — break-even 20.7 / 24.9 bps is under any realistic retail cost, and the
  holdout shows the rule badly trailing buy-and-hold (+3.5% vs +30.5%; +16.6% vs +45.4%). An index
  trend-follower that loses to holding the index is not worth live capital.
- **Defer SOL-USD.** Its numbers look spectacular (+353.6% net, BE 305 bps) but (a) it is 0.73–0.74
  correlated with BTC/ETH, so it mostly duplicates observations already being made, and (b) its 5y
  window is one extraordinary boom/bust, which flatters a high-vol asset under a summed-return metric.
  Revisit only if BTC/ETH/GLD fail to produce enough signal.
- **GLD not GC=F** — identical asset (0.91 correlation) but GLD is an ETF tradable on Alpaca; GC=F is
  a futures ticker usable for data only.

**Honest caveat that must travel with this change:** the strategy has *no demonstrated edge on gold* —
its holdout underperformed holding gold (+25.4% vs +54.8%). GLD is being added as an **independent
observation of the same rule**, not as a proven profit centre, and specifically because it is the only
candidate near-zero correlated with the crypto book (0.10–0.14). Its value is information, and it should
be reported separately rather than pooled into one "edge" number.

**Time-to-knowledge:** BTC alone ≈ 13 entries/yr. BTC+ETH+GLD ≈ 36 trades/yr — but because BTC/ETH are
0.84 correlated, the effective independent gain is closer to 1.8–2×, not 2.8×. Say "roughly 13 → 36
trades a year, of which maybe two-thirds are independent."

---

## 1. Current context / assumptions

- `run.py` is a single-instrument cycle: one `ASSET`, one `cfg["strategy"]`, one `compute_signal`, one
  decision block, one `meta` stamp, one `push_to_forge` config.
- `config.json` is flat: `strategy, ticker, interval, period, asset, risk_pct, exit_lookback_bars, mode,
  forge_push, forge_url`.
- Forge is the control room: `GET /api/portfolio/config` returns `{"config": {...}}` (singular) and
  `sync_from_forge()` merges it over the local config, persisting to `config.json`.
- `alpaca_buy()` sizes as `cash * risk_pct / 100`. **With multiple positions this is a latent bug** —
  instrument #2 would be sized off cash already depleted by instrument #1. Sizing must move to equity.
- Exposure is currently 15% per trade (`risk_pct: 15.0`).
- The live loop is the deployed Railway service `portfolio-manager`. Deploys reset the desired Forge
  config, and a fresh Forge container boots with a non-null default strategy — the deploy order matters.

## 2. Proposed approach

1. **Schema:** add an `instruments` list; derive it from the legacy flat keys when absent. The legacy
   path must keep working untouched — that is the rollback plan.
2. **Allocation:** size each instrument off *equity*, and enforce two hard caps at entry — a total
   exposure cap and a per-group cap, with crypto as one group precisely because of the 0.84 correlation.
3. **Loop:** iterate enabled instruments inside one cycle, sharing one `PortfolioState`. Per-instrument
   signal, decision, and journal attribution.
4. **Contract:** Forge accepts and returns the full config. Until the Forge UI has a list editor, its
   single-strategy PUT **refuses to save when more than one instrument is enabled**, so it cannot
   silently collapse the book back to one instrument.
5. **Monitoring:** digest and probe iterate instruments and report per-instrument condition state.

## 3. Files likely to change

- `portfolio/config.py` — schema, migration, `describe()`, `sync_from_forge()`
- `run.py` — the cycle loop, allocation, per-instrument meta
- `portfolio/executor.py` — sizing helper takes an explicit budget instead of `cash`
- `portfolio/journal.py` — per-instrument attribution
- `portfolio/state.py` — add `mark_prices` coverage for N tickers (likely no change; verify)
- `multi_instrument_gate.py` — already added (the gate)
- Forge: `tradingview/app.py` (`/api/portfolio/config` GET/PUT), `web/src/pages/Portfolio.tsx`
- `~/.hermes/scripts/forge_daily_digest.py`, `forge_strategy_probe.py` — per-instrument reporting

## 4. Step-by-step tasks

### Task 1 — Config schema with backward-compatible migration

**Objective:** `load()` returns an `instruments` list, synthesised from legacy keys when absent.

**Files:** Modify `portfolio/config.py`.

**Step 1 — write the failing test** (`tests/test_config_schema.py`):

```python
from portfolio.config import normalize

def test_legacy_flat_config_becomes_one_instrument():
    cfg = {"strategy": "a.yaml", "ticker": "BTC-USD", "interval": "1d",
           "period": "1y", "asset": "BTCUSD", "risk_pct": 15.0}
    out = normalize(cfg)
    assert len(out["instruments"]) == 1
    i = out["instruments"][0]
    assert i["ticker"] == "BTC-USD" and i["asset"] == "BTCUSD"
    assert i["enabled"] is True and i["risk_pct"] == 15.0

def test_explicit_instruments_list_is_preserved():
    cfg = {"instruments": [
        {"strategy": "a.yaml", "ticker": "BTC-USD", "interval": "1d",
         "period": "1y", "asset": "BTCUSD", "risk_pct": 15.0},
        {"strategy": "a.yaml", "ticker": "GLD", "interval": "1d",
         "period": "1y", "asset": "GLD", "risk_pct": 10.0},
    ]}
    out = normalize(cfg)
    assert [i["ticker"] for i in out["instruments"]] == ["BTC-USD", "GLD"]
```

**Step 2 — run it, expect failure:** `pytest tests/test_config_schema.py -v` → `ImportError: cannot import name 'normalize'`

**Step 3 — implement** in `portfolio/config.py`:

```python
INSTRUMENT_KEYS = ("strategy", "ticker", "interval", "period", "asset", "risk_pct")

def normalize(cfg: dict) -> dict:
    """Guarantee an `instruments` list. Legacy flat keys become a single instrument."""
    cfg = dict(cfg)
    insts = cfg.get("instruments")
    if insts:
        cfg["instruments"] = [
            {**i, "enabled": i.get("enabled", True),
             "risk_pct": float(i.get("risk_pct", cfg.get("risk_pct", 1.0)))}
            for i in insts
        ]
    else:
        cfg["instruments"] = [{
            "strategy": cfg.get("strategy", ""),
            "ticker": cfg.get("ticker", ""),
            "interval": cfg.get("interval", "1h"),
            "period": cfg.get("period", "7d"),
            "asset": cfg.get("asset", ""),
            "risk_pct": float(cfg.get("risk_pct", 1.0)),
            "enabled": True,
        }]
    cfg.setdefault("max_total_exposure_pct", 45.0)
    cfg.setdefault("max_group_exposure_pct", 30.0)
    cfg.setdefault("groups", {"crypto": ["BTC-USD", "ETH-USD", "SOL-USD"]})
    return cfg
```

Call `normalize()` at the end of `load()` and inside `sync_from_forge()` before `save()`.

**Step 4 — run, expect pass.**

**Step 5 — commit:** `git add portfolio/config.py tests/test_config_schema.py && git commit -m "feat(config): instruments list with legacy migration"`

### Task 2 — Per-instrument budget instead of `cash`

**Objective:** sizing takes an explicit dollar budget so instrument N is not sized off cash depleted by N−1.

**Files:** Modify `portfolio/executor.py`, `run.py` (`alpaca_buy`).

**Step 1 — failing test** (`tests/test_sizing.py`):

```python
from portfolio.executor import size_for_budget

def test_size_uses_budget_not_cash():
    # $10,000 equity, 10% budget -> $1,000 position at $100 -> 10 units
    assert size_for_budget(100.0, 1000.0) == 10.0

def test_budget_is_capped_by_available_cash():
    # budget $1,000 but only $400 cash -> size on $400
    assert size_for_budget(100.0, 1000.0, available_cash=400.0) == 4.0
```

**Step 2 — expect failure.**

**Step 3 — implement:**

```python
def size_for_budget(price: float, budget: float, available_cash: float | None = None) -> float:
    """Position size for a dollar budget. Cash is a hard ceiling, not the basis."""
    if price <= 0:
        raise ValueError("price must be positive")
    notional = max(0.0, budget)
    if available_cash is not None:
        notional = min(notional, max(0.0, available_cash))
    return round(notional / price, 8)
```

**Step 4 — pass.** **Step 5 — commit.**

### Task 3 — Exposure caps

**Objective:** refuse an entry that would breach the total or group exposure cap.

**Files:** Create `portfolio/allocation.py`; test `tests/test_allocation.py`.

**Test:**

```python
from portfolio.allocation import can_enter

BOOK = {"equity": 10_000.0, "positions": {"BTC-USD": 1_500.0}, "groups": {"crypto": ["BTC-USD", "ETH-USD"]}}

def test_blocks_when_over_total_cap():
    ok, why = can_enter(BOOK, "GLD", 4_000.0, max_total_pct=45.0, max_group_pct=30.0)
    assert not ok and "total" in why.lower()

def test_blocks_when_group_cap_breached():
    b = {"equity": 10_000.0, "positions": {"BTC-USD": 2_900.0}, "groups": {"crypto": ["BTC-USD", "ETH-USD"]}}
    ok, why = can_enter(b, "ETH-USD", 500.0, max_total_pct=45.0, max_group_pct=30.0)
    assert not ok and "group" in why.lower()

def test_allows_under_caps():
    ok, _ = can_enter(BOOK, "GLD", 2_000.0, max_total_pct=45.0, max_group_pct=30.0)
    assert ok
```

**Implement** `can_enter(book, ticker, notional, max_total_pct, max_group_pct) -> (bool, str)` summing
existing position value + notional against both caps. Group lookup: the group whose member list contains
the ticker, else no group cap.

**Commit** after pass.

### Task 4 — The multi-instrument cycle in `run.py`

**Objective:** one cycle evaluates every enabled instrument.

**Files:** Modify `run.py:161-322`.

Replace the single-signal block with a loop over `cfg["instruments"]` where `enabled`:

```python
for inst in [i for i in cfg["instruments"] if i.get("enabled")]:
    inst_cfg = {**cfg, **inst}
    asset, ticker = inst_cfg["asset"], inst_cfg["ticker"]
    in_position = any(p.asset == asset for p in pf.positions)
    entry_time = next((p.entry_time for p in pf.positions if p.asset == asset), "") if in_position else ""
    strategy = load_strategy(inst_cfg["strategy"])
    signal = compute_signal(strategy, ticker, inst_cfg["interval"], inst_cfg["period"], asset,
                            exit_lookback_bars=int(cfg.get("exit_lookback_bars", 6)) if in_position else 0,
                            entry_time=entry_time)
    meta = {"strategy": strategy.name, "ticker": ticker, "interval": inst_cfg["interval"],
            "source": f"forge:{strategy.name}"}
    ... existing decide/execute logic, but:
      - budget = pf.equity * inst_cfg["risk_pct"] / 100.0
      - gate buys through can_enter(...)
      - never let one instrument's signal touch another instrument's position
```

**Critical rule:** the exit path must resolve its position by `asset`, never by index or by "the first
position". Document it inline. This is the multi-asset form of the pre-entry-exit bug class.

**Two-sided test** (`tests/test_multi_instrument_cycle.py`) — the most important test in this plan:

```python
def test_exit_for_one_asset_does_not_close_another():
    # BTC long open, ETH long open; BTC exit signal fires
    # assert: BTC position closed, ETH position STILL OPEN

def test_entry_for_one_asset_does_not_block_another():
    # BTC already long; ETH buy signal fires
    # assert: ETH enters (the `already in position` guard is per-asset)
```

### Task 5 — Journal + state attribution per instrument

**Objective:** every entry/exit row carries its own instrument's strategy/ticker, and equity records
once per cycle rather than once per instrument.

**Files:** Modify `portfolio/journal.py`, verify `portfolio/state.py`.

**Test:** two instruments in one cycle produce two entries with distinct `ticker` in the journal, and
exactly one equity-curve point for the cycle.

### Task 6 — Forge contract: accept a list, and refuse to clobber it

**Objective:** Forge stores and returns the full config; the single-strategy UI cannot silently reduce
the book to one instrument.

**Files:** Modify Forge `tradingview/app.py` (`/api/portfolio/config`), then
`web/src/pages/Portfolio.tsx`.

**Step 1 — backend:** `PUT` accepts `instruments`; `GET` returns it. Add a guard:

```python
if len([i for i in cfg.get("instruments", []) if i.get("enabled")]) > 1 \
        and not data.get("instruments"):
    return jsonify({"error": "This config has multiple instruments; use the API to edit it. "
                             "The single-strategy editor would drop the others."}), 409
```

**Step 2 — verify with the Flask test client** (run from `tradingview/`, not the repo root):

```bash
cd "<forge>/tradingview" && ../venv/bin/python -c "
import app as f; c = f.app.test_client()
print(c.get('/api/portfolio/config').get_json())"
```

**Step 3 — UI (phase 2):** list editor. Until then the 409 guard is the protection.

### Task 7 — Monitoring iterates instruments

**Objective:** the daily digest and probe report per-instrument condition state.

**Files:** Modify `~/.hermes/scripts/forge_daily_digest.py`, `~/.hermes/scripts/forge_strategy_probe.py`.

`forge_strategy_probe.py` currently computes one strategy's conditions; make it loop the enabled
instruments and emit one JSON object per instrument. The digest's `>> CHANGE` detection must key on
(ticker, condition) rather than a single strategy's condition vector, or three instruments will fight
over one state slot and produce false change alerts.

**Test:** run the digest twice back-to-back with no data change → no `>> CHANGE` lines (two-sided: faked
per-instrument change → flagged).

## 5. Validation before anything touches the live loop

```bash
cd /Users/mark/DBot/portfolio-manager
.venv/bin/python -m pytest tests/ -v          # all green
python run.py --config                         # shows the normalised instrument list
python run.py --paper --summary                # state intact, no trades
python multi_instrument_gate.py                # gate unchanged
```

Then a **dry cycle against the live endpoints without trading**: point `forge_url` at production, run
`run.py` locally with `mode=paper` and all instruments enabled, and confirm it reads signals for three
instruments, opens nothing it shouldn't, and one `push_to_forge` carries all three.

## 6. Deploy (order matters — this is a known trap)

```bash
./build_bundle.sh                      # wipes vendor/ first — do not skip
railway up --no-gitignore
# WAIT for the new container to actually serve; a fresh container boots with its own default
curl -s <pm>/health                    # must show a bumped cycle_count
# ONLY NOW set the desired config:
curl -X PUT <forge>/api/portfolio/config -d '{"mode":"paper","instruments":[...]}'
curl -X POST <pm>/api/portfolio/cycle  # trigger
# verify TWO cycles — anything that happens "on the next pass" is invisible to one check
curl -s <pm>/api/portfolio/state | ... # all three instruments present, correct strategy_name per ticker
```

Rollback: restore the flat single-instrument `config.json` (no `instruments` key) — `normalize()`
regenerates a one-element list, and the code path is identical to today's.

## 7. Risks, tradeoffs, open questions

- **Capital concentration.** Three instruments at 15% each is 45% gross exposure, and BTC/ETH at 0.84
  correlation means the crypto sleeve behaves like one ~30% bet. The group cap is the mitigation; do not
  raise `max_group_exposure_pct` without revisiting this.
- **Deeper drawdowns are already the accepted tradeoff** of the 3-of-4 variant (5y maxDD 45.4% BTC vs
  33.7% for 4/4). More instruments does not reduce that; it diversifies across assets, not within one.
- **Gold has no demonstrated edge** (holdout +25.4% vs +54.8% buy-and-hold). Report it separately;
  do not pool it into a single edge statistic.
- **Sizing change from cash → equity is a behavioural change** to the live loop even at one instrument.
  It is *correct* (cash-based sizing is a latent multi-position bug) but it must be called out, and the
  first post-deploy cycle should be checked against the old sizing by hand.
- **Summed, not compounded, returns.** The gate's `net%` column sums per-trade percentage returns. Do
  not read +353.6% as an account return.
- **Metric honesty.** 36 trades/yr still means ~2 years to a meaningful live sample. This change
  shortens time-to-knowledge; it does not produce a verdict.
- **Open question:** should `exit_lookback_bars` stay 6 for daily bars now that three instruments poll
  the same cycle? Unchanged for now; revisit if a missed exit appears.
- **Open question:** does the Forge UI need the list editor before this is usable by Mark, or is
  config-via-API acceptable for phase 1? Assumption: API is fine for phase 1.
