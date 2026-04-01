#!/usr/bin/env python3
"""
runtime_mode.py  --  Phase 16 Runtime Mode Management

Modes (ordered by restriction level):
  FULL                - normal operation, all entries and exits allowed
  NO_NEW_ENTRY        - hold existing positions, no new entries
  REDUCE_ONLY         - exits allowed, entries blocked
  OBSERVATION_ONLY    - log only, no orders submitted
  RECONCILIATION_ONLY - startup integrity check, no trading

Persistence: crash-safe atomic JSON file (ops/logs/runtime_mode.json)
"""

import json
import os
import time
from typing import Any, Dict, Optional, Tuple


# ---------------------------------------------------------------------------
# Mode definitions
# ---------------------------------------------------------------------------

FULL = "FULL"
NO_NEW_ENTRY = "NO_NEW_ENTRY"
REDUCE_ONLY = "REDUCE_ONLY"
OBSERVATION_ONLY = "OBSERVATION_ONLY"
RECONCILIATION_ONLY = "RECONCILIATION_ONLY"

ALL_MODES = (FULL, NO_NEW_ENTRY, REDUCE_ONLY, OBSERVATION_ONLY, RECONCILIATION_ONLY)

# Restriction ordering: higher = more restricted
_RESTRICTION_LEVEL: Dict[str, int] = {
    FULL: 0,
    NO_NEW_ENTRY: 1,
    REDUCE_ONLY: 2,
    OBSERVATION_ONLY: 3,
    RECONCILIATION_ONLY: 4,
}

# Operation permissions per mode
_MODE_PERMISSIONS: Dict[str, Dict[str, bool]] = {
    FULL:                {"entry": True,  "exit": True,  "observe": True},
    NO_NEW_ENTRY:        {"entry": False, "exit": True,  "observe": True},
    REDUCE_ONLY:         {"entry": False, "exit": True,  "observe": True},
    OBSERVATION_ONLY:    {"entry": False, "exit": False, "observe": True},
    RECONCILIATION_ONLY: {"entry": False, "exit": False, "observe": False},
}


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _default_mode_file() -> str:
    return os.path.join(os.path.dirname(__file__), "ops", "logs", "runtime_mode.json")


def _atomic_write_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    for _ in range(3):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)

    # Windows can transiently deny atomic replacement when scanners briefly hold
    # the destination. Fall back to a direct overwrite so runtime mode still
    # persists instead of failing closed on operator state changes.
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.remove(tmp)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Load / Save
# ---------------------------------------------------------------------------

def load_mode(mode_file: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    """Load current mode from file.

    Returns (mode_string, full_metadata_dict).
    If file is missing or unreadable, defaults to FULL.
    """
    path = mode_file or _default_mode_file()
    if not os.path.exists(path):
        return FULL, {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        mode_str = str(data.get("mode", FULL)).upper()
        if mode_str not in ALL_MODES:
            mode_str = FULL
        return mode_str, data
    except Exception:
        return FULL, {}


def save_mode(
    mode: str,
    *,
    reason: str = "",
    triggered_by: str = "",
    mode_file: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Persist mode to file (crash-safe atomic write)."""
    if mode not in ALL_MODES:
        raise ValueError(f"Invalid runtime mode: {mode!r}. Must be one of {ALL_MODES}")
    path = mode_file or _default_mode_file()
    data: Dict[str, Any] = {
        "mode": mode,
        "reason": reason,
        "triggered_by": triggered_by,
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if extra:
        data["extra"] = extra
    _atomic_write_json(path, data)


# ---------------------------------------------------------------------------
# Permission queries
# ---------------------------------------------------------------------------

def can_entry(mode: Optional[str] = None, mode_file: Optional[str] = None) -> bool:
    if mode is None:
        mode, _ = load_mode(mode_file)
    return _MODE_PERMISSIONS.get(mode, {}).get("entry", False)


def can_exit(mode: Optional[str] = None, mode_file: Optional[str] = None) -> bool:
    if mode is None:
        mode, _ = load_mode(mode_file)
    return _MODE_PERMISSIONS.get(mode, {}).get("exit", False)


def can_observe(mode: Optional[str] = None, mode_file: Optional[str] = None) -> bool:
    if mode is None:
        mode, _ = load_mode(mode_file)
    return _MODE_PERMISSIONS.get(mode, {}).get("observe", True)


def restriction_level(mode: str) -> int:
    """Return integer restriction level (higher = more restricted)."""
    return _RESTRICTION_LEVEL.get(mode, 0)


def is_more_restrictive(a: str, b: str) -> bool:
    """Return True if mode *a* is strictly more restrictive than mode *b*."""
    return restriction_level(a) > restriction_level(b)


# ---------------------------------------------------------------------------
# Mode transitions
# ---------------------------------------------------------------------------

def escalate(
    target: str,
    *,
    reason: str = "",
    triggered_by: str = "",
    mode_file: Optional[str] = None,
) -> Tuple[bool, str]:
    """Escalate to a more restrictive mode only (never relax).

    Returns (changed, current_mode_after).
    """
    current, _ = load_mode(mode_file)
    if is_more_restrictive(target, current):
        save_mode(target, reason=reason, triggered_by=triggered_by, mode_file=mode_file)
        return True, target
    return False, current


def reset_to_full(
    *,
    reason: str = "operator_reset",
    triggered_by: str = "manual",
    mode_file: Optional[str] = None,
) -> None:
    """Explicit operator reset back to FULL mode.

    This is the only way to *relax* mode -- it must be deliberate.
    """
    save_mode(FULL, reason=reason, triggered_by=triggered_by, mode_file=mode_file)


# ---------------------------------------------------------------------------
# Action gating  (called from runner_live order-submission path)
# ---------------------------------------------------------------------------

def gate_action(action: str, mode: str) -> Tuple[bool, str]:
    """Check whether *action* (BUY / SELL / HOLD) is allowed under *mode*.

    Returns (allowed, block_reason).
    block_reason is empty string when allowed.
    """
    action_upper = str(action).upper()
    if action_upper == "BUY":
        if can_entry(mode):
            return True, ""
        return False, f"entry_blocked_by_runtime_mode_{mode}"
    elif action_upper == "SELL":
        if can_exit(mode):
            return True, ""
        return False, f"exit_blocked_by_runtime_mode_{mode}"
    # HOLD and everything else is always permitted
    return True, ""


# ---------------------------------------------------------------------------
# Mode transition history (append-only log)
# ---------------------------------------------------------------------------

def _mode_history_path(mode_file: Optional[str] = None) -> str:
    base = mode_file or _default_mode_file()
    return base.replace(".json", "_history.jsonl")


def _append_history(
    prev_mode: str,
    new_mode: str,
    reason: str,
    triggered_by: str,
    mode_file: Optional[str] = None,
) -> None:
    """Append a mode-transition record to the history log (JSONL)."""
    path = _mode_history_path(mode_file)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    record = {
        "ts": time.time(),
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "prev_mode": prev_mode,
        "new_mode": new_mode,
        "reason": reason,
        "triggered_by": triggered_by,
    }
    try:
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass  # history is best-effort


def transition(
    target: str,
    *,
    reason: str = "",
    triggered_by: str = "",
    mode_file: Optional[str] = None,
    force: bool = False,
) -> Tuple[bool, str, str]:
    """Transition to *target* mode with full audit trail.

    Unless force=True, only escalations (more restrictive) are allowed.
    Returns (changed, prev_mode, current_mode).
    """
    prev, _ = load_mode(mode_file)
    if prev == target:
        return False, prev, prev

    if not force and not is_more_restrictive(target, prev):
        return False, prev, prev

    save_mode(target, reason=reason, triggered_by=triggered_by, mode_file=mode_file)
    _append_history(prev, target, reason, triggered_by, mode_file)
    return True, prev, target
