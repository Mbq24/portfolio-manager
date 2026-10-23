#!/usr/bin/env python3
"""
portfolio_server.py — Always-on Portfolio Manager service.

Runs the portfolio-manager cycle on a schedule (default: hourly, at minute 5
past the hour) inside an always-on container, and exposes the state over HTTP
so the Hermes gateway / Slack bot / Forge control room can read it without a
local machine.

Endpoints:
    GET /health                    liveness probe + scheduler heartbeat
    GET /api/portfolio/state       full state + config + recent trades
    GET /api/portfolio/config      effective config
    POST /api/portfolio/cycle      trigger a cycle now (optional token)
    GET /api/briefing/preview      render the daily briefing without sending it
    POST /api/briefing/send        send the daily briefing now (optional token)

The cycle itself is run.py — the SAME code the launchd hook ran locally. The
server just schedules it and serves the results. State/journal persist under
the data/ dir, which must be a mounted volume so redeploys don't lose history.

v2 fixes (Aug 2026):
  - _next_run_delay() used (hour+1)%24 rollover which produced a NEGATIVE
    delay at 23:05 ("00:05 today" is in the past) -> time.sleep() raised
    ValueError -> scheduler thread died silently on night one and never
    restarted; the HTTP server kept answering 200s while the trading loop
    was dead. Now uses timedelta(hours=1) (date-safe) and clamps delay >= 0.
  - Scheduler loop body wrapped in try/except with logging so a single bad
    cycle can never kill the thread again.
  - /health now reports last_cycle_at so liveness of the LOOP (not just the
    web server) is observable from outside.
"""

import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, jsonify, request

from portfolio.config import load as effective_config

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
CYCLE_INTERVAL_S = int(os.environ.get("PM_CYCLE_INTERVAL_S", "3600"))
CYCLE_MINUTE = int(os.environ.get("PM_CYCLE_MINUTE", "5"))  # run at :05 past the hour
CYCLE_TOKEN = os.environ.get("PM_CYCLE_TOKEN", "")  # optional guard for manual trigger

app = Flask(__name__)

# Scheduler heartbeat state
_lock = threading.Lock()
_last_cycle_at = None
_last_cycle_ok = None
_cycle_count = 0


def _run_cycle() -> str:
    """Run one portfolio-manager cycle (run.py) and return its stdout."""
    env = dict(os.environ)
    proc = subprocess.run(
        [sys.executable, "run.py"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    out = proc.stdout or ""
    if proc.returncode != 0:
        out += f"\n[cycle exit code {proc.returncode}]\n{proc.stderr or ''}"
    return out


def _log(msg: str):
    ts = datetime.now(timezone.utc).isoformat()
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(DATA_DIR / "server.log", "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _next_run_delay() -> float:
    """Seconds until the next scheduled cycle (minute CYCLE_MINUTE each hour).

    Uses timedelta for the rollover so day boundaries are handled correctly:
    at 23:05 the next run is 00:05 TOMORROW, never "00:05 today" (which is in
    the past and produced a negative delay that crashed the scheduler).
    """
    now = datetime.now()
    target = now.replace(minute=CYCLE_MINUTE, second=0, microsecond=0)
    if target <= now:
        target += timedelta(hours=1)
    return max((target - now).total_seconds(), 0.0)


def scheduler_loop():
    """Background thread: run a cycle at startup, then on schedule."""
    global _last_cycle_at, _last_cycle_ok, _cycle_count
    _log(f"Scheduler started (interval={CYCLE_INTERVAL_S}s, minute={CYCLE_MINUTE})")
    # Run once at boot so the service is immediately useful after a redeploy.
    time.sleep(5)
    _log("Boot cycle start")
    try:
        out = _run_cycle()
        with _lock:
            _last_cycle_at = datetime.now(timezone.utc).isoformat()
            _last_cycle_ok = True
            _cycle_count += 1
        _log(f"Boot cycle done ({len(out)} chars)")
    except Exception as e:
        with _lock:
            _last_cycle_at = datetime.now(timezone.utc).isoformat()
            _last_cycle_ok = False
            _cycle_count += 1
        _log(f"Boot cycle FAILED: {e}")

    while True:
        try:
            delay = _next_run_delay()
            _log(f"Next cycle in {delay/60:.1f} min")
            time.sleep(delay)
            _log("Scheduled cycle start")
            out = _run_cycle()
            with _lock:
                _last_cycle_at = datetime.now(timezone.utc).isoformat()
                _last_cycle_ok = True
                _cycle_count += 1
            _log(f"Scheduled cycle done ({len(out)} chars)")
        except Exception as e:
            # Never let a single failure kill the loop — log, record, continue.
            with _lock:
                _last_cycle_at = datetime.now(timezone.utc).isoformat()
                _last_cycle_ok = False
                _cycle_count += 1
            _log(f"Scheduled cycle FAILED: {e} — continuing")


@app.route("/health")
def health():
    with _lock:
        last = _last_cycle_at
        ok = _last_cycle_ok
        count = _cycle_count
    # Briefing status is part of host health: if it stops posting, the daily
    # update is missing even though the trading loop looks fine.
    briefing: dict = {}
    try:
        from briefing import (briefing_hour_minute, _sent_marker, next_run_delay,
                              briefing_enabled)
        h, m = briefing_hour_minute()
        briefing.update({
            "enabled": briefing_enabled(),
            "at": f"{h:02d}:{m:02d} US/Eastern",
            "next_in_min": round(next_run_delay() / 60, 1),
            "last_sent": _sent_marker() or None,
        })
    except Exception as e:
        # briefing failed to import: it cannot run, so report disabled rather than
        # claiming health we don't have.
        briefing = {"enabled": False, "error": f"{type(e).__name__}: {e}"}
    return jsonify({
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
        "scheduler": {
            "last_cycle_at": last,
            "last_cycle_ok": ok,
            "cycle_count": count,
            "next_cycle_in_min": round(_next_run_delay() / 60, 1),
        },
        "briefing": briefing,
    })


@app.route("/api/portfolio/state")
def portfolio_state():
    state_file = DATA_DIR / "state.json"
    if not state_file.exists():
        return jsonify({"error": "no state yet — first cycle may still be running"}), 404
    try:
        import json
        state = json.loads(state_file.read_text())
        # Attach the EFFECTIVE config (normalized) — the same source of truth the loop
        # uses, read from the volume. Exposing the raw file would hide the
        # legacy→instruments migration that the probe and digest both key off.
        state["config"] = effective_config()
        return jsonify(state)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/portfolio/config")
def portfolio_config():
    """The effective config — what the loop will actually run, normalized."""
    return jsonify(effective_config())


@app.route("/api/portfolio/cycle", methods=["POST"])
def trigger_cycle():
    if CYCLE_TOKEN:
        token = request.headers.get("X-Cycle-Token", "")
        if token != CYCLE_TOKEN:
            return jsonify({"error": "bad token"}), 401

    def _trigger():
        try:
            out = _run_cycle()
            with _lock:
                global _last_cycle_at, _last_cycle_ok, _cycle_count
                _last_cycle_at = datetime.now(timezone.utc).isoformat()
                _last_cycle_ok = True
                _cycle_count += 1
            _log(f"Manual cycle done ({len(out)} chars)")
        except Exception as e:
            _log(f"Manual cycle FAILED: {e}")

    threading.Thread(target=_trigger, daemon=True).start()
    return jsonify({"status": "cycle started"})


@app.route("/api/briefing/preview")
def briefing_preview():
    """Render the daily briefing exactly as it would be sent, without sending.

    persist=False is the important detail: previewing must not consume the
    delta baseline (cycle/equity/condition snapshots) that the next real
    report is compared against.
    """
    from briefing import already_sent_today, build_digest
    text, meta = build_digest(persist=False)
    return jsonify({"text": text, "meta": meta, "sent_today": already_sent_today()})


@app.route("/api/briefing/send", methods=["POST"])
def briefing_send():
    if CYCLE_TOKEN:
        token = request.headers.get("X-Cycle-Token", "")
        if token != CYCLE_TOKEN:
            return jsonify({"error": "bad token"}), 401
    from briefing import send_briefing
    # force defaults to on for a manual send: asking for it is explicit intent.
    force = request.args.get("force", "1") != "0"
    return jsonify(send_briefing(force=force, reason="http"))


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    # The briefing scheduler lives in the same always-on container as the trading
    # loop, which is the whole point: it no longer depends on a machine that sleeps.
    # Log BOTH outcomes. Previously the disabled case printed nothing, so a briefing
    # that had never been switched on looked identical to one that was running
    # normally — the absence of a log line was the only clue, and nobody looks for
    # the absence of a line.
    try:
        from briefing import (scheduler_loop as briefing_scheduler_loop,
                              briefing_enabled)
        if briefing_enabled():
            threading.Thread(target=briefing_scheduler_loop, daemon=True).start()
            _log("Briefing scheduler thread started")
        else:
            _log("Briefing scheduler DISABLED (PM_BRIEFING_ENABLED is off) — "
                 "no daily briefing will be sent")
    except Exception as e:
        _log(f"Briefing scheduler FAILED to start: {e}")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
