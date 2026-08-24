#!/usr/bin/env python3
"""
portfolio_server.py — Always-on Portfolio Manager service.

Runs the portfolio-manager cycle on a schedule (default: hourly, at minute 5
past the hour) inside an always-on container, and exposes the state over HTTP
so the Hermes gateway / Slack bot / Forge control room can read it without a
local machine.

Endpoints:
    GET /health                    liveness probe
    GET /api/portfolio/state       full state + config + recent trades
    GET /api/portfolio/config      effective config
    POST /api/portfolio/cycle      trigger a cycle now (optional token)

The cycle itself is run.py — the SAME code the launchd hook ran locally. The
server just schedules it and serves the results. State/journal persist under
the data/ dir, which must be a mounted volume so redeploys don't lose history.
"""

import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
CYCLE_INTERVAL_S = int(os.environ.get("PM_CYCLE_INTERVAL_S", "3600"))
CYCLE_MINUTE = int(os.environ.get("PM_CYCLE_MINUTE", "5"))  # run at :05 past the hour
CYCLE_TOKEN = os.environ.get("PM_CYCLE_TOKEN", "")  # optional guard for manual trigger

app = Flask(__name__)


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
    """Seconds until the next scheduled cycle (minute CYCLE_MINUTE each hour)."""
    now = datetime.now()
    target = now.replace(minute=CYCLE_MINUTE, second=0, microsecond=0)
    if target <= now:
        target = target.replace(hour=(target.hour + 1) % 24)
    return (target - now).total_seconds()


def scheduler_loop():
    """Background thread: run a cycle at startup, then on schedule."""
    _log(f"Scheduler started (interval={CYCLE_INTERVAL_S}s, minute={CYCLE_MINUTE})")
    # Run once at boot so the service is immediately useful after a redeploy.
    time.sleep(5)
    _log("Boot cycle start")
    try:
        out = _run_cycle()
        _log(f"Boot cycle done ({len(out)} chars)")
    except Exception as e:
        _log(f"Boot cycle FAILED: {e}")

    while True:
        delay = _next_run_delay()
        _log(f"Next cycle in {delay/60:.1f} min")
        time.sleep(delay)
        _log("Scheduled cycle start")
        try:
            out = _run_cycle()
            _log(f"Scheduled cycle done ({len(out)} chars)")
        except Exception as e:
            _log(f"Scheduled cycle FAILED: {e}")


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})


@app.route("/api/portfolio/state")
def portfolio_state():
    state_file = DATA_DIR / "state.json"
    if not state_file.exists():
        return jsonify({"error": "no state yet — first cycle may still be running"}), 404
    try:
        import json
        state = json.loads(state_file.read_text())
        # Attach config for convenience
        cfg_file = ROOT / "config.json"
        if cfg_file.exists():
            import json as _j
            state["config"] = _j.loads(cfg_file.read_text())
        return jsonify(state)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/portfolio/config")
def portfolio_config():
    cfg_file = ROOT / "config.json"
    if not cfg_file.exists():
        return jsonify({"error": "no config"}), 404
    import json
    return jsonify(json.loads(cfg_file.read_text()))


@app.route("/api/portfolio/cycle", methods=["POST"])
def trigger_cycle():
    if CYCLE_TOKEN:
        token = request.headers.get("X-Cycle-Token", "")
        if token != CYCLE_TOKEN:
            return jsonify({"error": "bad token"}), 401
    thread = threading.Thread(target=lambda: _log(_run_cycle()[:500]), daemon=True)
    thread.start()
    return jsonify({"status": "cycle started"})


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
