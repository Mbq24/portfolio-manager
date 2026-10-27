#!/usr/bin/env python3
"""
Multi-instrument gate for the live Daily Trend Expansion 3of4 strategy.

PURPOSE
-------
Decide which instruments are allowed to join the always-on book. A rule taking ~13
trades/year on one instrument is unfalsifiable in reasonable time, so the only lever
that shortens time-to-knowledge is more INDEPENDENT observations. Adding instruments
naively buys less than it looks like, so this gate asks two questions:

  1. Does the rule hold on the new instrument AT ALL — out of sample, net of costs,
     and better than simply holding the instrument?
  2. Are the return streams INDEPENDENT? Correlated instruments multiply TRADES,
     not INFORMATION.

CRITERIA (all must pass)
------------------------
  BE bps/side            > --be-floor        (default 25; realistic retail is 10-25)
  full-window net        > 0
  holdout net            > 0
  holdout trades         >= --min-holdout-trades   (default 10)
  holdout vs buy & hold  must beat holding the asset (see --bh-hurdle)
  tradable               data-only tickers can be analysed but never adopted

WHY THE BUY-AND-HOLD HURDLE EXISTS
----------------------------------
The first version of this gate had four criteria and admitted QQQ while QQQ lost to
simply holding the index (holdout +17.0% vs +38.4%, return/drawdown 0.78 vs 2.02). A
trend rule that underperforms ownership of the same asset is not a source of edge — it
is an expensive way to hold it.

Return alone cannot rank the two, because the rule deliberately trades upside for
drawdown. So the default hurdle is RISK-ADJUSTED: the strategy's holdout
return/max-drawdown must beat the asset's own. Scaling exposure moves both legs
proportionally, which is precisely why the ratio is the comparable quantity and raw
return is not. `--bh-hurdle both` additionally demands raw-return superiority; that is
stricter than a risk-managed rule needs, but it is available.

RATIO STABILITY
---------------
Return/drawdown explodes when the denominator is near zero (a 5% gain with a 0.2%
drawdown scores 25). When either side's drawdown is below --min-dd-for-ratio the ratio
is treated as unreliable and the hurdle falls back to requiring a higher raw return.
Without this guard the hurdle silently rewards instruments that barely traded.

HOLDOUT METHOD
--------------
Indicators are computed ONCE on the full frame; the holdout is a SLICE of the
already-indicated frame. An earlier version re-ran compute_indicators on the holdout
slice, leaving sma_50 NaN for its first 50 bars — those bars could never signal, which
biased the holdout toward whichever trades happened to start later. The account is
assumed flat at the holdout boundary, correct for an anchored split.

CAVEATS THAT MUST TRAVEL WITH ANY NUMBER THIS PRINTS
----------------------------------------------------
  * net% is the SUM of per-trade percentage returns, not an account return. Never read
    +374% as "the account grew 374%".
  * One window per instrument. A high-volatility asset in a boom/bust window flatters
    any long-only trend rule. The holdout and B&H columns exist to catch that.
  * Long-only: a flat result in a downtrend is the rule working, not failing.

Usage
-----
    <forge>/venv/bin/python multi_instrument_gate.py                  # core universe
    <forge>/venv/bin/python multi_instrument_gate.py --universe wide   # 31 instruments
    <forge>/venv/bin/python multi_instrument_gate.py --bh-hurdle none  # pre-hurdle behaviour
"""
import argparse
import json
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

FORGE = Path("/Users/mark/Library/Mobile Documents/com~apple~CloudDocs/VisualStudioProjects/Forge")

HERE = Path(__file__).resolve().parent

# Forge is imported LAZILY (see forge_modules) so that this module's decision logic —
# the gate, the buy-and-hold hurdle, the independence clustering — can be imported and
# unit-tested without Forge's dependency tree present. The heavy half (fetch, compute,
# backtest) only loads when something actually measures an instrument.
_FORGE_CACHE = {}


def forge_modules():
    if not _FORGE_CACHE:
        for p in (str(FORGE), str(FORGE / "tradingview")):
            if p not in sys.path:
                sys.path.insert(0, p)
        from dsl.schema import IndicatorDSL
        from generators.local import compute_indicators
        from generators.backtest import run_backtest
        from data_fetcher import fetch_ohlcv
        _FORGE_CACHE.update(
            IndicatorDSL=IndicatorDSL,
            compute_indicators=compute_indicators,
            run_backtest=run_backtest,
            fetch_ohlcv=fetch_ohlcv,
        )
    return _FORGE_CACHE

# (ticker, asset_class) or (ticker, asset_class, tradable)
# `tradable=False` = data-only reference (futures quotes the broker cannot fill).
CORE = [
    ("BTC-USD", "crypto"),
    ("ETH-USD", "crypto"),
    ("SOL-USD", "crypto"),
    ("GC=F", "metals", False),   # gold futures: same asset as GLD at 0.91, not fillable
    ("GLD", "metals"),
    ("SPY", "us_equity"),
    ("QQQ", "us_equity"),
]

WIDE = CORE + [
    ("LTC-USD", "crypto"),
    ("LINK-USD", "crypto"),
    ("SLV", "metals"),
    ("GDX", "metals"),
    ("DBC", "commodities"),
    ("USO", "commodities"),
    ("CPER", "commodities"),
    ("IWM", "us_equity"),
    ("DIA", "us_equity"),
    ("XLK", "us_sector"),
    ("XLE", "us_sector"),
    ("XLF", "us_sector"),
    ("XLU", "us_sector"),
    ("XLV", "us_sector"),
    ("EFA", "intl_equity"),
    ("EEM", "intl_equity"),
    ("FXI", "intl_equity"),
    ("EWJ", "intl_equity"),
    ("TLT", "rates"),
    ("IEF", "rates"),
    ("LQD", "credit"),
    ("HYG", "credit"),
    ("UUP", "fx"),
    ("FXE", "fx"),
    ("FXY", "fx"),
]

UNIVERSES = {"core": CORE, "wide": WIDE}


# ── data --------------------------------------------------------------------

def normalize_universe(entries):
    out = []
    for e in entries:
        if len(e) == 2:
            out.append((e[0], e[1], True))
        else:
            out.append((e[0], e[1], bool(e[2])))
    return out


def drop_forming(df):
    """Drop the last daily bar if it is today (UTC) — it may be the forming candle."""
    idx = pd.DatetimeIndex(df.index)
    today = pd.Timestamp.now(tz="UTC").normalize()
    if idx.tz is None:
        today = today.tz_localize(None)
    if len(idx) and idx[-1] >= today:
        return df.iloc[:-1]
    return df


def load(ticker, window="5y", cache_dir=None, ttl_hours=20.0):
    """Fetch daily bars with an on-disk cache.

    allow_synthetic=False: a dead ticker must RAISE. A synthetic fallback means
    measuring a fabricated price series, which is worse than measuring nothing.
    """
    cache = Path(cache_dir) if cache_dir is not None else None
    p = (cache / ("%s_%s_1d.pkl" % (ticker.replace("=", "_").replace("/", "_"), window))
         if cache is not None else None)
    if p is not None and p.exists() and (time.time() - p.stat().st_mtime) < ttl_hours * 3600:
        return pd.read_pickle(p)
    df = forge_modules()["fetch_ohlcv"](ticker, interval="1d", period=window,
                                        allow_synthetic=False)
    if df is None or len(df) < 250:
        raise RuntimeError("insufficient bars: %s" % (0 if df is None else len(df)))
    df = drop_forming(df)
    if cache is not None and p is not None:
        cache.mkdir(exist_ok=True)
        df.to_pickle(p)
    return df


def M(res, name, default=None):
    """run_backtest returns a BacktestResult OBJECT, not a dict. Never subscript it."""
    if res is None:
        return default
    if isinstance(res, dict):
        return res.get(name, default)
    return getattr(res, name, default)


# ── analysis ----------------------------------------------------------------

@dataclass
class Result:
    ticker: str
    group: str
    tradable: bool = True
    bars: int = 0
    first: str = ""
    last: str = ""
    trades: int = 0
    win: float = 0.0
    gross: float = 0.0
    net: float = 0.0
    be_bps: Optional[float] = None
    pf: float = 0.0
    avg_trade: float = 0.0
    maxdd: float = 0.0
    bh_full: Optional[float] = None
    h_trades: int = 0
    h_net: float = 0.0
    h_win: float = 0.0
    h_dd: float = 0.0
    h_bh: Optional[float] = None
    h_bh_dd: Optional[float] = None
    trade_rets: list = field(default_factory=list, repr=False)
    series: object = field(default=None, repr=False)


def analyse(dsl, ticker, group, tradable=True, cost_bps=10.0, window="5y",
            holdout_frac=1.0 / 3.0, cache_dir=None, df=None):
    """Measure one instrument against one DSL.

    `df` lets a caller supply an already-fetched frame (e.g. 4h bars from a venue the default
    feeder cannot reach). Additive on purpose: experiments must reuse THIS measurement code
    rather than reimplement it, or two backtest paths will eventually disagree and the
    disagreement becomes the finding instead of the strategy.
    """
    if df is None:
        df = load(ticker, window=window, cache_dir=cache_dir)
    fm = forge_modules()
    ind = fm["compute_indicators"](df.copy(), dsl)
    if "signal_entry" not in ind.columns:
        raise RuntimeError("no signal_entry column produced")

    gross = fm["run_backtest"](ind, cost_bps_per_side=0.0)
    net = fm["run_backtest"](ind, cost_bps_per_side=cost_bps)

    cut = int(len(ind) * (1.0 - holdout_frac))
    h_ind = ind.iloc[cut:]
    h_net = fm["run_backtest"](h_ind, cost_bps_per_side=cost_bps)

    n_trades = M(gross, "total_trades", 0) or 0
    gross_pct = M(gross, "total_return_pct", 0.0) or 0.0
    be_bps = (gross_pct * 50.0 / n_trades) if n_trades else None

    h_close = h_ind["close"]
    h_bh = 100.0 * (h_close.iloc[-1] / h_close.iloc[0] - 1.0) if len(h_close) > 1 else None
    h_bh_dd = None
    if len(h_close) > 1:
        peak = h_close.cummax()
        h_bh_dd = -float(((h_close / peak) - 1.0).min() * 100.0)

    bh_full = (100.0 * (ind["close"].iloc[-1] / ind["close"].iloc[0] - 1.0)
               if len(ind) > 1 else None)

    s = df["close"].pct_change().dropna()
    di = pd.DatetimeIndex(s.index)
    s.index = di.tz_localize(None).normalize() if di.tz is not None else di.normalize()
    s = s[~s.index.duplicated(keep="last")]

    trades = M(net, "trades", []) or []
    rets = [t.get("return_pct") for t in trades if t.get("return_pct") is not None]

    return Result(
        ticker=ticker, group=group, tradable=tradable,
        bars=len(ind),
        first=str(pd.DatetimeIndex(ind.index)[0])[:10],
        last=str(pd.DatetimeIndex(ind.index)[-1])[:10],
        trades=n_trades,
        win=round(M(net, "win_rate", 0.0) or 0.0, 1),
        gross=round(gross_pct, 2),
        net=round(M(net, "total_return_pct", 0.0) or 0.0, 2),
        be_bps=None if be_bps is None else round(be_bps, 1),
        pf=round(M(net, "profit_factor", 0.0) or 0.0, 2),
        avg_trade=round(M(net, "avg_return_pct", 0.0) or 0.0, 3),
        maxdd=round(M(net, "max_drawdown_pct", 0.0) or 0.0, 1),
        bh_full=None if bh_full is None else round(bh_full, 2),
        h_trades=M(h_net, "total_trades", 0) or 0,
        h_net=round(M(h_net, "total_return_pct", 0.0) or 0.0, 2),
        h_win=round(M(h_net, "win_rate", 0.0) or 0.0, 1),
        h_dd=round(float(M(h_net, "max_drawdown_pct", 0.0) or 0.0), 1),
        h_bh=None if h_bh is None else round(h_bh, 2),
        h_bh_dd=None if h_bh_dd is None else round(h_bh_dd, 1),
        trade_rets=rets, series=s,
    )


# ── the gate ---------------------------------------------------------------

def ret_dd(ret, dd):
    """Return per unit of drawdown. None when undefined or the denominator is noise."""
    if ret is None or dd is None or dd == 0:
        return None
    return float(ret) / abs(float(dd))


def beats_buy_and_hold(r, hurdle="risk_adjusted", min_dd_for_ratio=1.0):
    """(ok, reason). Compares the HOLDOUT only — the full window is in-sample."""
    if hurdle == "none":
        return True, "hurdle disabled"

    if r.h_bh is None or r.h_net is None:
        return False, "no holdout buy-and-hold comparison available"

    if hurdle == "both" and r.h_net <= r.h_bh:
        return False, "holdout %.1f%% <= B&H %.1f%%" % (r.h_net, r.h_bh)

    s_ratio = ret_dd(r.h_net, r.h_dd)
    b_ratio = ret_dd(r.h_bh, r.h_bh_dd)

    # A ratio on a near-zero drawdown denominator is noise, not skill. Fall back to
    # requiring the strategy to out-return holding, rather than reward an instrument
    # that barely traded and therefore barely drew down.
    unstable = ((r.h_dd or 0) < min_dd_for_ratio) or ((r.h_bh_dd or 0) < min_dd_for_ratio)
    if unstable or s_ratio is None or b_ratio is None:
        if r.h_net > r.h_bh:
            return True, "ratio unstable (DD < %.1f%%); out-returned B&H %.1f%% vs %.1f%%" % (
                min_dd_for_ratio, r.h_net, r.h_bh)
        return False, "ratio unstable and holdout %.1f%% <= B&H %.1f%%" % (r.h_net, r.h_bh)

    if s_ratio > b_ratio:
        return True, "ret/DD %.2f > B&H %.2f" % (s_ratio, b_ratio)
    return False, "ret/DD %.2f <= B&H %.2f (holdout %.1f%% vs B&H %.1f%%)" % (
        s_ratio, b_ratio, r.h_net, r.h_bh)


def evaluate_gate(r, be_floor=25.0, min_holdout_trades=10, hurdle="risk_adjusted",
                  min_dd_for_ratio=1.0):
    """(passed, reasons, bh_ok, bh_reason)."""
    reasons = []
    if not r.tradable:
        reasons.append("not tradable (data-only reference)")
    if r.trades == 0:
        reasons.append("no trades at all")
    if r.be_bps is None or r.be_bps < be_floor:
        reasons.append("BE %s bps < %.0f floor" % (r.be_bps, be_floor))
    if r.net <= 0:
        reasons.append("full net %.2f%% <= 0" % r.net)
    if r.h_net <= 0:
        reasons.append("holdout net %.2f%% <= 0" % r.h_net)
    if (r.h_trades or 0) < min_holdout_trades:
        reasons.append("only %s holdout trades" % r.h_trades)

    bh_ok, bh_reason = beats_buy_and_hold(r, hurdle, min_dd_for_ratio)
    if not bh_ok:
        reasons.append("vs B&H: " + bh_reason)

    return (not reasons), reasons, bh_ok, bh_reason


# ── independence -----------------------------------------------------------

def correlation_families(corr, threshold=0.75):
    """Greedy connected components on |rho| >= threshold.

    Same component = the same observation. NOTE the threshold has an artifact: a pair
    landing exactly on 0.75 splits into two "independent" families, which manufactures
    independence right at the boundary. Eyeball the matrix before trusting the count.
    """
    keys = list(corr.columns)
    parent = {k: k for k in keys}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            v = corr.loc[a, b]
            if pd.notna(v) and abs(v) >= threshold:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra

    groups = {}
    for k in keys:
        groups.setdefault(find(k), []).append(k)
    return {k: tuple(sorted(v)) for k, v in ((k, groups[find(k)]) for k in keys)}


def near_threshold_singletons(corr, families, threshold=0.75, margin=0.05):
    """Singleton families whose closest neighbour is within `margin` of the threshold.

    A hard cut-off manufactures independence right at the boundary: an instrument whose
    correlation rounds to exactly the threshold escapes into its own family, and the
    family count then overstates the independent evidence. Flagged, not merged —
    merging two families is a judgement call that belongs to a human.
    """
    keys = list(corr.columns)
    out = []
    for fam in families:
        if len(fam) != 1:
            continue
        t = fam[0]
        pairs = [(k, corr.loc[t, k]) for k in keys
                 if k != t and pd.notna(corr.loc[t, k])]
        if not pairs:
            continue
        partner, rho = max(pairs, key=lambda p: abs(p[1]))
        if abs(rho) >= threshold - margin:
            out.append({"instrument": t, "closest": partner,
                        "correlation": round(float(rho), 3)})
    return out


def pooled_stats(trade_rets):
    """Pooled expectancy over a set of per-trade percentage returns."""
    a = np.asarray(trade_rets, dtype=float)
    if a.size == 0:
        return {}
    wins, losses = a[a > 0], a[a <= 0]
    pf = (wins.sum() / abs(losses.sum())) if losses.sum() != 0 else None
    t_stat = None
    if a.size > 2 and a.std(ddof=1) > 0:
        t_stat = float(a.mean() / (a.std(ddof=1) / np.sqrt(a.size)))
    return {
        "trades": int(a.size),
        "win_rate": round(100.0 * wins.size / a.size, 1),
        "avg_trade_pct": round(float(a.mean()), 3),
        "median_trade_pct": round(float(np.median(a)), 3),
        "avg_win_pct": round(float(wins.mean()), 3) if wins.size else None,
        "avg_loss_pct": round(float(losses.mean()), 3) if losses.size else None,
        "profit_factor": None if pf is None else round(float(pf), 2),
        "t_stat": None if t_stat is None else round(t_stat, 2),
    }


# ── reporting --------------------------------------------------------------

def fmt(v, width=9, prec=2):
    if v is None:
        return "%*s" % (width, "-")
    if isinstance(v, float):
        return "%*.{}f".format(prec) % (width, v)
    return "%*s" % (width, v)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Gate instruments for the live trend book.")
    ap.add_argument("--universe", choices=sorted(UNIVERSES), default="core")
    ap.add_argument("--strategy", default=str(FORGE / "examples" / "daily-trend-expansion-3of4.yaml"))
    ap.add_argument("--window", default="5y")
    ap.add_argument("--cost-bps", type=float, default=10.0)
    ap.add_argument("--be-floor", type=float, default=25.0)
    ap.add_argument("--min-holdout-trades", type=int, default=10)
    ap.add_argument("--holdout-frac", type=float, default=1.0 / 3.0)
    ap.add_argument("--bh-hurdle", choices=["risk_adjusted", "both", "none"],
                    default="risk_adjusted")
    ap.add_argument("--min-dd-for-ratio", type=float, default=1.0)
    ap.add_argument("--corr-threshold", type=float, default=0.75)
    ap.add_argument("--cache-dir", default=str(HERE / ".data_cache"))
    ap.add_argument("--json", default=str(HERE / "multi_instrument_gate.json"))
    ap.add_argument("--quiet", action="store_true", help="only the verdict + JSON")
    a = ap.parse_args(argv)

    dsl = forge_modules()["IndicatorDSL"].from_yaml_file(a.strategy)
    universe = normalize_universe(UNIVERSES[a.universe])

    print("strategy : %s" % dsl.name)
    print("universe : %s (%d instruments)" % (a.universe, len(universe)))
    print("interval : 1d   window: %s   cost: %.0f bps/side   holdout: last %.0f%%"
          % (a.window, a.cost_bps, a.holdout_frac * 100))
    print("hurdle   : vs buy & hold, %s" % a.bh_hurdle)
    print("=" * 108)

    rows, failures = [], []
    for ticker, group, tradable in universe:
        t0 = time.time()
        try:
            r = analyse(dsl, ticker, group, tradable=tradable, cost_bps=a.cost_bps,
                        window=a.window, holdout_frac=a.holdout_frac,
                        cache_dir=a.cache_dir)
            rows.append(r)
            if not a.quiet:
                print("  %-9s %-12s trades=%3d net=%8.2f%% BE=%7s hold=%3d/%8.2f%%  (%.1fs)"
                      % (r.ticker, r.group, r.trades, r.net, r.be_bps,
                         r.h_trades, r.h_net, time.time() - t0))
        except Exception as e:
            failures.append({"ticker": ticker, "error": "%s: %s" % (type(e).__name__, str(e)[:120])})
            print("  %-9s %-12s FAILED  %s" % (ticker, group, str(e)[:70]))

    if not rows:
        print("\nno instruments evaluated — nothing to decide")
        return 1

    if not a.quiet:
        print("\n" + "=" * 108)
        print("MEASUREMENTS   net%% is the SUM of per-trade returns, NOT an account return")
        print("=" * 108)
        print("%-9s %-12s %6s %5s %6s %9s %9s %8s %6s %8s %8s"
              % ("ticker", "class", "bars", "trd", "win%", "gross%", "net%", "BEbps",
                 "pf", "maxDD%", "bh_full%"))
        print("-" * 108)
        for r in sorted(rows, key=lambda x: -(x.be_bps if x.be_bps is not None else -1e9)):
            print("%-9s %-12s %6d %5d %6.1f %9.2f %9.2f %8s %6.2f %8.1f %8s"
                  % (r.ticker, r.group, r.bars, r.trades, r.win, r.gross, r.net,
                     r.be_bps, r.pf, r.maxdd, r.bh_full))

        print("\n" + "=" * 108)
        print("HOLDOUT vs BUY & HOLD   the column that turns returns into a decision")
        print("=" * 108)
        print("%-9s %9s %8s %8s %9s %9s %9s  %s"
              % ("ticker", "net%", "maxDD%", "ret/DD", "B&H%", "B&H DD%", "B&H r/DD", "verdict"))
        print("-" * 108)
        for r in rows:
            s_r, b_r = ret_dd(r.h_net, r.h_dd), ret_dd(r.h_bh, r.h_bh_dd)
            ok, why = beats_buy_and_hold(r, a.bh_hurdle, a.min_dd_for_ratio)
            print("%-9s %9.1f %8.1f %8s %9s %9s %9s  %s"
                  % (r.ticker, r.h_net, r.h_dd, fmt(s_r, 8), fmt(r.h_bh, 9),
                     fmt(r.h_bh_dd, 9), fmt(b_r, 9), "pass" if ok else "FAIL - " + why))

    # ── gate ────────────────────────────────────────────────────────────
    print("\n" + "=" * 108)
    print("GATE   BE > %.0f bps/side | full net > 0 | holdout net > 0 | holdout trades >= %d | beats B&H"
          % (a.be_floor, a.min_holdout_trades))
    print("=" * 108)
    passed, failed = [], []
    for r in rows:
        ok, reasons, _, _ = evaluate_gate(r, a.be_floor, a.min_holdout_trades,
                                          a.bh_hurdle, a.min_dd_for_ratio)
        if ok:
            passed.append(r.ticker)
            print("  PASS  %-9s BE %5.0f bps | net %7.1f%% | holdout %6.1f%% on %d trades"
                  % (r.ticker, r.be_bps, r.net, r.h_net, r.h_trades))
        else:
            failed.append({"ticker": r.ticker, "reasons": reasons})
            print("  FAIL  %-9s %s" % (r.ticker, "; ".join(reasons)))

    pass_rows = [r for r in rows if r.ticker in passed]

    # ── independence ────────────────────────────────────────────────────
    series = {r.ticker: r.series for r in rows if r.series is not None}
    corr = pd.DataFrame(series).dropna(how="all").corr()
    keys = list(corr.columns)
    labels = correlation_families(corr, a.corr_threshold)
    uniq = sorted({tuple(v) for v in labels.values()}, key=lambda t: (-len(t), t))

    # A hard threshold manufactures independence right at the boundary: an instrument
    # whose correlation rounds to exactly the threshold escapes into its own family and
    # the family count then overstates how much independent evidence exists.
    near_threshold = near_threshold_singletons(corr, uniq, a.corr_threshold)

    if not a.quiet:
        print("\n" + "=" * 108)
        print("CORRELATION OF DAILY RETURNS   correlated instruments multiply TRADES, not INFORMATION")
        print("=" * 108)
        print("           " + "".join("%8s" % k[:7] for k in keys))
        for k in keys:
            print("  %-9s" % k + "".join("%8.2f" % corr.loc[k, c] for c in keys))
        off = corr.values[np.triu_indices(len(keys), 1)] if len(keys) > 1 else np.array([])
        if off.size:
            print("\n  mean pairwise correlation: %.2f" % float(np.nanmean(off)))

    print("\n  observation families at |rho| >= %.2f  (%d from %d instruments):"
          % (a.corr_threshold, len(uniq), len(keys)))
    for fam in uniq:
        print("    %s" % (", ".join(fam) if len(fam) > 1 else fam[0]))

    if near_threshold:
        print("\n  !! NEAR-THRESHOLD SINGLETONS — these are probably NOT independent:")
        for w in near_threshold:
            print("     %-9s rho=%.3f with %-9s (threshold %.2f) — treat as the same observation"
                  % (w["instrument"], w["correlation"], w["closest"], a.corr_threshold))
        print("     the family count above therefore OVERSTATES the independent evidence")

    # ── pooled expectancy: naive vs independent ─────────────────────────
    naive = pooled_stats([x for r in pass_rows for x in r.trade_rets])

    reps, rep_rets = [], []
    by_t = {r.ticker: r for r in pass_rows}
    for fam in uniq:
        members = [t for t in fam if t in by_t]
        if not members:
            continue
        rep = max(members, key=lambda t: by_t[t].trades)
        reps.append({"family": list(fam), "representative": rep,
                     "redundant": [t for t in members if t != rep],
                     "trades": by_t[rep].trades})
        rep_rets.extend(by_t[rep].trade_rets)

    indep = pooled_stats(rep_rets)
    raw_trades = sum(r.trades for r in pass_rows)
    incumbent = max((r.trades for r in pass_rows), default=0)
    multiplier = (len(rep_rets) / (incumbent * 1.0)) if incumbent else None

    print("\n" + "=" * 108)
    print("POOLED EXPECTANCY")
    print("=" * 108)
    if naive:
        print("  ALL PASSING INSTRUMENTS (%d) — t is INFLATED, correlated trades treated as independent:"
              % len(pass_rows))
        for k, v in naive.items():
            print("    %-22s %s" % (k, v))
    if indep:
        print("\n  ONE REPRESENTATIVE PER FAMILY (%d) — the honest significance test:" % len(reps))
        for rep in reps:
            print("    %-9s represents %-28s redundant: %s"
                  % (rep["representative"], ", ".join(rep["family"]),
                     ", ".join(rep["redundant"]) or "-"))
        print()
        for k, v in indep.items():
            print("    %-22s %s" % (k, v))
        ts = indep.get("t_stat")
        if ts is not None:
            print("\n  t = %.2f on %d genuinely independent trades  =>  %s"
                  % (ts, indep["trades"],
                     "edge distinguishable from zero" if abs(ts) >= 2
                     else "NOT distinguishable from zero — insufficient evidence"))

    # ── verdict ─────────────────────────────────────────────────────────
    print("\n" + "=" * 108)
    print("ADOPTED: %s" % (", ".join(passed) if passed else "none"))
    if indep:
        print("  raw trades across passers: %d (%.1f/yr)" % (raw_trades, raw_trades / 5.0))
        if multiplier is not None:
            print("  cluster-adjusted honest multiplier vs one incumbent: %.1fx "
                  "(raw count implies %.1fx)"
                  % (multiplier, raw_trades / (incumbent * 1.0) if incumbent else 0))
        print("  quote the ADJUSTED number when describing how much has been learned")
    print("=" * 108)

    out = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "strategy": dsl.name, "universe": a.universe, "window": a.window,
        "cost_bps_per_side": a.cost_bps, "bh_hurdle": a.bh_hurdle,
        "criteria": {"be_floor": a.be_floor, "min_holdout_trades": a.min_holdout_trades,
                     "holdout_frac": a.holdout_frac, "corr_threshold": a.corr_threshold,
                     "min_dd_for_ratio": a.min_dd_for_ratio},
        "rows": [{k: v for k, v in asdict(r).items() if k not in ("trade_rets", "series")}
                 for r in rows],
        "passed": passed, "failed": failed, "fetch_failures": failures,
        "pooled_all_passing": naive,
        "pooled_independent": indep,
        "representatives": reps,
        "families": [list(f) for f in uniq],
        "near_threshold_singletons": near_threshold,
        "correlation": {x: {y: (None if pd.isna(corr.loc[x, y]) else round(float(corr.loc[x, y]), 3))
                            for y in keys} for x in keys},
    }
    Path(a.json).write_text(json.dumps(out, indent=2))
    print("\nwrote %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
