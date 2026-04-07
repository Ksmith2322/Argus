#!/usr/bin/env python3
"""
ops/watchdog.py  --  Phase 16 Supervisor / Watchdog Process

Wraps runner_live.py with:
  - auto-restart on crash (with exponential backoff)
  - health-check driven mode escalation
  - Discord alerting on HALT / anomaly / mode transitions
  - clean shutdown on KILL_SWITCH

Usage:
    python -m ops.watchdog

Or from repo root:
    C:\\Argus\\.venv\\Scripts\\python.exe ops/watchdog.py
"""

import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, Optional

# Ensure repo root is on sys.path so imports work
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import runtime_mode


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULTS: Dict[str, Any] = {
    "MAX_RESTARTS": 10,              # max restarts before giving up
    "INITIAL_BACKOFF_S": 5,          # first restart delay
    "MAX_BACKOFF_S": 300,            # max restart delay (5 min)
    "BACKOFF_MULTIPLIER": 2.0,       # exponential backoff factor
    "BACKOFF_RESET_AFTER_S": 600,    # reset backoff after this many seconds of healthy running
    "HEALTH_CHECK_INTERVAL_S": 60,   # how often to check health (between restarts)
    "PYTHON_EXE": os.path.join(_REPO_ROOT, "..", ".venv", "Scripts", "python.exe"),
    "RUNNER_MODULE": "runner_live",
}


def _load_watchdog_config() -> Dict[str, Any]:
    """Load watchdog config from environment, falling back to defaults."""
    cfg = dict(_DEFAULTS)
    for key in _DEFAULTS:
        env_key = f"ARGUS_WATCHDOG_{key}"
        env_val = os.environ.get(env_key, "").strip()
        if env_val:
            # Coerce to same type as default
            default_type = type(_DEFAULTS[key])
            try:
                if default_type == int:
                    cfg[key] = int(env_val)
                elif default_type == float:
                    cfg[key] = float(env_val)
                else:
                    cfg[key] = env_val
            except ValueError:
                pass
    return cfg


# ---------------------------------------------------------------------------
# Watchdog state persistence
# ---------------------------------------------------------------------------

def _watchdog_state_path() -> str:
    return os.path.join(_REPO_ROOT, "ops", "logs", "watchdog_state.json")


def _save_watchdog_state(state: Dict[str, Any]) -> None:
    path = _watchdog_state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    for _ in range(3):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)

    with open(path, "w") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.remove(tmp)
    except OSError:
        pass


def _load_watchdog_state() -> Dict[str, Any]:
    path = _watchdog_state_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Kill switch check
# ---------------------------------------------------------------------------

def _is_kill_switch_on() -> bool:
    """Check for kill switch file (same as io_logs)."""
    ks_file = os.environ.get("KILL_SWITCH_FILE", "").strip() or "KILL_SWITCH.txt"
    # Check both repo root and absolute path
    if os.path.isabs(ks_file):
        return os.path.exists(ks_file)
    return os.path.exists(os.path.join(_REPO_ROOT, ks_file))


# ---------------------------------------------------------------------------
# Discord alerting (thin wrapper)
# ---------------------------------------------------------------------------

def _notify_discord(title: str, msg: str) -> None:
    """Best-effort Discord notification."""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return
    try:
        import requests
        payload = {"content": f"**{title}**\n{msg}"}
        requests.post(webhook_url, json=payload, timeout=10).raise_for_status()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Core watchdog loop
# ---------------------------------------------------------------------------

def run_watchdog() -> int:
    """Main watchdog loop. Returns exit code."""
    cfg = _load_watchdog_config()

    python_exe = cfg["PYTHON_EXE"]
    runner_module = cfg["RUNNER_MODULE"]
    max_restarts = cfg["MAX_RESTARTS"]
    initial_backoff = cfg["INITIAL_BACKOFF_S"]
    max_backoff = cfg["MAX_BACKOFF_S"]
    multiplier = cfg["BACKOFF_MULTIPLIER"]
    reset_after = cfg["BACKOFF_RESET_AFTER_S"]

    restart_count = 0
    backoff = initial_backoff

    print(f"[WATCHDOG] Starting supervisor for {runner_module}")
    print(f"[WATCHDOG] python={python_exe} max_restarts={max_restarts}")
    print(f"[WATCHDOG] backoff: initial={initial_backoff}s max={max_backoff}s multiplier={multiplier}")

    _save_watchdog_state({
        "status": "STARTING",
        "restart_count": 0,
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })

    while restart_count < max_restarts:
        if _is_kill_switch_on():
            print("[WATCHDOG] Kill switch detected. Stopping.")
            _notify_discord("WATCHDOG STOP", "Kill switch detected, watchdog exiting.")
            _save_watchdog_state({
                "status": "KILLED",
                "restart_count": restart_count,
                "ts": time.time(),
                "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            return 0

        # Check runtime mode
        current_mode, _ = runtime_mode.load_mode()
        if current_mode == runtime_mode.RECONCILIATION_ONLY:
            print(f"[WATCHDOG] Mode is {current_mode}, not starting runner.")
            _save_watchdog_state({
                "status": "MODE_BLOCKED",
                "mode": current_mode,
                "restart_count": restart_count,
                "ts": time.time(),
                "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            time.sleep(cfg["HEALTH_CHECK_INTERVAL_S"])
            continue

        run_start = time.time()
        print(f"[WATCHDOG] Starting runner (attempt {restart_count + 1}/{max_restarts})")

        _save_watchdog_state({
            "status": "RUNNING",
            "restart_count": restart_count,
            "run_start": run_start,
            "ts": time.time(),
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })

        try:
            result = subprocess.run(
                [python_exe, "-m", runner_module],
                cwd=_REPO_ROOT,
                timeout=None,  # no timeout -- runner runs indefinitely
            )
            exit_code = result.returncode
        except KeyboardInterrupt:
            print("[WATCHDOG] Keyboard interrupt. Exiting.")
            _save_watchdog_state({
                "status": "INTERRUPTED",
                "restart_count": restart_count,
                "ts": time.time(),
                "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            return 0
        except Exception as e:
            exit_code = -1
            print(f"[WATCHDOG] Runner process error: {e}")

        run_duration = time.time() - run_start

        # Clean exit (code 0) means normal shutdown -- don't restart
        if exit_code == 0:
            print(f"[WATCHDOG] Runner exited cleanly (code 0) after {run_duration:.0f}s.")
            _save_watchdog_state({
                "status": "CLEAN_EXIT",
                "restart_count": restart_count,
                "run_duration_s": round(run_duration, 1),
                "ts": time.time(),
                "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            return 0

        # Crash or non-zero exit
        restart_count += 1
        print(f"[WATCHDOG] Runner exited with code {exit_code} after {run_duration:.0f}s. "
              f"Restart {restart_count}/{max_restarts}.")

        _notify_discord(
            "WATCHDOG RESTART",
            f"Runner crashed (exit code {exit_code}) after {run_duration:.0f}s. "
            f"Restart {restart_count}/{max_restarts}. Backoff {backoff:.0f}s.",
        )

        # Reset backoff if runner ran long enough to be considered healthy
        if run_duration >= reset_after:
            backoff = initial_backoff
            restart_count = max(0, restart_count - 1)  # forgive one restart
            print(f"[WATCHDOG] Runner ran {run_duration:.0f}s (>{reset_after}s), resetting backoff.")
        else:
            # Wait with backoff before restarting
            if _is_kill_switch_on():
                print("[WATCHDOG] Kill switch detected during backoff. Stopping.")
                return 0
            print(f"[WATCHDOG] Waiting {backoff:.0f}s before restart...")
            time.sleep(backoff)
            backoff = min(max_backoff, backoff * multiplier)

        _save_watchdog_state({
            "status": "RESTARTING",
            "restart_count": restart_count,
            "next_backoff_s": round(backoff, 1),
            "last_exit_code": exit_code,
            "last_run_duration_s": round(run_duration, 1),
            "ts": time.time(),
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })

    # Exhausted restarts
    msg = f"Max restarts ({max_restarts}) exhausted. Entering OBSERVATION_ONLY mode."
    print(f"[WATCHDOG] {msg}")
    _notify_discord("WATCHDOG EXHAUSTED", msg)

    runtime_mode.escalate(
        runtime_mode.OBSERVATION_ONLY,
        reason="watchdog_restarts_exhausted",
        triggered_by="watchdog",
    )

    _save_watchdog_state({
        "status": "EXHAUSTED",
        "restart_count": restart_count,
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })

    return 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(run_watchdog())
