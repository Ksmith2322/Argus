"""IBKR Position Monitor — reconcile runner state vs broker truth.

Detects: dead runners with open positions, state mismatches, stale heartbeats.
Exits nonzero on CRITICAL conditions for scheduler/alerting integration.

Usage:
    python -m argus_flow.ops.position_monitor
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from argus_flow.ops.broker_truth import file_age_s, load_fleet_snapshot, load_runner_broker_state
from argus_flow.ops.fleet_registry import discover_managed_runners

load_dotenv()

REPO = Path(__file__).resolve().parents[2]

# Active cohort — must match runner_unified.py and dashboard.py
COHORT_RUNNERS = [
    {"name": "GBP/USD", "ib_canonical": "GBP.USD", "log_dir": "argus_flow/logs/gbpusd", "type": "forex"},
    {"name": "EUR/USD", "ib_canonical": "EUR.USD", "log_dir": "argus_flow/logs/eurusd", "type": "forex"},
    {"name": "EUR/JPY", "ib_canonical": "EUR.JPY", "log_dir": "argus_flow/logs/eurjpy", "type": "forex"},
]

HEARTBEAT_STALE_S = 600  # 10 min (heartbeat writes every 5 min, so 2x buffer)


def _managed_runners() -> list[dict]:
    dynamic = []
    for runner in discover_managed_runners():
        if not runner.get("launch_enabled", True):
            continue
        dynamic.append(
            {
                "name": runner["name"],
                "ib_canonical": f"{runner['symbol'][:3]}.{runner['symbol'][3:]}" if runner["instrument_type"] == "forex" and len(runner["symbol"]) == 6 else runner["symbol"],
                "log_dir": runner["log_dir"],
                "type": runner["instrument_type"],
            }
        )
    return dynamic or COHORT_RUNNERS


def _normalize_ib_symbol(contract) -> str:
    """Normalize IBKR contract to canonical instrument key.

    FX: base.quote (e.g. EUR.USD)
    Futures: symbol (root only, ignore expiry for matching)
    """
    sec_type = getattr(contract, "secType", "")
    if sec_type == "CASH":
        # FX pair
        return f"{contract.symbol}.{contract.currency}"
    elif sec_type == "FUT":
        return contract.symbol
    else:
        return contract.localSymbol or contract.symbol


def get_runner_state(runner: dict) -> dict:
    """Read runner state from heartbeat file (primary) and state.json (secondary)."""
    log_dir = REPO / runner["log_dir"]
    hb_file = log_dir / "heartbeat.json"
    state_file = log_dir / "state.json"
    broker_state_file = log_dir / "broker_state.json"

    result = {
        "name": runner["name"],
        "runner_position": "UNKNOWN",
        "alive": False,
        "heartbeat_age_s": 9999,
        "state_read_error": False,
    }

    # Primary liveness: heartbeat file
    if hb_file.exists():
        try:
            hb = json.loads(hb_file.read_text())
            age = time.time() - hb_file.stat().st_mtime
            result["heartbeat_age_s"] = int(age)
            result["alive"] = age < HEARTBEAT_STALE_S
            result["quarantined"] = hb.get("quarantined", False)
            result["consecutive_errors"] = hb.get("consecutive_errors", 0)
        except Exception:
            result["state_read_error"] = True

    # State file for position truth
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
            result["runner_position"] = state.get("position", "UNKNOWN")
            result["entry_price"] = state.get("entry_price", 0)
            result["trade_count"] = state.get("trade_count", 0)
        except (json.JSONDecodeError, KeyError) as e:
            result["state_read_error"] = True
            result["state_error_detail"] = str(e)
    elif not hb_file.exists():
        # Neither heartbeat nor state — runner never started
        result["runner_position"] = "NEVER_STARTED"

    broker_state = load_runner_broker_state(log_dir)
    if broker_state:
        result["broker_state_age_s"] = file_age_s(broker_state_file)
        result["broker_reconciliation"] = broker_state.get("reconciliation", {}).get("result", "")
        result["broker_reconciliation_detail"] = broker_state.get("reconciliation", {}).get("detail", "")
        result["broker_artifact_position"] = broker_state.get("broker", {}).get("position", "UNKNOWN")
        result["broker_artifact_qty"] = broker_state.get("broker", {}).get("qty", 0.0)
        result["broker_truth_source"] = broker_state.get("source", "runner_unified")

    return result


def get_ibkr_positions() -> tuple[dict, bool, str, dict | None]:
    """Get broker truth, preferring the live runner snapshot when it is fresh."""
    snapshot = load_fleet_snapshot(max_age_s=HEARTBEAT_STALE_S)
    if snapshot and snapshot.get("broker_connected"):
        positions = snapshot.get("positions", {})
        if isinstance(positions, dict):
            return positions, True, "runner_snapshot", snapshot

    # Fallback: connect directly to IBKR if the runner snapshot is unavailable.
    try:
        from ib_insync import IB
        ib = IB()
        port = int(os.getenv("IBKR_PORT", "7496"))
        ib.connect("127.0.0.1", port, clientId=85, timeout=5)
        positions = ib.positions()
        ib.disconnect()

        pos_map = {}
        for p in positions:
            key = _normalize_ib_symbol(p.contract)
            qty = float(p.position)
            direction = "LONG" if qty > 0 else ("SHORT" if qty < 0 else "FLAT")
            pos_map[key] = {"qty": qty, "avg_cost": float(p.avgCost), "direction": direction}
        return pos_map, True, "direct_ibkr", None
    except Exception as e:
        return {"_error": str(e)}, False, "direct_ibkr_failed", snapshot


def _classify_severity(alive: bool, mismatch: bool, ibkr_dir: str, state_error: bool) -> str:
    """Explicit severity ladder."""
    if not alive and ibkr_dir in ("LONG", "SHORT"):
        return "CRITICAL"  # dead runner + broker non-flat
    if state_error:
        return "ERROR"  # corrupt/unreadable state
    if mismatch:
        return "MISMATCH"
    if not alive:
        return "STALE"  # dead but broker flat
    return "OK"


SEVERITY_COLOR = {
    "OK": "32",       # green
    "STALE": "33",    # yellow
    "MISMATCH": "31", # red
    "ERROR": "35",    # magenta
    "CRITICAL": "31", # red bold
}


def main():
    print("=" * 65)
    print(f"  IBKR Position Monitor -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print("=" * 65)

    ibkr_positions, ibkr_ok, position_source, fleet_snapshot = get_ibkr_positions()
    snapshot_age = file_age_s(REPO / "argus_flow" / "logs" / "_broker" / "broker_snapshot.json")
    if not ibkr_ok:
        print(f"\n  \033[31mIBKR connection failed: {ibkr_positions['_error']}\033[0m")
        print("  Cannot verify positions. Check TWS.")
        ibkr_positions = {}
    else:
        source_suffix = f" via {position_source}"
        if position_source == "runner_snapshot" and snapshot_age is not None:
            source_suffix += f" ({snapshot_age}s old)"
        print(f"\n  Broker truth source: {source_suffix}")

    results = []
    alerts = []
    has_critical = False

    for runner in _managed_runners():
        state = get_runner_state(runner)
        ibkr_pos = ibkr_positions.get(runner["ib_canonical"], {"qty": 0, "direction": "FLAT"})

        runner_pos = state["runner_position"]
        ibkr_dir = ibkr_pos.get("direction", "FLAT")
        alive = state.get("alive", False)
        state_error = state.get("state_read_error", False)

        # Mismatch detection
        mismatch = False
        if runner_pos in ("LONG", "SHORT") and ibkr_dir == "FLAT":
            mismatch = True
            alerts.append(f"MISMATCH: {runner['name']} runner={runner_pos} but IBKR=FLAT (phantom position)")
        elif runner_pos == "FLAT" and ibkr_dir in ("LONG", "SHORT"):
            mismatch = True
            alerts.append(f"MISMATCH: {runner['name']} runner=FLAT but IBKR={ibkr_dir} (orphaned position)")

        severity = _classify_severity(alive, mismatch, ibkr_dir, state_error)
        if severity == "CRITICAL":
            has_critical = True
            alerts.append(f"CRITICAL: {runner['name']} runner DEAD but IBKR has {ibkr_dir} position!")
        if state_error:
            alerts.append(f"ERROR: {runner['name']} state file read error: {state.get('state_error_detail', 'unknown')}")

        color = SEVERITY_COLOR.get(severity, "37")
        print(f"\n  {runner['name']:>10s}: \033[{color}m{severity}\033[0m")
        print(f"    Runner: pos={runner_pos} alive={'yes' if alive else 'NO'} hb_age={state.get('heartbeat_age_s', '?')}s")
        print(f"    IBKR:   pos={ibkr_dir} qty={ibkr_pos.get('qty', 0)}")
        if state.get("quarantined"):
            print(f"    \033[31mQUARANTINED (errors={state.get('consecutive_errors', '?')})\033[0m")

        results.append({
            "name": runner["name"],
            "runner_position": runner_pos,
            "ibkr_position": ibkr_dir,
            "alive": alive,
            "mismatch": mismatch,
            "severity": severity,
            "state_read_error": state_error,
            "heartbeat_age_s": state.get("heartbeat_age_s", 9999),
            "broker_state_age_s": state.get("broker_state_age_s"),
            "broker_reconciliation": state.get("broker_reconciliation", ""),
        })

    # Check for orphaned IBKR positions not tracked by any runner.
    # Scope: argus is the FX-runner family. Equity (UVXY/GLD/SPY/...) and
    # futures positions belong to forge runners which manage their own state
    # in-process — they are out of scope for this monitor and would otherwise
    # produce false-positive ORPHAN alerts. FX symbols use base.quote canonical
    # form (e.g. "GBP.USD"); equity/futures symbols are bare (e.g. "UVXY").
    tracked_symbols = {r["ib_canonical"] for r in _managed_runners()}
    for sym, pos in ibkr_positions.items():
        if pos.get("direction") == "FLAT":
            continue
        is_fx_canonical = "." in sym
        if not is_fx_canonical:
            # forge-owned (equity/future). Log INFO so it shows up in the report
            # but does not trigger CRITICAL on the argus position monitor.
            results.append({
                "name": f"forge_position:{sym}",
                "runner_position": "(forge-owned)",
                "ibkr_position": pos["direction"],
                "alive": True,
                "mismatch": False,
                "severity": "INFO",
                "state_read_error": False,
                "heartbeat_age_s": 0,
                "broker_state_age_s": None,
                "broker_reconciliation": "out_of_scope_for_argus_monitor",
            })
            continue
        if sym not in tracked_symbols:
            alerts.append(f"ORPHAN: IBKR has {pos['direction']} in {sym} — not tracked by any argus FX runner!")
            has_critical = True

    if alerts:
        print(f"\n  \033[31mALERTS:\033[0m")
        for a in alerts:
            print(f"    ! {a}")
    else:
        print(f"\n  \033[32mNo alerts. All positions reconciled.\033[0m")

    # Save
    out_path = REPO / "argus_flow" / "logs" / "position_monitor.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    overall_status = "CRITICAL" if has_critical else ("WARN" if alerts else "OK")
    out_path.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": overall_status,
        "ibkr_connected": ibkr_ok,
        "position_source": position_source,
        "runners": results,
        "alerts": alerts,
        "has_critical": has_critical,
        "ibkr_positions": {k: v for k, v in ibkr_positions.items() if k != "_error"},
        "account": (fleet_snapshot or {}).get("account", {}),
    }, indent=2, default=str))
    print(f"\n  Saved: {out_path}")

    # Exit nonzero on critical for scheduler integration
    if has_critical:
        sys.exit(1)


if __name__ == "__main__":
    main()
