#!/usr/bin/env python3
"""Monitoring-mode snapshot for the current paper/live fleet.

Usage:
    python ops/paper_monitor_status.py
    python ops/paper_monitor_status.py --json

Exit codes:
    0 -> no stale/down systems detected
    1 -> one or more systems are stale/down
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
NOW = time.time()

ARGUS_THRESHOLD_S = 300
LEGACY_THRESHOLDS = {
    "titan": 4500,
    "hermes": 8400,
    "apollo": 16200,
}
HELIO_THRESHOLDS = {
    "helio_swing": 129600,
    "helio_hermes": 129600,
    "helio_apollo": 43200,
}
FORGE_THRESHOLD_S = 5400  # 90 min (signal-only loop is 60min + buffer)
EXPECTED_BLOCK_REASONS = {
    "FRIDAY_CLOSE",
    "MARKET_CLOSED",
    "WEEKEND_CLOSE",
}


@dataclass
class SystemStatus:
    name: str
    status: str
    age_s: int | None
    detail: str
    extra: dict[str, Any]


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _age_seconds(path: Path) -> int | None:
    if not path.exists():
        return None
    return int(max(0, NOW - path.stat().st_mtime))


def _human_age(age_s: int | None) -> str:
    if age_s is None:
        return "missing"
    if age_s < 60:
        return f"{age_s}s"
    if age_s < 3600:
        return f"{age_s // 60}m"
    if age_s < 86400:
        return f"{age_s // 3600}h"
    return f"{age_s // 86400}d"


def _status_from_age(age_s: int | None, threshold_s: int) -> str:
    if age_s is None:
        return "DOWN"
    if age_s > threshold_s:
        return "STALE"
    return "OK"


def _collect_argus() -> list[SystemStatus]:
    out: list[SystemStatus] = []
    for pair in ("audjpy", "usdjpy", "gbpusd", "cadjpy"):
        path = REPO / "argus_flow" / "logs" / pair / "heartbeat.json"
        age_s = _age_seconds(path)
        payload = _load_json(path) or {}
        status = _status_from_age(age_s, ARGUS_THRESHOLD_S)
        blocked = bool(payload.get("entries_blocked"))
        reason = str(payload.get("entry_block_reason", "") or "")
        block_label = "open"
        if blocked:
            block_label = f"blocked:{reason or 'UNKNOWN'}"
            if reason in EXPECTED_BLOCK_REASONS:
                block_label += " (expected)"
        detail = (
            f"stage={payload.get('deployment_stage', '?')} "
            f"mode={payload.get('runtime_mode', '?')} "
            f"pos={payload.get('position', '?')} "
            f"recon={payload.get('reconciliation', '?')} "
            f"{block_label}"
        )
        out.append(SystemStatus(
            name=f"argus:{pair}",
            status=status,
            age_s=age_s,
            detail=detail,
            extra={
                "broker_connected": payload.get("broker_connected"),
                "quarantined": payload.get("quarantined"),
            },
        ))
    return out


def _collect_legacy() -> list[SystemStatus]:
    specs = {
        "titan": REPO / "titan" / "logs" / "heartbeat.json",
        "hermes": REPO / "hermes" / "logs" / "heartbeat.json",
        "apollo": REPO / "apollo" / "logs" / "heartbeat.json",
    }
    out: list[SystemStatus] = []
    for name, path in specs.items():
        age_s = _age_seconds(path)
        payload = _load_json(path) or {}
        status = _status_from_age(age_s, LEGACY_THRESHOLDS[name])
        if name == "titan":
            detail = (
                f"mode={payload.get('mode', '?')} "
                f"open_positions={payload.get('open_positions', '?')} "
                f"ibkr_connected={payload.get('ibkr_connected', '?')}"
            )
        elif name == "hermes":
            detail = (
                f"mode={payload.get('mode', '?')} "
                f"open_positions={payload.get('open_positions', '?')} "
                f"gaps_today={payload.get('gaps_today', '?')} "
                f"ibkr_connected={payload.get('ibkr_connected', '?')}"
            )
        else:
            detail = (
                f"mode={payload.get('mode', '?')} "
                f"watchlist={payload.get('watchlist', '?')} "
                f"actionable={payload.get('actionable', '?')} "
                f"open_positions={payload.get('open_positions', '?')}"
            )
        out.append(SystemStatus(name=f"legacy:{name}", status=status, age_s=age_s, detail=detail, extra={}))
    return out


def _managed_heartbeat_paths(config_glob: str, log_prefix: str = "") -> list[tuple[str, Path]]:
    cfg_dir = REPO / "helio" / "configs"
    out: list[tuple[str, Path]] = []
    for cfg_path in sorted(cfg_dir.glob(config_glob)):
        payload = _load_json(cfg_path) or {}
        symbol = str(payload.get("symbol", cfg_path.stem)).lower()
        out.append((symbol, REPO / "helio" / "logs" / f"{log_prefix}{symbol}" / "heartbeat.json"))
    return out


def _collect_managed_helio() -> list[SystemStatus]:
    # Managed Helio is SUSPENDED per ARCHITECTURE_DECISION.md
    # Legacy Greek (titan/hermes/apollo) is the active execution path.
    # Return empty list — these systems are not running and should not
    # pollute the monitor with stale heartbeats.
    return []


def _collect_forge() -> list[SystemStatus]:
    out: list[SystemStatus] = []

    # GDX/GLD pairs
    gdx_path = REPO / "forge" / "logs" / "gdx_gld" / "heartbeat.json"
    age_s = _age_seconds(gdx_path)
    payload = _load_json(gdx_path) or {}
    status = _status_from_age(age_s, FORGE_THRESHOLD_S)
    z = payload.get("z_score")
    z_str = f"{z:.4f}" if isinstance(z, (int, float)) else "?"
    detail = f"mode={payload.get('mode', '?')} pos={payload.get('position', '?')} z={z_str}"
    out.append(SystemStatus(name="forge:gdx_gld", status=status, age_s=age_s, detail=detail, extra={}))

    # Themis
    themis_path = REPO / "forge" / "logs" / "themis" / "heartbeat.json"
    age_s = _age_seconds(themis_path)
    payload = _load_json(themis_path) or {}
    status = _status_from_age(age_s, 28800)  # 8h threshold
    detail = (
        f"trades={payload.get('total_trades', '?')} "
        f"signals={payload.get('active_signals', '?')} "
        f"new={payload.get('new_signals_this_cycle', '?')}"
    )
    out.append(SystemStatus(name="forge:themis", status=status, age_s=age_s, detail=detail, extra={}))

    # Atlas
    atlas_path = REPO / "forge" / "logs" / "atlas" / "heartbeat.json"
    age_s = _age_seconds(atlas_path)
    payload = _load_json(atlas_path) or {}
    status = _status_from_age(age_s, 600)  # 10 min threshold
    detail = (
        f"regime={payload.get('regime', '?')} "
        f"alert={payload.get('alert_level', '?')} "
        f"events_today={payload.get('events_today', '?')} "
        f"size_mod={payload.get('position_size_modifier', '?')}"
    )
    out.append(SystemStatus(name="forge:atlas", status=status, age_s=age_s, detail=detail, extra={}))

    # Mamba (NAS100/US30 scalping)
    mamba_path = REPO / "forge" / "logs" / "mamba" / "heartbeat.json"
    age_s = _age_seconds(mamba_path)
    payload = _load_json(mamba_path) or {}
    status = _status_from_age(age_s, 600)  # 10 min during NY session
    detail = (
        f"status={payload.get('status', '?')} "
        f"v={payload.get('version', '?')} "
        f"instruments={','.join(payload.get('tickers', []))}"
    )
    out.append(SystemStatus(name="forge:mamba", status=status, age_s=age_s, detail=detail, extra={}))

    # Cue Banks (US30 confluence)
    cb_path = REPO / "forge" / "logs" / "cuebanks" / "heartbeat.json"
    age_s = _age_seconds(cb_path)
    payload = _load_json(cb_path) or {}
    status = _status_from_age(age_s, 600)
    detail = (
        f"status={payload.get('status', '?')} "
        f"bias={payload.get('daily_bias', payload.get('bias', '?'))} "
        f"instrument={payload.get('instrument', 'YM=F')}"
    )
    out.append(SystemStatus(name="forge:cuebanks", status=status, age_s=age_s, detail=detail, extra={}))

    # Tori (4H commodity swing)
    tori_path = REPO / "forge" / "logs" / "tori" / "heartbeat.json"
    age_s = _age_seconds(tori_path)
    payload = _load_json(tori_path) or {}
    status = _status_from_age(age_s, 18000)  # 5h threshold (scans every 4h)
    instruments = payload.get("instruments", [])
    detail = (
        f"status={payload.get('status', '?')} "
        f"instruments={','.join(instruments) if instruments else '?'}"
    )
    out.append(SystemStatus(name="forge:tori", status=status, age_s=age_s, detail=detail, extra={}))

    return out


def _collect_registry() -> dict[str, Any]:
    path = REPO / "argus_flow" / "logs" / "deployment_registry.json"
    payload = _load_json(path) or {}
    summary = payload.get("summary", {})
    return {
        "watcher": summary.get("watcher"),
        "paper": summary.get("paper"),
        "real": summary.get("real"),
        "governance_ready": summary.get("governance_ready"),
        "ready_for_real": summary.get("ready_for_real"),
        "stale_inputs": summary.get("stale_inputs", []),
    }


def _collect_divergence() -> list[str]:
    path = REPO / "argus_flow" / "logs" / "divergence_report.json"
    payload = _load_json(path) or {}
    out: list[str] = []
    for runner in payload.get("runners", []):
        verdict = str(runner.get("verdict", "") or runner.get("status", ""))
        if verdict and verdict != "OK":
            out.append(f"{runner.get('symbol', '?')}={verdict}")
    return out


def _collect_portfolio_guard() -> dict[str, Any]:
    path = REPO / "helio" / "logs" / "portfolio_guard.json"
    payload = _load_json(path) or {}
    metrics = payload.get("metrics", {})
    return {
        "allowed": payload.get("allowed"),
        "reason": payload.get("reason", ""),
        "total_open_positions": metrics.get("total_open_positions"),
        "positions_by_family": metrics.get("positions_by_family", {}),
        "directional_bias": metrics.get("directional_bias"),
    }


def _collect_snapshot() -> dict[str, Any]:
    systems = _collect_argus() + _collect_legacy() + _collect_managed_helio() + _collect_forge()
    failing = [s.name for s in systems if s.status != "OK"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "systems": [asdict(s) for s in systems],
        "registry": _collect_registry(),
        "divergence_flags": _collect_divergence(),
        "portfolio_guard": _collect_portfolio_guard(),
        "failing_systems": failing,
    }


def _print_text(snapshot: dict[str, Any]) -> None:
    print(f"Paper Monitoring Snapshot {snapshot['generated_at']}")
    print()

    sections = (
        ("Managed FX", "argus:"),
        ("Legacy Greek", "legacy:"),
        ("Forge (Paper)", "forge:"),
    )
    systems = snapshot["systems"]
    for title, prefix in sections:
        print(title)
        for item in systems:
            if item["name"].startswith(prefix):
                age_label = _human_age(item["age_s"])
                print(f"  {item['status']:<5} {item['name']:<26} age={age_label:<5} {item['detail']}")
        print()

    registry = snapshot["registry"]
    print(
        "Registry "
        f"watcher={registry.get('watcher')} "
        f"paper={registry.get('paper')} "
        f"real={registry.get('real')} "
        f"ready_for_real={registry.get('ready_for_real')} "
        f"governance_ready={registry.get('governance_ready')}"
    )
    if registry.get("stale_inputs"):
        print(f"  stale_inputs={registry['stale_inputs']}")

    divergence = snapshot["divergence_flags"]
    if divergence:
        print(f"Divergence {', '.join(divergence)}")
    else:
        print("Divergence none")

    guard = snapshot["portfolio_guard"]
    guard_state = "OPEN" if guard.get("allowed") else "BLOCKED"
    print(
        "Portfolio guard "
        f"{guard_state} "
        f"reason={guard.get('reason', '')} "
        f"open_positions={guard.get('total_open_positions')} "
        f"bias={guard.get('directional_bias')}"
    )

    if snapshot["failing_systems"]:
        print()
        print(f"Attention needed: {', '.join(snapshot['failing_systems'])}")
    else:
        print()
        print("All monitored systems are fresh.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitoring-mode fleet snapshot")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = parser.parse_args()

    snapshot = _collect_snapshot()
    if args.json:
        json.dump(snapshot, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        _print_text(snapshot)
    return 1 if snapshot["failing_systems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
