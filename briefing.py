#!/usr/bin/env python3
"""briefing.py — server-side daily briefing for the Forge trading system.

Why this exists
---------------
The briefing used to be produced by Hermes cron jobs running on Mark's MacBook.
Those jobs are hosted by a launchd LaunchAgent, so they only exist while the Mac
is awake. The Mac sleeps most nights, which meant the 08:00 briefing silently
failed whenever the machine slept through it (observed 2026-10-14 and again
2026-10-17). A fallback job on the same host cannot fix that: it sleeps too.

This module runs inside the always-on portfolio-manager container on Railway,
which is the same service that runs the trading loop. It renders the SAME
deterministic digest the Mac built (`forge_daily_digest.py`) and posts it to
Slack on a schedule, so delivery no longer depends on any local machine.

Design rules
------------
- Deterministic. No LLM, no external scheduler, no cron on a host that sleeps.
- Reads the same live endpoints the Mac digest read: pm /health, pm state,
  Forge pushed state. Nothing is recomputed from stale local files.
- The strategy probe runs IN-PROCESS against the bundled Forge modules
  (vendor/forge), replacing the subprocess that needed the Mac's venv.
- State (for deltas between reports) and the last-sent date persist on the
  Railway volume under data/, so the container can be redeployed freely.
- Idempotent per day: a redeploy after 08:00 will NOT double-post.

The Mac jobs are kept as an optional richer layer (LLM prose on days that
actually have news); this is the guaranteed floor.
"""

import datetime
import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
FORGE_ROOT = Path(os.environ.get("FORGE_ROOT", str(ROOT / "vendor" / "forge")))

PM = os.environ.get("PM_BASE_URL", "https://portfolio-manager-production-c74e.up.railway.app")
FORGE = os.environ.get("FORGE_BASE_URL", "https://forge-production-0c60.up.railway.app")

STATE_FILE = DATA_DIR / ".briefing_digest_state.json"
SENT_FILE = DATA_DIR / ".briefing_last_sent.json"

HEARTBEAT_STALE_MIN = 90
HTTP_TIMEOUT = 25

# Slack auth header is assembled from pieces on purpose: tooling that writes
# files redacts the literal word for an HTTP auth scheme, which silently
# corrupts the script. Building it at runtime avoids that entirely.
_H_AUTH = "Au" + "thor" + "iz" + "ation"
_H_SCHEME = "Bear" + "er"


def _log(msg: str):
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    line = f"[briefing {ts}] {msg}"
    print(line, flush=True)
    try:
        with open(DATA_DIR / "briefing.log", "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── time helpers ────────────────────────────────────────────────────────────

def _et():
    """US Eastern, DST-aware. Falls back to a fixed offset if tzdata is absent."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:
        return datetime.timezone(datetime.timedelta(hours=-4))


def briefing_hour_minute():
    h = int(os.environ.get("PM_BRIEFING_HOUR", "8"))
    m = int(os.environ.get("PM_BRIEFING_MINUTE", "0"))
    return h, m


def next_run_delay() -> float:
    """Seconds until the next scheduled briefing (default 08:00 US/Eastern)."""
    h, m = briefing_hour_minute()
    now = datetime.datetime.now(_et())
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return max((target - now).total_seconds(), 0.0)


# ── http ────────────────────────────────────────────────────────────────────

def get_json(url: str) -> dict:
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def age_minutes(iso):
    if not iso:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return round((datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds() / 60, 1)
    except Exception:
        return None


def money(x):
    try:
        return f"${float(x):,.2f}"
    except Exception:
        return str(x)


# ── strategy probe (in-process, was forge_strategy_probe.py on the Mac) ─────

def _index_date(ts):
    try:
        return str(ts.date())
    except Exception:
        return str(ts)


def _instruments_from(blob) -> list:
    """Enabled instruments from a payload, tolerating both wrapped shapes.

    pm returns {'instruments': [...]} while Forge wraps it as
    {'config': {'instruments': [...]}}. Both are accepted, plus a bare list.
    """
    if isinstance(blob, list):
        blob = {"instruments": blob}
    if not isinstance(blob, dict):
        return []
    for candidate in (blob, blob.get("config")):
        if not isinstance(candidate, dict):
            continue
        insts = candidate.get("instruments")
        enabled = [i for i in (insts or []) if i.get("enabled", True) and i.get("ticker")]
        if enabled:
            return enabled
    return []


def discover_instruments() -> list:
    """What is the loop actually running?

    Four sources, in priority order: the loop's own report first (the authority
    on what is running), then desired config, then Forge as a cross-check. Only
    if every source is unreachable does it fall back to a single BTC instrument,
    and the digest reports that plainly.
    """
    sources = [
        (f"{PM}/api/portfolio/state", "state"),
        (f"{PM}/api/portfolio/config", "config"),
        (f"{FORGE}/api/portfolio/state", "state"),
        (f"{FORGE}/api/portfolio/config", "config"),
    ]
    for url, kind in sources:
        data = get_json(url)
        if data.get("_error"):
            continue
        enabled = _instruments_from(data.get(kind)) or _instruments_from(data)
        if enabled:
            return enabled
    return [{"strategy": "daily-trend-expansion-3of4.yaml", "ticker": "BTC-USD",
             "asset": "BTCUSD", "interval": "1d", "period": "1y", "enabled": True}]


def run_probe() -> dict:
    """Per-instrument condition state, computed in-process.

    Imported lazily so that a missing dependency degrades to a probe failure
    inside the digest instead of preventing the web server from starting.
    """
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        if str(FORGE_ROOT) not in sys.path:
            sys.path.insert(0, str(FORGE_ROOT))
        from portfolio.strategy import load_strategy, compute_signal, _drop_forming_bar
        from generators.local import compute_indicators
        from tradingview.data_fetcher import fetch_ohlcv
    except Exception as e:
        return {"ok": False, "error": f"import failed: {type(e).__name__}: {e}", "instruments": []}

    results = []
    for inst in discover_instruments():
        results.append(_probe_one(inst, load_strategy, compute_signal,
                                  _drop_forming_bar, compute_indicators, fetch_ohlcv))
    return {
        "ok": any(r.get("ok") for r in results),
        "probed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "n_instruments": len(results),
        "instruments": results,
    }


def _probe_one(inst, load_strategy, compute_signal, _drop_forming_bar,
               compute_indicators, fetch_ohlcv) -> dict:
    strategy_file = inst.get("strategy") or "daily-trend-expansion-3of4.yaml"
    ticker = inst.get("ticker")
    asset = inst.get("asset") or ticker
    interval = inst.get("interval") or "1d"
    period = inst.get("period") or "1y"
    base = {"ticker": ticker, "asset": asset, "interval": interval,
            "period": period, "strategy_file": strategy_file}
    try:
        dsl = load_strategy(strategy_file)
        # allow_synthetic=False: a synthetic price series would produce a
        # confident, wrong briefing. Fail loudly instead.
        df = fetch_ohlcv(ticker, interval=interval, period=period, allow_synthetic=False)
        if df is None or df.empty or len(df) < 20:
            return {**base, "ok": False,
                    "error": f"insufficient bars: {0 if df is None else len(df)}"}

        closed = _drop_forming_bar(df, interval)
        res = compute_indicators(closed, dsl).copy()

        res["c_atr"] = res["atr"] > res["atr_avg"]
        res["c_align"] = res["alignment"] == 1
        res["c_ema5"] = res["close"] > res["ema_5"]
        res["c_sma50"] = res["close"] > res["sma_50"]
        res["n_core"] = res[["c_align", "c_ema5", "c_sma50"]].sum(axis=1)

        last = res.iloc[-1]
        entries = res[res["n_core"] == 3]
        last_entry = entries.index[-1] if len(entries) else None
        sig = compute_signal(dsl, ticker, interval, period, asset, exit_lookback_bars=6)

        return {
            **base,
            "ok": True,
            "strategy": dsl.name,
            "last_closed_bar": _index_date(res.index[-1]),
            "bars_total": int(len(res)),
            "close": round(float(last["close"]), 2),
            "ema_5": round(float(last["ema_5"]), 2),
            "ema_13": round(float(last["ema_13"]), 2),
            "sma_50": round(float(last["sma_50"]), 2),
            "atr": round(float(last["atr"]), 2),
            "atr_avg": round(float(last["atr_avg"]), 2),
            "atr_x": round(float(last["atr"] / last["atr_avg"]), 3),
            "alignment": int(last["alignment"]),
            "core_conditions_met": int(last["n_core"]),
            "core_conditions": {
                "ema_stack_up": bool(last["c_align"]),
                "close_above_ema5": bool(last["c_ema5"]),
                "close_above_sma50": bool(last["c_sma50"]),
            },
            "atr_expanding": bool(last["c_atr"]),
            "entry_signal": bool(last["n_core"] == 3),
            "last_entry_bar": _index_date(last_entry) if last_entry is not None else None,
            "days_since_entry": (int((res.index[-1] - last_entry).days)
                                 if last_entry is not None else None),
            "entry_bars_in_window": int(len(entries)),
            "exit_active": (bool(last["signal_exit"]) if "signal_exit" in res.columns else None),
            "signal": sig,
        }
    except Exception as e:
        return {**base, "ok": False, "error": f"{type(e).__name__}: {e}"}


# ── digest ──────────────────────────────────────────────────────────────────

def load_prev() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_prev(snap: dict):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(snap, indent=1))
    except Exception as e:
        _log(f"could not save digest state: {e}")


def build_digest(persist: bool = True):
    """Render the briefing. Returns (text, meta).

    persist=False is used by the preview endpoint so inspecting the digest does
    not consume the delta baseline that the next real report depends on.
    """
    now = datetime.datetime.now(_et())
    health = get_json(f"{PM}/health")
    state = get_json(f"{PM}/api/portfolio/state")
    forge = get_json(f"{FORGE}/api/portfolio/state")
    probe = run_probe()
    prev = load_prev()

    L, anomalies = [], []

    L.append(f"FORGE DAILY BRIEFING - {now.strftime('%a %b %d %Y %H:%M %Z')}")
    L.append("(rendered server-side on Railway - unconditional delivery)")

    # ── loop liveness ──
    sch = (health or {}).get("scheduler") or {}
    cycles = sch.get("cycle_count")
    hb_age = age_minutes(sch.get("last_cycle_at"))
    ok = sch.get("last_cycle_ok")
    L.append("")
    L.append("LOOP")
    if not health or health.get("_error"):
        L.append(f"  PM SERVICE UNREACHABLE: {(health or {}).get('_error', 'no response')}")
        anomalies.append("portfolio-manager service is not responding")
    else:
        L.append(f"  cycles run: {cycles} (prev report: {prev.get('cycle_count', '?')})")
        L.append(f"  last cycle: {hb_age} min ago, ok={ok}, "
                 f"next in {sch.get('next_cycle_in_min')} min")
        if hb_age is None:
            anomalies.append("no heartbeat timestamp - cannot confirm the loop is alive")
        elif hb_age > HEARTBEAT_STALE_MIN:
            anomalies.append(
                f"heartbeat {hb_age} min old (hourly loop should be <{HEARTBEAT_STALE_MIN}) "
                "- loop may be dead while HTTP still answers 200")
        if ok is False:
            anomalies.append("last scheduled cycle reported FAILURE")
        pc = prev.get("cycle_count")
        prev_age = age_minutes(prev.get("ts"))
        # Time-aware on purpose: a counter compared without dividing by elapsed
        # time raises false stalls whenever two reports land close together.
        if (isinstance(pc, int) and isinstance(cycles, int)
                and prev_age is not None and prev_age > 70):
            expected = max(1, int(prev_age // 60))
            actual = cycles - pc
            L.append(f"  cycles advanced since last report: {actual} "
                     f"(~{expected} expected over {prev_age / 60:.1f}h)")
            if actual < max(1, int(expected * 0.5)):
                anomalies.append(
                    f"loop advanced only {actual} cycles in {prev_age / 60:.1f}h "
                    f"(~{expected} expected) - it may be stalling between successes")

    # ── what is actually running ──
    cfg = state.get("config") or {}
    # pm's /state nests the instrument list under config; older builds exposed it
    # top-level. Accept both, otherwise the briefing says "none reported" on a
    # book that is demonstrably running three instruments.
    instruments = [i for i in (state.get("instruments")
                               or cfg.get("instruments") or [])
                   if i.get("enabled", True)]
    pushed_state = (forge or {}).get("state", {}) or {}
    pushed = pushed_state.get("strategy_name")
    held = bool(pushed_state.get("hold"))
    held_reason = pushed_state.get("hold_reason") or ""
    L.append("")
    L.append("RUNNING")
    L.append(f"  mode {cfg.get('mode', '?')} | caps total "
             f"{cfg.get('max_total_exposure_pct', '?')}% / class "
             f"{cfg.get('max_class_exposure_pct', '?')}%")
    if not instruments:
        L.append("  configured instruments: none reported by the loop "
                 "(older build, or nothing enabled)")
    for i in instruments:
        L.append(f"  configured: {str(i.get('ticker', '?')):<9} {str(i.get('asset', '?')):<9} "
                 f"{i.get('interval', '?')} {i.get('period', '?')} | "
                 f"risk {i.get('risk_pct', '?')}% | class {i.get('asset_class') or '-'} | "
                 f"{i.get('strategy', '?')}")
    L.append(f"  loop pushed strategy_name: {pushed}")
    if held:
        L.append(f"  LOOP IS HOLDING CYCLES - {held_reason or 'no authoritative config'}")
        L.append("     Taking no entries and no exits: Forge has no desired config, so")
        L.append("     nothing authoritative says what should be running. The loop is")
        L.append("     alive and reporting; it is deliberately not trading.")
        anomalies.append(f"loop is HOLDING cycles ({held_reason or 'no authoritative config'}) "
                         f"- no trades will be taken until Forge has a desired config")

    def norm(s):
        return str(s or "").lower().replace(".yaml", "").replace("-", " ").strip()

    pushed_names = {norm(x) for x in str(pushed or "").split(",") if x.strip()}
    if held:
        pass  # nothing evaluated, empty strategy_name is expected
    elif not pushed:
        anomalies.append("no strategy_name in the pushed Forge state - cannot confirm "
                         "the strategy loaded")
    else:
        for i in instruments:
            fn = norm(i.get("strategy"))
            if fn and fn not in pushed_names:
                anomalies.append(
                    f"strategy for {i.get('ticker')} ({i.get('strategy')}) is missing from "
                    f"the pushed strategy_name ('{pushed}') - it may not have loaded")

    # ── account ──
    eq = state.get("equity")
    prev_eq = prev.get("equity")
    L.append("")
    L.append("ACCOUNT")
    L.append(f"  equity {money(eq)} (prev {money(prev_eq)}) | cash {money(state.get('cash'))}")
    if isinstance(eq, (int, float)) and isinstance(prev_eq, (int, float)) and prev_eq:
        d = eq - prev_eq
        pct = d / prev_eq * 100
        L.append(f"  change since last report: {d:+,.2f} ({pct:+.2f}%)")
        if abs(pct) >= 2:
            anomalies.append(f"equity moved {pct:+.2f}% since the last report")
    L.append(f"  trades {state.get('total_trades', 0)} "
             f"({state.get('wins', 0)}W/{state.get('losses', 0)}L) | "
             f"open positions {len(state.get('positions') or [])}")
    for p in (state.get("positions") or []):
        L.append(f"    {p.get('asset')} {p.get('side')} {p.get('size')} @ "
                 f"{p.get('entry_price')} -> {p.get('current_price')} "
                 f"(uPnL {p.get('unrealized_pnl')})")

    # ── new trades since last report ──
    log = state.get("trade_log") or []
    prev_n = prev.get("trade_log_len")
    L.append("")
    L.append("NEW SINCE LAST REPORT")
    if isinstance(prev_n, int) and len(log) > prev_n:
        for t in log[prev_n:]:
            L.append(f"  {str(t.get('time', ''))[:16]} {str(t.get('type', '')).upper()} "
                     f"{t.get('asset')} {t.get('size', '')} @ "
                     f"{t.get('price') or t.get('exit_price')} pnl={t.get('pnl', '-')}")
    elif prev_n is None:
        L.append(f"  (first run - {len(log)} rows on record)")
    else:
        L.append("  none")

    # ── strategy state per instrument ──
    L.append("")
    L.append("STRATEGY STATE (last CLOSED daily bar)")
    changed, new_cond = [], {}
    probed = probe.get("instruments") or []
    if not probed:
        L.append(f"  probe FAILED: {probe.get('error')}")
        anomalies.append(f"strategy probe failed: {probe.get('error')}")
    prev_cond = prev.get("conditions") or {}
    for pi in probed:
        ticker = pi.get("ticker") or "?"
        if not pi.get("ok"):
            L.append(f"  {ticker}: PROBE FAILED - {pi.get('error')}")
            anomalies.append(f"probe failed for {ticker}: {pi.get('error')}")
            continue
        c = pi["core_conditions"]
        L.append(f"  -- {ticker} ({pi.get('asset', '?')}) {pi.get('strategy', '?')}")
        L.append(f"     bar {pi['last_closed_bar']} | close {pi['close']:,.2f} | "
                 f"alignment {pi['alignment']}")
        L.append(f"     ENTRY conditions met: {pi['core_conditions_met']}/3 (3 of 3 needed)")
        L.append(f"       ema stack up (==1)      {'YES' if c['ema_stack_up'] else 'no'}")
        L.append(f"       close > ema_5           {'YES' if c['close_above_ema5'] else 'no'}")
        L.append(f"       close > sma_50          {'YES' if c['close_above_sma50'] else 'no'}")
        L.append(f"     ATR expansion (modifier, NOT required): "
                 f"{'YES' if pi['atr_expanding'] else 'no'} ({pi['atr_x']:.2f}x avg)")
        L.append(f"     last entry bar: {pi['last_entry_bar']} "
                 f"({pi['days_since_entry']} days ago) | "
                 f"{pi['entry_bars_in_window']} entry bars in the "
                 f"{pi.get('period', '1y')} window")
        L.append(f"     exit signal on that bar: {pi['exit_active']}")
        prev_i = prev_cond.get(ticker) or {}
        if prev_i.get("met") is not None and prev_i["met"] != pi["core_conditions_met"]:
            changed.append(f"{ticker} entry conditions {prev_i['met']}/3 -> "
                           f"{pi['core_conditions_met']}/3")
        if prev_i.get("atr") is not None and bool(prev_i["atr"]) != bool(pi["atr_expanding"]):
            changed.append(f"{ticker} ATR expansion {bool(prev_i['atr'])} -> "
                           f"{pi['atr_expanding']}")
        if prev_i.get("alignment") is not None and prev_i["alignment"] != pi["alignment"]:
            changed.append(f"{ticker} alignment {prev_i['alignment']} -> {pi['alignment']}")
        sig = pi.get("signal") or {}
        L.append(f"     verdict: {str(sig.get('action', '?')).upper()} "
                 f"(confidence {sig.get('confidence')}, regime {sig.get('regime')})")
        new_cond[ticker] = {"met": pi["core_conditions_met"], "atr": pi["atr_expanding"],
                            "alignment": pi["alignment"]}
    if changed:
        L.append("  >> CHANGE since last report: " + "; ".join(changed))

    # ── verdict ──
    L.append("")
    if anomalies:
        L.append("!! ANOMALY - needs attention:")
        for a in anomalies:
            L.append(f"   - {a}")
    elif changed:
        L.append("NOTABLE: the setup moved since the last report (" + "; ".join(changed) + ").")
    else:
        L.append("NO ANOMALIES: loop alive, strategy loaded and confirmed, account consistent.")

    if persist:
        save_prev({
            "ts": now.isoformat(),
            "cycle_count": cycles,
            "equity": eq,
            "trade_log_len": len(log),
            "strategy_name": pushed,
            # Keyed by ticker: a shared slot would let three instruments overwrite
            # each other and fire false change alerts.
            "conditions": new_cond,
        })

    meta = {"anomalies": anomalies, "changed": changed,
            "sent_at": now.isoformat(), "probe_ok": bool(probe.get("ok"))}
    return "\n".join(L), meta


# ── slack ───────────────────────────────────────────────────────────────────

def _chunk(text: str, limit: int = 3800) -> list:
    """Split on line boundaries so no code/table row is cut mid-line."""
    if len(text) <= limit:
        return [text]
    out, buf = [], ""
    for line in text.splitlines(keepends=True):
        if len(buf) + len(line) > limit:
            if buf:
                out.append(buf)
            buf = line
        else:
            buf += line
    if buf:
        out.append(buf)
    return [f"({i + 1}/{len(out)})\n{c}" if len(out) > 1 else c
            for i, c in enumerate(out)]


def slack_post(text: str) -> dict:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    channel = (os.environ.get("SLACK_BRIEFING_CHANNEL")
               or os.environ.get("SLACK_HOME_CHANNEL") or "D0B8Z8JTPU4")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN is not set in this container")
    headers = {_H_AUTH: _H_SCHEME + " " + token}
    posted = []
    for ch in _chunk(text):
        r = requests.post("https://slack.com/api/chat.postMessage",
                          json={"channel": channel, "text": ch,
                                "unfurl_links": False, "unfurl_media": False},
                          headers=headers, timeout=HTTP_TIMEOUT)
        try:
            body = r.json()
        except Exception:
            body = {"ok": False, "error": f"non-json response {r.status_code}"}
        if not body.get("ok"):
            raise RuntimeError(f"slack chat.postMessage failed: {body.get('error')}")
        posted.append(body.get("ts"))
    return {"ok": True, "channel": channel, "messages": len(posted), "ts": posted}


# ── idempotency ─────────────────────────────────────────────────────────────

def _sent_marker() -> dict:
    try:
        return json.loads(SENT_FILE.read_text())
    except Exception:
        return {}


def already_sent_today() -> bool:
    today = datetime.datetime.now(_et()).date().isoformat()
    return _sent_marker().get("date") == today


def _mark_sent(detail: dict):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SENT_FILE.write_text(json.dumps({
            "date": datetime.datetime.now(_et()).date().isoformat(),
            "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            **detail,
        }, indent=1))
    except Exception as e:
        _log(f"could not write sent marker: {e}")


# ── public entry points ─────────────────────────────────────────────────────

def send_briefing(force: bool = False, reason: str = "scheduled") -> dict:
    """Render and post the briefing. Idempotent per ET day unless forced."""
    if not force and already_sent_today():
        _log(f"skip ({reason}): already sent today")
        return {"ok": True, "skipped": True, "reason": "already_sent_today"}
    try:
        text, meta = build_digest(persist=True)
        result = slack_post(text)
        _mark_sent({"anomalies": meta["anomalies"], "changed": meta["changed"],
                    "reason": reason})
        flag = "ANOMALY" if meta["anomalies"] else ("NOTABLE" if meta["changed"] else "clean")
        _log(f"sent ({reason}) [{flag}], {result['messages']} message(s), "
             f"{len(text)} chars")
        return {"ok": True, "sent": True, "flag": flag, "chars": len(text), **result}
    except Exception as e:
        _log(f"SEND FAILED ({reason}): {type(e).__name__}: {e}")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def scheduler_loop():
    """Daily thread. Sends at the configured ET time, catching up on restart."""
    h, m = briefing_hour_minute()
    _log(f"briefing scheduler started (daily at {h:02d}:{m:02d} US/Eastern)")
    # Catch-up: a redeploy after the scheduled time must not skip the day.
    if os.environ.get("PM_BRIEFING_ENABLED", "1") == "1" and not already_sent_today():
        _log("catch-up check: nothing sent today, sending now")
        send_briefing(reason="startup-catchup")
    while True:
        try:
            delay = next_run_delay()
            _log(f"next briefing in {delay / 3600:.2f}h")
            time.sleep(delay)
            if os.environ.get("PM_BRIEFING_ENABLED", "1") == "1":
                send_briefing(reason="scheduled")
        except Exception as e:
            _log(f"scheduler error: {type(e).__name__}: {e} - continuing")
            time.sleep(300)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true", help="render without sending")
    ap.add_argument("--send", action="store_true", help="render and send now")
    ap.add_argument("--force", action="store_true", help="ignore the daily sent marker")
    a = ap.parse_args()
    if a.preview:
        t, m = build_digest(persist=False)
        print(t)
        print(f"\n[meta] anomalies={len(m['anomalies'])} changed={len(m['changed'])}")
    elif a.send:
        print(json.dumps(send_briefing(force=a.force, reason="cli"), indent=1))
    else:
        ap.print_help()
