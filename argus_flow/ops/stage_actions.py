"""Stage Actions — execute stage transitions and record history.

Provides both automatic execution (called after deployment_pipeline) and
manual actions (called from dashboard API endpoints).

Transition History is persisted to stage_history.jsonl (append-only log)
for audit trail and dashboard timeline display.

Usage:
    # Auto-apply pending transitions from registry
    python -m argus_flow.ops.stage_actions --auto-apply

    # Manual action
    python -m argus_flow.ops.stage_actions --action promote --symbol EURUSD
    python -m argus_flow.ops.stage_actions --action demote --symbol GBPJPY
    python -m argus_flow.ops.stage_actions --action kill --symbol AUDUSD
    python -m argus_flow.ops.stage_actions --action pause --symbol EURJPY
    python -m argus_flow.ops.stage_actions --action unpause --symbol EURJPY
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LOGS_DIR = REPO / "argus_flow" / "logs"
HISTORY_FILE = LOGS_DIR / "stage_history.jsonl"
DEPLOYMENT_REGISTRY_FILE = LOGS_DIR / "deployment_registry.json"
CONFIGS_DIR = REPO / "argus_flow" / "configs"

from argus_flow.ops.fleet_registry import (
    STAGE_KILLED,
    STAGE_PAPER,
    STAGE_QUARANTINE,
    STAGE_REAL,
    STAGE_WATCHER,
    normalize_stage,
)

# Valid manual action transitions
VALID_TRANSITIONS = {
    "promote": {
        STAGE_WATCHER: STAGE_PAPER,
        STAGE_PAPER: STAGE_REAL,
    },
    "demote": {
        STAGE_REAL: STAGE_PAPER,
        STAGE_PAPER: STAGE_WATCHER,
        STAGE_QUARANTINE: STAGE_PAPER,
    },
    "quarantine": {
        STAGE_REAL: STAGE_QUARANTINE,
    },
    "kill": {
        STAGE_WATCHER: STAGE_KILLED,
        STAGE_PAPER: STAGE_KILLED,
        STAGE_REAL: STAGE_KILLED,
        STAGE_QUARANTINE: STAGE_KILLED,
    },
    "revive": {
        STAGE_KILLED: STAGE_WATCHER,
    },
}


def _load_registry() -> dict | None:
    if not DEPLOYMENT_REGISTRY_FILE.exists():
        return None
    try:
        return json.loads(DEPLOYMENT_REGISTRY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _find_runner(registry: dict, symbol: str) -> dict | None:
    symbol = symbol.upper()
    for runner in registry.get("runners", []):
        if runner.get("symbol", "").upper() == symbol:
            return runner
    return None


def record_transition(
    symbol: str,
    from_stage: str,
    to_stage: str,
    trigger: str,
    reason: str = "",
    operator: str = "system",
) -> dict:
    """Append a transition event to the history log."""
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol.upper(),
        "from_stage": from_stage,
        "to_stage": to_stage,
        "trigger": trigger,  # "auto", "manual", "demotion", "kill_discipline"
        "reason": reason,
        "operator": operator,
    }
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")
    return event


def load_history(symbol: str | None = None, limit: int = 100) -> list[dict]:
    """Load recent transition history, optionally filtered by symbol."""
    if not HISTORY_FILE.exists():
        return []
    events = []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    if symbol and event.get("symbol", "").upper() != symbol.upper():
                        continue
                    events.append(event)
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return events[-limit:]


def _update_config_stage(config_path: str, new_stage: str) -> bool:
    """Update a config file's deployment.stage field."""
    full_path = REPO / config_path
    if not full_path.exists():
        return False
    try:
        cfg = json.loads(full_path.read_text(encoding="utf-8"))
        if "deployment" not in cfg:
            cfg["deployment"] = {}
        cfg["deployment"]["stage"] = new_stage
        cfg["deployment"]["stage_changed_at"] = datetime.now(timezone.utc).isoformat()
        full_path.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
        return True
    except Exception:
        return False


def auto_apply_transitions() -> list[dict]:
    """Apply all pending transitions from the deployment registry.

    Called after deployment_pipeline.build_registry() to execute transitions
    that the pipeline identified as ready.
    """
    registry = _load_registry()
    if not registry:
        return []

    applied = []
    for runner in registry.get("runners", []):
        if not runner.get("transition_ready", False):
            continue

        symbol = runner["symbol"]
        from_stage = runner.get("previous_stage", runner.get("current_stage", ""))
        to_stage = runner.get("current_stage", "")  # pipeline already set current_stage
        reason = runner.get("transition_reason", "")
        config_path = runner.get("config_path", "")

        # Update config file stage
        if config_path:
            _update_config_stage(config_path, to_stage)

        # Record in history
        event = record_transition(
            symbol=symbol,
            from_stage=from_stage,
            to_stage=to_stage,
            trigger="auto",
            reason=reason,
        )
        applied.append(event)
        print(f"  AUTO: {symbol} {from_stage} → {to_stage} ({reason})")

    return applied


def execute_manual_action(
    action: str,
    symbol: str,
    reason: str = "",
    operator: str = "dashboard",
) -> dict:
    """Execute a manual stage transition action.

    Returns dict with {success, symbol, from_stage, to_stage, reason, error}.
    """
    action = action.lower()
    symbol = symbol.upper()
    result = {"success": False, "symbol": symbol, "action": action}

    # Handle pause/unpause separately (file-based, not stage transition)
    if action == "pause":
        pause_file = REPO / "PAUSE_ENTRIES"
        pause_file.write_text(f"manual_pause_{symbol}_{datetime.now(timezone.utc).isoformat()}")
        record_transition(symbol, "active", "paused", "manual", reason or "manual pause", operator)
        return {"success": True, "symbol": symbol, "action": "pause", "message": "Entries paused fleet-wide"}

    if action == "unpause":
        pause_file = REPO / "PAUSE_ENTRIES"
        if pause_file.exists():
            pause_file.unlink()
        record_transition(symbol, "paused", "active", "manual", reason or "manual unpause", operator)
        return {"success": True, "symbol": symbol, "action": "unpause", "message": "Entries resumed"}

    # Stage transitions
    if action not in VALID_TRANSITIONS:
        result["error"] = f"Unknown action: {action}. Valid: {list(VALID_TRANSITIONS.keys())}"
        return result

    registry = _load_registry()
    if not registry:
        result["error"] = "Deployment registry not found"
        return result

    runner = _find_runner(registry, symbol)
    if not runner:
        result["error"] = f"Symbol {symbol} not found in registry"
        return result

    current_stage = runner.get("current_stage", "")
    transitions = VALID_TRANSITIONS[action]

    if current_stage not in transitions:
        result["error"] = f"Cannot {action} from stage '{current_stage}'. Valid from: {list(transitions.keys())}"
        return result

    to_stage = transitions[current_stage]
    config_path = runner.get("config_path", "")

    # Apply to config
    if config_path:
        ok = _update_config_stage(config_path, to_stage)
        if not ok:
            result["error"] = f"Failed to update config at {config_path}"
            return result

    # Record history
    event = record_transition(
        symbol=symbol,
        from_stage=current_stage,
        to_stage=to_stage,
        trigger="manual",
        reason=reason or f"manual {action} via {operator}",
        operator=operator,
    )

    # Trigger registry rebuild so dashboard picks up change immediately
    try:
        from argus_flow.ops.deployment_pipeline import build_registry
        build_registry(auto_materialize_live=(action == "promote" and to_stage == STAGE_REAL))
    except Exception as e:
        print(f"  Warning: registry rebuild failed: {e}")

    result["success"] = True
    result["from_stage"] = current_stage
    result["to_stage"] = to_stage
    result["message"] = f"{symbol}: {current_stage} → {to_stage}"
    return result


def main():
    parser = argparse.ArgumentParser(description="Stage transition actions")
    parser.add_argument("--auto-apply", action="store_true",
                        help="Apply all pending automatic transitions")
    parser.add_argument("--action", type=str, choices=["promote", "demote", "quarantine", "kill", "pause", "unpause"])
    parser.add_argument("--symbol", type=str)
    parser.add_argument("--reason", type=str, default="")
    parser.add_argument("--history", action="store_true", help="Show transition history")
    args = parser.parse_args()

    if args.history:
        events = load_history(args.symbol)
        print(f"Stage History ({len(events)} events):")
        for e in events:
            print(f"  {e['ts'][:19]} | {e['symbol']:>8s} | {e['from_stage']} → {e['to_stage']} | {e['trigger']} | {e.get('reason', '')[:60]}")
        return

    if args.auto_apply:
        print("Auto-applying pending transitions...")
        applied = auto_apply_transitions()
        print(f"Applied {len(applied)} transition(s).")
        return

    if args.action and args.symbol:
        result = execute_manual_action(args.action, args.symbol, args.reason)
        if result["success"]:
            print(f"OK: {result['message']}")
        else:
            print(f"FAILED: {result.get('error', 'unknown')}")
            sys.exit(1)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
