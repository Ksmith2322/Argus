"""Heartbeat Monitor — check if runners are alive based on file activity.

Usage:
    python -m argus_flow.ops.heartbeat_monitor
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

RUNNERS = [
    {"name": "EUR/USD", "log_dir": "argus_flow/logs/eurusd"},
    {"name": "MNQ", "log_dir": "argus_flow/logs/mnq"},
    {"name": "GBP/USD", "log_dir": "argus_flow/logs/gbpusd"},
    {"name": "GBP/JPY", "log_dir": "argus_flow/logs/gbpjpy"},
    {"name": "AUD/USD", "log_dir": "argus_flow/logs/audusd"},
    {"name": "USD/JPY", "log_dir": "argus_flow/logs/usdjpy"},
    {"name": "MES", "log_dir": "argus_flow/logs/mes"},
    {"name": "MYM", "log_dir": "argus_flow/logs/mym"},
]


def check_heartbeat(runner: dict) -> dict:
    log_dir = REPO / runner["log_dir"]
    sig_file = log_dir / "signals.csv"
    state_file = log_dir / "state.json"

    result = {"name": runner["name"], "status": "UNKNOWN", "signal_age_s": None, "state_age_s": None,
              "position": "UNKNOWN", "signal_count": 0}

    if sig_file.exists():
        result["signal_age_s"] = int(time.time() - sig_file.stat().st_mtime)
        try:
            result["signal_count"] = sum(1 for _ in open(sig_file)) - 1
        except Exception:
            pass

    if state_file.exists():
        result["state_age_s"] = int(time.time() - state_file.stat().st_mtime)
        try:
            state = json.loads(state_file.read_text())
            result["position"] = state.get("position", "UNKNOWN")
            result["trade_count"] = state.get("trade_count", 0)
            result["pnl"] = state.get("pnl_pips", state.get("pnl_points", 0))
        except Exception:
            pass

    # Determine status
    sig_age = result["signal_age_s"]
    if sig_age is None:
        result["status"] = "NOT_STARTED"
    elif sig_age < 120:
        result["status"] = "ALIVE"
    elif sig_age < 600:
        result["status"] = "SLOW"
    else:
        result["status"] = "DEAD"

    return result


def main():
    now = datetime.now(timezone.utc)
    print(f"\n{'='*60}")
    print(f"  Heartbeat Monitor — {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"{'='*60}\n")

    results = []
    for runner in RUNNERS:
        r = check_heartbeat(runner)
        results.append(r)

        colors = {"ALIVE": "32", "SLOW": "33", "DEAD": "31", "NOT_STARTED": "90", "UNKNOWN": "90"}
        c = colors.get(r["status"], "0")
        age = f"{r['signal_age_s']}s" if r["signal_age_s"] is not None else "—"

        print(f"  {r['name']:>10s}: \033[{c}m{r['status']:>8s}\033[0m  sig_age={age:>6s}  pos={r['position']:>6s}  signals={r['signal_count']:>5}")

    # Save
    out_path = REPO / "argus_flow" / "logs" / "heartbeat.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "timestamp": now.isoformat(),
        "runners": results,
    }, indent=2, default=str))
    print(f"\n  Saved: {out_path}\n")


if __name__ == "__main__":
    main()