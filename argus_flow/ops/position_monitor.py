"""IBKR Position Monitor — detect state mismatches and dead runners with open positions.

Usage:
    python -m argus_flow.ops.position_monitor
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO = Path(__file__).resolve().parents[2]

RUNNERS = [
    {"name": "EUR/USD", "symbol": "EUR.USD", "log_dir": "argus_flow/logs/eurusd", "type": "forex"},
    {"name": "MNQ", "symbol": "MNQ", "log_dir": "argus_flow/logs/mnq", "type": "future"},
    {"name": "GBP/USD", "symbol": "GBP.USD", "log_dir": "argus_flow/logs/gbpusd", "type": "forex"},
]


def get_runner_state(runner: dict) -> dict:
    state_file = REPO / runner["log_dir"] / "state.json"
    sig_file = REPO / runner["log_dir"] / "signals.csv"

    result = {"name": runner["name"], "runner_position": "UNKNOWN", "runner_alive": False}

    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            result["runner_position"] = state.get("position", "UNKNOWN")
            result["entry_price"] = state.get("entry_price", 0)
            result["trade_count"] = state.get("trade_count", 0)
        except Exception:
            pass

    if sig_file.exists():
        age = time.time() - sig_file.stat().st_mtime
        result["signal_age_s"] = int(age)
        result["runner_alive"] = age < 300  # alive if signal written in last 5 min

    return result


def get_ibkr_positions() -> dict:
    """Connect to IBKR and get actual positions."""
    try:
        from ib_insync import IB
        ib = IB()
        port = int(os.getenv("IBKR_PORT", "4002"))
        ib.connect("127.0.0.1", port, clientId=85, timeout=5)
        positions = ib.positions()
        ib.disconnect()

        pos_map = {}
        for p in positions:
            symbol = p.contract.localSymbol or p.contract.symbol
            qty = float(p.position)
            pos_map[symbol] = {"qty": qty, "avg_cost": float(p.avgCost), "direction": "LONG" if qty > 0 else ("SHORT" if qty < 0 else "FLAT")}
        return pos_map
    except Exception as e:
        return {"_error": str(e)}


def main():
    print("=" * 65)
    print(f"  IBKR Position Monitor — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print("=" * 65)

    ibkr_positions = get_ibkr_positions()
    if "_error" in ibkr_positions:
        print(f"\n  \033[31mIBKR connection failed: {ibkr_positions['_error']}\033[0m")
        print("  Cannot verify positions. Check gateway.")
        ibkr_positions = {}

    results = []
    alerts = []

    for runner in RUNNERS:
        state = get_runner_state(runner)
        ibkr_pos = ibkr_positions.get(runner["symbol"], {"qty": 0, "direction": "FLAT"})

        runner_pos = state["runner_position"]
        ibkr_dir = ibkr_pos.get("direction", "FLAT")
        alive = state.get("runner_alive", False)

        # Mismatch detection
        mismatch = False
        if runner_pos in ("LONG", "SHORT") and ibkr_dir == "FLAT":
            mismatch = True
            alerts.append(f"MISMATCH: {runner['name']} runner={runner_pos} but IBKR=FLAT (phantom position)")
        elif runner_pos == "FLAT" and ibkr_dir in ("LONG", "SHORT"):
            mismatch = True
            alerts.append(f"MISMATCH: {runner['name']} runner=FLAT but IBKR={ibkr_dir} (orphaned position)")

        # Dead runner with position
        if not alive and ibkr_dir in ("LONG", "SHORT"):
            alerts.append(f"CRITICAL: {runner['name']} runner DEAD but IBKR has {ibkr_dir} position!")

        status = "OK" if not mismatch and alive else ("DEAD" if not alive else "MISMATCH")
        color = "32" if status == "OK" else ("31" if "CRITICAL" in str(alerts) else "33")

        print(f"\n  {runner['name']:>10s}: \033[{color}m{status}\033[0m")
        print(f"    Runner: pos={runner_pos} alive={'yes' if alive else 'NO'} sig_age={state.get('signal_age_s', '?')}s")
        print(f"    IBKR:   pos={ibkr_dir} qty={ibkr_pos.get('qty', 0)}")

        results.append({
            "name": runner["name"],
            "runner_position": runner_pos,
            "ibkr_position": ibkr_dir,
            "runner_alive": alive,
            "mismatch": mismatch,
            "status": status,
        })

    if alerts:
        print(f"\n  \033[31mALERTS:\033[0m")
        for a in alerts:
            print(f"    ! {a}")
    else:
        print(f"\n  \033[32mNo alerts. All positions reconciled.\033[0m")

    # Save
    out_path = REPO / "argus_flow" / "logs" / "position_monitor.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "runners": results,
        "alerts": alerts,
        "ibkr_positions": {k: v for k, v in ibkr_positions.items() if k != "_error"},
    }, indent=2, default=str))
    print(f"\n  Saved: {out_path}")


if __name__ == "__main__":
    main()