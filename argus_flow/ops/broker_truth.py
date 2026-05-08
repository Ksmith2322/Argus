"""Shared helpers for canonical broker/account truth artifacts."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
LOGS = REPO / "argus_flow" / "logs"
BROKER_DIR = LOGS / "_broker"
FLEET_SNAPSHOT_FILE = BROKER_DIR / "broker_snapshot.json"
PROCESS_SNAPSHOT_GLOB = "broker_snapshot_*.json"
DEFAULT_FRESH_S = 600


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically on the same filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    for _ in range(3):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            time.sleep(0.05)

    # Fall back to a direct overwrite if Windows briefly denies replacement.
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass


def read_json(path: Path) -> dict | list | None:
    """Read JSON, returning None on missing/corrupt files."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def file_age_s(path: Path) -> int | None:
    """Return file age in whole seconds, or None if unavailable."""
    if not path.exists():
        return None
    try:
        return int(max(0.0, time.time() - path.stat().st_mtime))
    except OSError:
        return None


def is_fresh(path: Path, max_age_s: int = DEFAULT_FRESH_S) -> bool:
    """Return True if the file exists and is younger than the freshness window."""
    age = file_age_s(path)
    return age is not None and age <= max_age_s


def fleet_snapshot_path() -> Path:
    return FLEET_SNAPSHOT_FILE


def process_snapshot_path(source_pid: int | str) -> Path:
    return BROKER_DIR / f"broker_snapshot_{source_pid}.json"


def load_process_snapshots(max_age_s: int | None = None) -> list[dict]:
    """Load fresh per-process broker snapshots."""
    snapshots: list[dict] = []
    for path in sorted(BROKER_DIR.glob(PROCESS_SNAPSHOT_GLOB)):
        if max_age_s is not None and not is_fresh(path, max_age_s):
            continue
        data = read_json(path)
        if not isinstance(data, dict):
            continue
        data["_path"] = str(path)
        data["_age_s"] = file_age_s(path)
        snapshots.append(data)
    return snapshots


def merge_fleet_snapshots(snapshots: list[dict]) -> dict | None:
    """Merge per-process snapshots into one fleet-level broker truth artifact."""
    if not snapshots:
        return None

    snapshots = sorted(snapshots, key=lambda item: item.get("timestamp", ""))
    merged_positions: dict = {}
    merged_orders: list[dict] = []
    merged_reconciliation: dict = {}
    merged_sources: list[dict] = []
    best_account = {}
    best_equity = float("-inf")
    latest_timestamp = snapshots[-1].get("timestamp")

    for snap in snapshots:
        positions = snap.get("positions", {})
        if isinstance(positions, dict):
            merged_positions.update(positions)

        open_orders = snap.get("open_orders", [])
        if isinstance(open_orders, list):
            merged_orders.extend(open_orders)

        reconciliation = snap.get("reconciliation", {})
        if isinstance(reconciliation, dict):
            merged_reconciliation.update(reconciliation)

        account = snap.get("account", {})
        if isinstance(account, dict):
            equity = account.get("net_liquidation_usd", float("-inf"))
            try:
                equity = float(equity)
            except (TypeError, ValueError):
                equity = float("-inf")
            # 2026-05-07 audit: reject zero or negative equity snapshots.
            # When TWS resets, runners briefly report account={..., net_liquidation_usd: 0}
            # before re-subscribing. Letting equity=0 win the merge propagated
            # into fleet_sizing as the sizing anchor, which (correctly) refused
            # to size, but masked the underlying TWS-reset symptom.
            if equity > 0 and equity >= best_equity:
                best_equity = equity
                best_account = account

        merged_sources.append({
            "source_pid": snap.get("source_pid"),
            "runtime_mode": snap.get("runtime_mode"),
            "timestamp": snap.get("timestamp"),
            "age_s": snap.get("_age_s"),
            "path": snap.get("_path"),
        })

    return {
        "timestamp": latest_timestamp,
        "source": "merged_runner_snapshots",
        "broker_connected": any(bool(snap.get("broker_connected")) for snap in snapshots),
        "account": best_account,
        # 2026-05-07: visibility flag for the "all snapshots had zero equity"
        # case (TWS reset window). Downstream consumers (risk_oversight,
        # fleet_sizing) can detect and surface this rather than silently
        # serving $0 anchor.
        "equity_unavailable": (best_equity <= 0),
        "positions": merged_positions,
        "open_orders": merged_orders,
        "reconciliation": merged_reconciliation,
        "sources": merged_sources,
    }


def runner_broker_state_path(log_dir: Path) -> Path:
    return log_dir / "broker_state.json"


def load_fleet_snapshot(max_age_s: int | None = None) -> dict | None:
    """Load the fleet broker snapshot, optionally requiring freshness."""
    path = fleet_snapshot_path()
    if path.exists() and (max_age_s is None or is_fresh(path, max_age_s)):
        data = read_json(path)
        if isinstance(data, dict):
            return data

    return merge_fleet_snapshots(load_process_snapshots(max_age_s=max_age_s))


def load_runner_broker_state(log_dir: Path, max_age_s: int | None = None) -> dict | None:
    """Load a per-runner broker_state artifact, optionally requiring freshness."""
    path = runner_broker_state_path(log_dir)
    if max_age_s is not None and not is_fresh(path, max_age_s):
        return None
    data = read_json(path)
    return data if isinstance(data, dict) else None
