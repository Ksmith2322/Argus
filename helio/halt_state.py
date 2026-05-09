"""Canonical halt-state reader for execution and dashboard layers.

This module is intentionally read-only. It reconciles the filesystem halt flag
with broker-drift state and returns a fail-closed fleet halt decision whenever
either source says entries should stop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
HALT_FLAG_PATH = REPO / "argus_flow" / "logs" / "HALT.flag"
FLATTEN_FLAG_PATH = REPO / "argus_flow" / "logs" / "FLATTEN_EOD.flag"
BROKER_DRIFT_STATE_PATH = REPO / "argus_flow" / "logs" / "_risk" / "broker_drift_state.json"


@dataclass(frozen=True)
class HaltState:
    halted: bool
    reason: str
    sources: tuple[str, ...]
    halt_flag_present: bool
    flatten_flag_present: bool
    broker_drift_tripped: bool
    broker_drift_state_path: str
    broker_drift_state_ts: str
    checked_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return "(unable to read flag file)"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def get_halt_state(
    *,
    halt_flag_path: Path = HALT_FLAG_PATH,
    flatten_flag_path: Path = FLATTEN_FLAG_PATH,
    broker_drift_state_path: Path = BROKER_DRIFT_STATE_PATH,
) -> HaltState:
    """Return the reconciled fleet halt decision.

    Runtime entry checks should treat ``halted=True`` as a hard stop for new
    orders. Broker drift is included here because the dashboard already reads
    it; this prevents a split where the dashboard says drift is tripped while
    executors only look at HALT.flag.
    """
    checked_at = datetime.now(timezone.utc).isoformat()
    reasons: list[str] = []
    sources: list[str] = []

    halt_flag_present = halt_flag_path.exists()
    if halt_flag_present:
        sources.append("HALT.flag")
        reasons.append(_read_text(halt_flag_path) or "HALT.flag present")

    flatten_flag_present = flatten_flag_path.exists()
    if flatten_flag_present:
        sources.append("FLATTEN_EOD.flag")
        reasons.append(f"flatten active: {_read_text(flatten_flag_path) or 'FLATTEN_EOD.flag present'}")

    drift = _read_json(broker_drift_state_path)
    broker_drift_tripped = bool(drift.get("tripped"))
    if broker_drift_tripped:
        sources.append("broker_drift_state.json")
        div = drift.get("divergence_pct", "unknown")
        sustained = drift.get("sustained_minutes", "unknown")
        reasons.append(f"broker drift tripped: divergence={div}% sustained={sustained}m")

    return HaltState(
        halted=bool(sources),
        reason="; ".join(reasons),
        sources=tuple(sources),
        halt_flag_present=halt_flag_present,
        flatten_flag_present=flatten_flag_present,
        broker_drift_tripped=broker_drift_tripped,
        broker_drift_state_path=str(broker_drift_state_path),
        broker_drift_state_ts=str(drift.get("ts", "")),
        checked_at=checked_at,
    )


def is_fleet_halted() -> tuple[bool, str]:
    state = get_halt_state()
    return state.halted, state.reason
