#!/usr/bin/env python3
"""ops/health_check.py -- Quick health check for Argus system components.

Usage:
    python ops/health_check.py              # check all, print report
    python ops/health_check.py --discord    # also send to Discord
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

STATE_DIR = REPO / "state"
OPS_LOGS = REPO / "ops" / "logs"


def check_runner():
    """Check if runner_live state is fresh."""
    state_path = STATE_DIR / "runtime_state_ETH_USD.json"
    if not state_path.exists():
        return "WARN", "No runtime state file"
    try:
        with open(state_path) as f:
            state = json.load(f)
        saved_at = state.get("saved_at", 0)
        age_s = time.time() - saved_at
        bot_state = state.get("bot_state", "UNKNOWN")
        cash = float(state.get("cash", 0))
        pnl = float(state.get("realized_pnl", 0))

        if age_s > 120:
            return "CRIT", f"State stale ({int(age_s)}s old) — runner may be dead"
        return "OK", f"state={bot_state} cash=${cash:.2f} pnl=${pnl:.4f} age={int(age_s)}s"
    except Exception as e:
        return "WARN", f"State read error: {e}"


def check_price_feed():
    """Check if we're getting fresh price data."""
    sig_path = OPS_LOGS / "live_signals.csv"
    if not sig_path.exists():
        return "WARN", "No live_signals.csv"
    try:
        # Read last line
        import csv
        last_row = {}
        with open(sig_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                last_row = row
        if not last_row:
            return "WARN", "Empty signals file"
        epoch = int(last_row.get("epoch", 0))
        age_s = time.time() - epoch
        px = last_row.get("px", "?")
        if age_s > 120:
            return "CRIT", f"Last signal {int(age_s)}s ago — feed may be down (px=${px})"
        return "OK", f"px=${px} age={int(age_s)}s"
    except Exception as e:
        return "WARN", f"Signal read error: {e}"


def check_disk():
    """Check disk space on C: drive."""
    try:
        import shutil
        total, used, free = shutil.disk_usage("C:\\")
        free_gb = free / (1024**3)
        if free_gb < 5:
            return "CRIT", f"Only {free_gb:.1f} GB free on C:"
        if free_gb < 20:
            return "WARN", f"{free_gb:.1f} GB free on C:"
        return "OK", f"{free_gb:.1f} GB free on C:"
    except Exception as e:
        return "WARN", f"Disk check error: {e}"


def check_logs_size():
    """Check if logs directory is getting too large."""
    try:
        total_mb = 0
        for f in OPS_LOGS.rglob("*"):
            if f.is_file():
                total_mb += f.stat().st_size / (1024 * 1024)
        if total_mb > 5000:
            return "WARN", f"Logs dir: {total_mb:.0f} MB (consider cleanup)"
        return "OK", f"Logs dir: {total_mb:.0f} MB"
    except Exception as e:
        return "WARN", f"Logs check error: {e}"


def check_governor():
    """Check if ML governor model exists and is loaded."""
    model_path = REPO / "data" / "ml_governor.pkl"
    if not model_path.exists():
        return "INFO", "No governor model (data/ml_governor.pkl)"
    try:
        age_days = (time.time() - model_path.stat().st_mtime) / 86400
        size_kb = model_path.stat().st_size / 1024
        return "OK", f"Governor model: {size_kb:.0f} KB, {age_days:.1f} days old"
    except Exception as e:
        return "WARN", f"Governor check error: {e}"


def check_backtest_queue():
    """Check queue status."""
    q1 = REPO / "ops" / "backtest_queue.jsonl"
    q2 = REPO / "ops" / "backtest_queue_pc2.jsonl"
    pending = 0
    if q1.exists():
        pending += sum(1 for l in open(q1) if l.strip())
    if q2.exists():
        pending += sum(1 for l in open(q2) if l.strip())
    if pending > 0:
        return "INFO", f"{pending} queued backtest jobs"
    return "OK", "No pending queue jobs"


def main():
    send_discord = "--discord" in sys.argv

    checks = [
        ("Runner", check_runner),
        ("Price Feed", check_price_feed),
        ("Disk", check_disk),
        ("Logs", check_logs_size),
        ("Governor", check_governor),
        ("Queue", check_backtest_queue),
    ]

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"=== Argus Health Check — {ts} ===\n")

    results = []
    any_crit = False
    any_warn = False
    for name, fn in checks:
        status, detail = fn()
        icon = {"OK": "+", "WARN": "!", "CRIT": "X", "INFO": "~"}[status]
        print(f"  [{icon}] {name:15s} {detail}")
        results.append((name, status, detail))
        if status == "CRIT":
            any_crit = True
        if status == "WARN":
            any_warn = True

    print()
    if any_crit:
        print("OVERALL: CRITICAL — immediate attention needed")
    elif any_warn:
        print("OVERALL: WARNING — review items above")
    else:
        print("OVERALL: HEALTHY")

    if send_discord:
        lines = [f"**Argus Health Check** — {ts}"]
        for name, status, detail in results:
            icon = {"OK": "✅", "WARN": "⚠️", "CRIT": "🔴", "INFO": "ℹ️"}[status]
            lines.append(f"{icon} **{name}**: {detail}")

        overall = "🔴 CRITICAL" if any_crit else "⚠️ WARNING" if any_warn else "✅ HEALTHY"
        lines.append(f"\n**Overall: {overall}**")

        try:
            from ops.notify import send_discord as _send
            _send("\n".join(lines))
            print("[Discord notification sent]")
        except Exception as e:
            print(f"[Discord failed: {e}]")


if __name__ == "__main__":
    main()