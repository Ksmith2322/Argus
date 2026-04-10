"""helio/fleet_monitor.py -- Unified fleet observability + watchdog.

Single process that handles:
  1. Heartbeat staleness checks for all 5 systems (every 60s)
  2. Auto-restart crashed runners (Argus/Titan/Hermes/Apollo)
  3. Daily fleet snapshots (positions, PnL, risk)
  4. Discord alerts on staleness/crashes
  5. Trade quality metrics aggregation

Usage:
    python -m helio.fleet_monitor              # run forever
    python -m helio.fleet_monitor --once       # single check + exit
    python -m helio.fleet_monitor --snapshot   # take snapshot only
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv
load_dotenv(REPO / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] fleet | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(REPO / "argus_flow" / "logs" / "fleet_monitor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("fleet_monitor")

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# Per-system config
SYSTEMS = {
    "argus": {
        "heartbeats": [
            REPO / "argus_flow" / "logs" / sym / "heartbeat.json"
            for sym in ["audjpy", "usdjpy", "gbpusd", "cadjpy"]
        ],
        "stale_threshold_s": 300,  # 5 min
        "process_match": "runner_unified",
        "restart_args": [
            "-m", "argus_flow.runner_unified", "--configs",
            "argus_flow/configs/audjpy_mtf_paper_v1.json",
            "argus_flow/configs/usdjpy_mtf_paper_v1.json",
            "argus_flow/configs/gbpusd_range_paper_v1.json",
            "argus_flow/configs/cadjpy_mtf_paper_v1.json",
        ],
    },
    "titan": {
        "heartbeats": [REPO / "titan" / "logs" / "heartbeat.json"],
        "stale_threshold_s": 4500,  # 75 min (loop is 60min + buffer)
        "process_match": "titan.runner",
        "restart_args": ["-m", "titan.runner", "--loop", "--interval-min", "60"],
    },
    "hermes": {
        "heartbeats": [REPO / "hermes" / "logs" / "heartbeat.json"],
        "stale_threshold_s": 8400,  # 140 min (loop is 120min + buffer)
        "process_match": "hermes.runner",
        "restart_args": ["-m", "hermes.runner", "--loop", "--interval-min", "120", "--min-score", "80"],
    },
    "apollo": {
        "heartbeats": [REPO / "apollo" / "logs" / "heartbeat.json"],
        "stale_threshold_s": 16200,  # 270 min (loop is 240min + buffer)
        "process_match": "apollo.runner",
        "restart_args": ["-m", "apollo.runner", "--loop", "--interval-min", "240", "--days", "14"],
    },
    "dashboard": {
        "heartbeats": [],  # no heartbeat, check via process only
        "stale_threshold_s": 0,
        "process_match": "dashboard.py",
        "restart_args": ["ops/dashboard.py", "--port", "8080"],
    },
}

PYTHON = r"C:\Argus\.venv\Scripts\python.exe"
SNAPSHOT_DIR = REPO / "argus_flow" / "logs" / "fleet_snapshots"
ALERT_COOLDOWN_S = 1800  # don't alert same issue more than once per 30 min
_last_alert = {}  # system -> timestamp


# ── Heartbeat checks ─────────────────────────────────────────

def get_heartbeat_age(path: Path) -> float | None:
    """Return age in seconds, or None if file doesn't exist."""
    if not path.exists():
        return None
    return time.time() - path.stat().st_mtime


def check_system_health(name: str, cfg: dict) -> dict:
    """Check if a system is healthy. Returns status dict."""
    result = {"name": name, "status": "UNKNOWN", "stale_count": 0, "details": []}
    process_alive = is_process_running(cfg["process_match"])
    result["process_alive"] = process_alive

    if cfg["heartbeats"]:
        ages = []
        for hb in cfg["heartbeats"]:
            age = get_heartbeat_age(hb)
            if age is None:
                result["stale_count"] += 1
                result["details"].append(f"{hb.parent.name}: NO_HEARTBEAT")
            elif age > cfg["stale_threshold_s"]:
                result["stale_count"] += 1
                ages.append(age)
                result["details"].append(f"{hb.parent.name}: stale {int(age)}s")
            else:
                ages.append(age)
        if ages:
            result["max_age_s"] = int(max(ages))

    if not process_alive:
        result["status"] = "DOWN"
    elif result["stale_count"] > 0:
        result["status"] = "STALE"
    else:
        result["status"] = "OK"

    return result


def is_process_running(pattern: str) -> bool:
    """Check if a python process matches a substring pattern."""
    try:
        result = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "CommandLine"],
            capture_output=True, text=True, timeout=10,
        )
        return pattern in result.stdout
    except Exception:
        return False


# ── Restart handler ─────────────────────────────────────────

def restart_system(name: str, cfg: dict) -> bool:
    """Auto-restart a crashed/stale system."""
    log.warning(f"Restarting {name}...")
    try:
        subprocess.Popen(
            [PYTHON] + cfg["restart_args"],
            cwd=str(REPO),
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0,
        )
        send_discord(f"**FLEET MONITOR: Restarted {name.upper()}** (was DOWN/STALE)")
        return True
    except Exception as e:
        log.error(f"Restart failed for {name}: {e}")
        return False


def send_discord(msg: str, system: str = "general") -> None:
    """Send Discord alert with cooldown to prevent spam."""
    now = time.time()
    last = _last_alert.get(system, 0)
    if now - last < ALERT_COOLDOWN_S:
        return
    _last_alert[system] = now

    if not WEBHOOK_URL:
        return
    try:
        import requests
        requests.post(WEBHOOK_URL, json={"content": msg[:2000]}, timeout=10)
    except Exception:
        pass


# ── Snapshots ──────────────────────────────────────────────

def take_snapshot() -> dict:
    """Capture current state of every system to a JSON snapshot."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    snapshot = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "systems": {},
    }

    # Argus
    argus_data = {"trades": 0, "win_rate": 0, "total_pnl": 0, "pairs": {}}
    for sym in ["audjpy", "usdjpy", "gbpusd", "cadjpy"]:
        trades_path = REPO / "argus_flow" / "logs" / sym / "trades.csv"
        if trades_path.exists():
            try:
                with open(trades_path) as f:
                    rows = [r for r in csv.DictReader(f) if r.get("experiment_valid", "").lower() == "true"]
                pnls = [float(r.get("pnl_pips", 0)) for r in rows]
                wins = sum(1 for p in pnls if p > 0)
                argus_data["trades"] += len(rows)
                argus_data["pairs"][sym] = {
                    "trades": len(rows),
                    "wins": wins,
                    "wr": round(wins / len(rows) * 100, 1) if rows else 0,
                    "pnl": round(sum(pnls), 2),
                }
                argus_data["total_pnl"] += sum(pnls)
            except Exception:
                pass
    if argus_data["trades"] > 0:
        total_wins = sum(p["wins"] for p in argus_data["pairs"].values())
        argus_data["win_rate"] = round(total_wins / argus_data["trades"] * 100, 1)
        argus_data["total_pnl"] = round(argus_data["total_pnl"], 2)
    snapshot["systems"]["argus"] = argus_data

    # Titan / Hermes / Apollo / Ares — read from positions + trades
    for sys_name in ["titan", "hermes", "apollo", "ares"]:
        sys_data = {"positions": 0, "trades": 0, "pnl": 0}

        pos_path = REPO / sys_name / "logs" / "positions.json"
        if pos_path.exists():
            try:
                pos = json.loads(pos_path.read_text())
                if sys_name == "apollo":
                    sys_data["positions"] = len(pos.get("positions", {}))
                elif sys_name == "ares":
                    sys_data["positions"] = len(pos.get("holdings", {}))
                else:
                    # titan/hermes use top-level dict (titan has special keys)
                    skip_keys = {"last_rebalance", "last_signal", "holdings"}
                    sys_data["positions"] = sum(
                        1 for k in pos.keys() if k not in skip_keys
                    )
            except Exception:
                pass

        trades_path = REPO / sys_name / "logs" / "trades.csv"
        if trades_path.exists():
            try:
                with open(trades_path) as f:
                    rows = list(csv.DictReader(f))
                sys_data["trades"] = len(rows)
                pnls = [float(r.get("pnl_pct", 0)) for r in rows]
                sys_data["pnl"] = round(sum(pnls), 2)
                if rows:
                    wins = sum(1 for p in pnls if p > 0)
                    sys_data["win_rate"] = round(wins / len(rows) * 100, 1)
            except Exception:
                pass

        snapshot["systems"][sys_name] = sys_data

    # Save snapshot
    fname = SNAPSHOT_DIR / f"snapshot_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    fname.write_text(json.dumps(snapshot, indent=2))
    log.info(f"Snapshot saved: {fname.name}")
    return snapshot


# ── Trade quality metrics ──────────────────────────────────

def collect_trade_metrics() -> dict:
    """Aggregate trade quality across all systems."""
    metrics = {}

    for sys_name in ["titan", "hermes", "apollo", "ares"]:
        orders_path = REPO / sys_name / "logs" / "orders.csv"
        if not orders_path.exists():
            metrics[sys_name] = {"orders": 0}
            continue
        try:
            with open(orders_path) as f:
                rows = list(csv.DictReader(f))
            metrics[sys_name] = {
                "orders": len(rows),
                "entries": sum(1 for r in rows if r.get("action") == "ENTRY"),
                "closes": sum(1 for r in rows if r.get("action") == "CLOSE"),
            }
        except Exception:
            metrics[sys_name] = {"orders": 0}

    return metrics


# ── Main monitor loop ───────────────────────────────────────

def run_check_cycle(auto_restart: bool = True) -> dict:
    """One full health check cycle. Returns status dict."""
    overall = {"ts": datetime.now(timezone.utc).isoformat(), "systems": {}}

    for name, cfg in SYSTEMS.items():
        status = check_system_health(name, cfg)
        overall["systems"][name] = status

        if status["status"] in ("DOWN", "STALE"):
            log.warning(f"{name}: {status['status']} | {' | '.join(status['details'][:3])}")
            if auto_restart and status["status"] == "DOWN":
                restart_system(name, cfg)
        else:
            log.info(f"{name}: OK")

    return overall


def write_status(status: dict) -> None:
    """Write current status to a file the dashboard can read."""
    out = REPO / "argus_flow" / "logs" / "fleet_status.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(status, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Fleet Monitor")
    parser.add_argument("--once", action="store_true", help="Single check + exit")
    parser.add_argument("--snapshot", action="store_true", help="Take snapshot only")
    parser.add_argument("--no-restart", action="store_true", help="Don't auto-restart")
    parser.add_argument("--interval-s", type=int, default=60, help="Check interval seconds")
    parser.add_argument("--snapshot-interval-h", type=int, default=24, help="Snapshot interval hours")
    args = parser.parse_args()

    if args.snapshot:
        snap = take_snapshot()
        print(json.dumps(snap, indent=2))
        return

    if args.once:
        status = run_check_cycle(auto_restart=not args.no_restart)
        write_status(status)
        print(json.dumps(status, indent=2))
        return

    log.info("Fleet monitor starting...")
    log.info(f"Interval: {args.interval_s}s | Snapshot: every {args.snapshot_interval_h}h | Auto-restart: {not args.no_restart}")

    last_snapshot_time = 0
    snapshot_interval_s = args.snapshot_interval_h * 3600

    try:
        while True:
            try:
                status = run_check_cycle(auto_restart=not args.no_restart)
                write_status(status)

                # Daily snapshot
                if time.time() - last_snapshot_time > snapshot_interval_s:
                    take_snapshot()
                    last_snapshot_time = time.time()

            except Exception as e:
                log.error(f"Monitor cycle error: {e}")

            time.sleep(args.interval_s)

    except KeyboardInterrupt:
        log.info("Fleet monitor stopped")


if __name__ == "__main__":
    main()
