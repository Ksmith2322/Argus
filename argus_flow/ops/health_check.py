"""Fleet health check — quick status of all runners.

Usage:
    python -m argus_flow.ops.health_check
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


RUNNERS = [
    {"name": "EUR/USD", "log_dir": "argus_flow/logs/eurusd", "symbol": "EURUSD"},
    {"name": "MNQ", "log_dir": "argus_flow/logs/mnq", "symbol": "MNQ"},
    {"name": "GBP/USD", "log_dir": "argus_flow/logs/gbpusd", "symbol": "GBPUSD"},
]


def check_runner(runner: dict) -> dict:
    log_dir = Path(runner["log_dir"])
    state_file = log_dir / "state.json"
    signal_file = log_dir / "signals.csv"
    trade_file = log_dir / "trades.csv"

    result = {
        "name": runner["name"],
        "symbol": runner["symbol"],
        "status": "UNKNOWN",
        "issues": [],
    }

    # Check state file
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            result["position"] = state.get("position", "?")
            result["trade_count"] = state.get("trade_count", 0)
            result["pnl"] = state.get("pnl_pips", state.get("pnl_points", 0))

            # Check freshness
            mtime = datetime.fromtimestamp(state_file.stat().st_mtime, tz=timezone.utc)
            age_s = (datetime.now(timezone.utc) - mtime).total_seconds()
            result["state_age_s"] = int(age_s)

            if age_s > 300:
                result["issues"].append(f"State file stale ({age_s:.0f}s old)")
        except Exception as e:
            result["issues"].append(f"State file error: {e}")
    else:
        result["issues"].append("No state file (not started?)")

    # Check signals
    if signal_file.exists():
        try:
            line_count = sum(1 for _ in open(signal_file)) - 1
            result["signal_count"] = line_count

            mtime = datetime.fromtimestamp(signal_file.stat().st_mtime, tz=timezone.utc)
            age_s = (datetime.now(timezone.utc) - mtime).total_seconds()
            result["signals_age_s"] = int(age_s)
        except Exception as e:
            result["issues"].append(f"Signal file error: {e}")
    else:
        result["signal_count"] = 0

    # Check trades
    if trade_file.exists():
        try:
            line_count = sum(1 for _ in open(trade_file)) - 1
            result["closed_trades"] = line_count
        except Exception:
            result["closed_trades"] = 0
    else:
        result["closed_trades"] = 0

    # Determine status
    if result["issues"]:
        result["status"] = "WARNING" if len(result["issues"]) == 1 else "ERROR"
    elif result.get("state_age_s", 999) < 120:
        result["status"] = "RUNNING"
    elif result.get("signal_count", 0) > 0:
        result["status"] = "IDLE"
    else:
        result["status"] = "NOT STARTED"

    return result


def main():
    now = datetime.now(timezone.utc)
    print(f"\n{'='*65}")
    print(f"  IBKR Fleet Health Check — {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"{'='*65}\n")

    colors = {"RUNNING": "32", "IDLE": "33", "WARNING": "33", "ERROR": "31", "NOT STARTED": "90", "UNKNOWN": "90"}

    for runner_cfg in RUNNERS:
        r = check_runner(runner_cfg)
        color = colors.get(r["status"], "0")
        status_str = f"\033[{color}m{r['status']}\033[0m"

        print(f"  {r['name']:>10s} | {status_str:>20s} | pos={r.get('position', '?'):>5s} | "
              f"signals={r.get('signal_count', 0):>5} | trades={r.get('closed_trades', 0):>3} | "
              f"pnl={r.get('pnl', 0):>+8.1f}")

        if r["issues"]:
            for issue in r["issues"]:
                print(f"             \033[31m! {issue}\033[0m")

    # IBKR gateway check
    print(f"\n  {'Gateway':>10s} | ", end="")
    try:
        from ib_insync import IB
        ib = IB()
        ib.connect("127.0.0.1", int(os.getenv("IBKR_PORT", "4002")), clientId=99, timeout=3)
        accounts = ib.managedAccounts()
        print(f"\033[32mCONNECTED\033[0m | account={accounts[0]}")
        ib.disconnect()
    except Exception as e:
        print(f"\033[31mDOWN\033[0m | {e}")

    print()


if __name__ == "__main__":
    main()