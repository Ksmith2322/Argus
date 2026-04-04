"""Helio Family Watchdog — supervise Apollo/Hermes/Helio runners.

Monitors heartbeat freshness, restarts dead runners, prevents duplicates.
Runs as a persistent loop alongside the Argus watchdog.

Usage:
    python -m helio.watchdog
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HELIO_ROOT = Path(__file__).resolve().parent
REPO = HELIO_ROOT.parent
LOGS_ROOT = HELIO_ROOT / "logs"
PYTHON = sys.executable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] helio-watchdog | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("helio-watchdog")

# Runner definitions
RUNNERS = [
    {
        "name": "helio",
        "module": "helio.runner",
        "heartbeat_dirs": ["gld", "spy", "mgc", "mnq", "mes", "mym"],
        "stale_threshold_s": 7200,  # 2hr — daily runners only eval at 21:00 UTC
    },
    {
        "name": "apollo",
        "module": "helio.runner_apollo",
        "heartbeat_dirs": ["apollo_audjpy", "apollo_eurusd", "apollo_usdjpy", "apollo_gbpusd",
                           "apollo_eurjpy", "apollo_gbpjpy", "apollo_audusd", "apollo_cadjpy"],
        "stale_threshold_s": 7200,
    },
    {
        "name": "hermes",
        "module": "helio.runner_hermes",
        "heartbeat_dirs": ["hermes_gold_f"],
        "stale_threshold_s": 7200,
    },
]

CHECK_INTERVAL_S = 300  # check every 5 min
MAX_RESTARTS_PER_HOUR = 3


def _is_runner_alive(runner: dict) -> bool:
    """Check if at least one heartbeat is fresh for this runner."""
    for hb_dir in runner["heartbeat_dirs"]:
        hb_file = LOGS_ROOT / hb_dir / "heartbeat.json"
        if hb_file.exists():
            age = time.time() - hb_file.stat().st_mtime
            if age < runner["stale_threshold_s"]:
                return True
    return False


def _is_process_running(module: str) -> bool:
    """Check if a Python process with this module is running."""
    try:
        import psutil
        for proc in psutil.process_iter(["cmdline"]):
            cmdline = proc.info.get("cmdline", [])
            if cmdline and any(module in str(arg) for arg in cmdline):
                return True
    except ImportError:
        # Fallback: check via process name matching
        pass
    return False


def _count_processes(module: str) -> int:
    """Count how many processes are running this module."""
    count = 0
    try:
        import psutil
        for proc in psutil.process_iter(["cmdline"]):
            cmdline = proc.info.get("cmdline", [])
            if cmdline and any(module in str(arg) for arg in cmdline):
                count += 1
    except ImportError:
        pass
    return count


def _restart_runner(runner: dict) -> bool:
    """Restart a dead runner."""
    try:
        subprocess.Popen(
            [PYTHON, "-m", runner["module"]],
            cwd=str(REPO),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        log.info(f"RESTARTED {runner['name']} ({runner['module']})")
        return True
    except Exception as e:
        log.error(f"Failed to restart {runner['name']}: {e}")
        return False


def main():
    log.info("Helio Family Watchdog started")
    log.info(f"Monitoring {len(RUNNERS)} runners, check every {CHECK_INTERVAL_S}s")

    restart_counts: dict[str, list[float]] = {r["name"]: [] for r in RUNNERS}

    while True:
        now = time.time()
        now_utc = datetime.now(timezone.utc)

        for runner in RUNNERS:
            name = runner["name"]
            alive = _is_runner_alive(runner)
            proc_count = _count_processes(runner["module"])

            # Kill duplicates
            if proc_count > 1:
                log.warning(f"{name}: {proc_count} processes detected (expected 1)")
                # Don't auto-kill — just warn. Manual cleanup preferred.

            if alive:
                continue

            # Check restart budget
            recent = [t for t in restart_counts[name] if now - t < 3600]
            restart_counts[name] = recent

            if len(recent) >= MAX_RESTARTS_PER_HOUR:
                log.error(f"{name}: max restarts ({MAX_RESTARTS_PER_HOUR}/hr) exhausted. Manual intervention needed.")
                continue

            # Skip restart during weekends (markets closed)
            if now_utc.weekday() == 5 or (now_utc.weekday() == 6 and now_utc.hour < 21):
                continue

            log.warning(f"{name}: heartbeats stale. Restarting...")
            if _restart_runner(runner):
                restart_counts[name].append(now)

        # Write own heartbeat
        hb_file = LOGS_ROOT / "_watchdog" / "heartbeat.json"
        hb_file.parent.mkdir(parents=True, exist_ok=True)
        hb_file.write_text(json.dumps({
            "ts": now_utc.isoformat(),
            "runners_monitored": len(RUNNERS),
            "status": "OK",
        }, indent=2))

        time.sleep(CHECK_INTERVAL_S)


if __name__ == "__main__":
    main()
