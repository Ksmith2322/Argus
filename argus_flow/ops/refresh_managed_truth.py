"""Canonical managed-truth refresh for the staged fleet.

Runs the full governance/report chain used by the dashboard, deployment
pipeline, and alerting surfaces. This is the authoritative refresh path for:
- launch_fleet preflight
- watchdog background refresh
- scheduled task refresh
- nightly summary refresh
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ops.process_lock import ProcessLock, ProcessLockError

REPO = Path(__file__).resolve().parents[2]
LOGS = REPO / "argus_flow" / "logs"
STATUS_PATH = LOGS / "managed_truth_refresh.json"
LOG_PATH = LOGS / "managed_truth_refresh.log"
LOCK_NAME = "managed_truth_refresh"

DEFAULT_ACCEPT_EXISTING_AGE_S = 900
DEFAULT_WALKFORWARD_MAX_AGE_S = 21600

STEPS = [
    {"id": "trade_artifact_schema", "module": "argus_flow.ops.trade_artifact_schema", "critical": True},
    {"id": "daily_report", "module": "argus_flow.ops.daily_report", "critical": True},
    {"id": "divergence_guard", "module": "argus_flow.ops.divergence_guard", "critical": False},
    {"id": "correlation_guard", "module": "argus_flow.ops.correlation_guard", "critical": False},
    {
        "id": "walkforward_validation",
        "module": "argus_flow.ops.walkforward_validation",
        "args": ["--all-active"],
        "critical": False,
        "max_age_s": DEFAULT_WALKFORWARD_MAX_AGE_S,
    },
    {"id": "kill_discipline", "module": "argus_flow.ops.kill_discipline", "critical": False},
    {"id": "promotion_gate_v2", "module": "argus_flow.ops.promotion_gate_v2", "critical": False},
    {"id": "artifact_divergence", "module": "argus_flow.ops.artifact_divergence", "critical": False},
    {"id": "deployment_pipeline", "module": "argus_flow.ops.deployment_pipeline", "critical": True},
    {"id": "stage_actions", "module": "argus_flow.ops.stage_actions", "args": ["--auto-apply"], "critical": False},
    {"id": "evidence_registry", "module": "argus_flow.ops.evidence_registry", "critical": True},
    {"id": "demotion_check", "module": "argus_flow.ops.demotion_check", "critical": False},
    {"id": "position_monitor", "module": "argus_flow.ops.position_monitor", "critical": False},
    {"id": "risk_oversight", "module": "argus_flow.ops.risk_oversight", "critical": False},
    {"id": "qa_learning", "module": "argus_flow.ops.qa_learning", "critical": False},
    {"id": "edge_allocation", "module": "argus_flow.ops.edge_allocation", "critical": False},
    {"id": "alert_escalation_v2", "module": "argus_flow.ops.alert_escalation_v2", "critical": True},
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{_now_iso()}] {message}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _load_status() -> dict | None:
    if not STATUS_PATH.exists():
        return None
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _status_age_s(status: dict | None) -> float | None:
    if not isinstance(status, dict):
        return None
    text = str(status.get("timestamp", "") or "").strip()
    if not text:
        return None
    try:
        ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())


def _write_status(payload: dict) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _previous_step_map(status: dict | None) -> dict[str, dict]:
    if not isinstance(status, dict):
        return {}
    result: dict[str, dict] = {}
    for step in status.get("steps", []):
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("id", "") or "").strip()
        if step_id:
            result[step_id] = step
    return result


def _reuse_step(step: dict, previous: dict, age_s: float) -> dict:
    reused = dict(previous)
    reused["status"] = "SKIPPED_FRESH"
    reused["reused"] = True
    reused["source_status"] = str(previous.get("status", "") or "OK")
    reused["source_age_s"] = round(age_s, 2)
    reused["duration_s"] = 0.0
    reused["stdout_tail"] = [f"reused prior {step['id']} result ({int(age_s)}s old)"]
    reused["stderr_tail"] = []
    return reused


def _run_step(step: dict) -> dict:
    args = [sys.executable, "-m", step["module"], *step.get("args", [])]
    start = time.time()
    result = subprocess.run(
        args,
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    duration_s = round(time.time() - start, 2)
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    status = "OK" if result.returncode == 0 else "FAILED"
    if stdout:
        _log(f"{step['id']}: stdout={stdout.splitlines()[-1][:400]}")
    if stderr:
        _log(f"{step['id']}: stderr={stderr.splitlines()[-1][:400]}")
    if result.returncode != 0:
        _log(f"{step['id']}: FAILED rc={result.returncode}")
    return {
        "id": step["id"],
        "module": step["module"],
        "critical": bool(step.get("critical", False)),
        "returncode": int(result.returncode),
        "status": status,
        "duration_s": duration_s,
        "stdout_tail": stdout.splitlines()[-5:],
        "stderr_tail": stderr.splitlines()[-5:],
    }


def refresh_managed_truth(*, include_summary: bool, accept_existing_age_s: int) -> int:
    lock = ProcessLock(LOCK_NAME)
    try:
        lock.acquire(metadata={"kind": "managed_truth_refresh"})
    except ProcessLockError:
        status = _load_status()
        age_s = _status_age_s(status)
        if (
            isinstance(status, dict)
            and status.get("status") == "OK"
            and age_s is not None
            and age_s <= accept_existing_age_s
        ):
            _log(
                f"refresh lock already held; reusing existing OK refresh "
                f"({int(age_s)}s old)"
            )
            return 0
        _log("refresh lock already held and existing status is stale or failed")
        return 2

    steps: list[dict] = []
    critical_failure = False
    failed_critical: list[str] = []
    failed_noncritical: list[str] = []
    started_at = _now_iso()
    previous_status = _load_status()
    previous_age_s = _status_age_s(previous_status)
    previous_step_map = _previous_step_map(previous_status)

    try:
        _log("managed truth refresh started")
        for step in STEPS:
            previous_step = previous_step_map.get(step["id"])
            if (
                step.get("max_age_s")
                and isinstance(previous_status, dict)
                and previous_status.get("status") == "OK"
                and previous_age_s is not None
                and previous_age_s <= float(step["max_age_s"])
                and isinstance(previous_step, dict)
                and str(previous_step.get("status", "") or "") in {"OK", "SKIPPED_FRESH"}
            ):
                step_result = _reuse_step(step, previous_step, previous_age_s)
                _log(f"{step['id']}: reused previous OK result ({int(previous_age_s)}s old)")
            else:
                step_result = _run_step(step)
            steps.append(step_result)
            if step_result["status"] not in {"OK", "SKIPPED_FRESH"}:
                if step_result["critical"]:
                    critical_failure = True
                    failed_critical.append(step_result["id"])
                else:
                    failed_noncritical.append(step_result["id"])

        if include_summary:
            summary_step = _run_step(
                {
                    "id": "discord_summary",
                    "module": "argus_flow.ops.discord_alerts",
                    "args": ["--summary"],
                    "critical": False,
                }
            )
            steps.append(summary_step)
            if summary_step["status"] != "OK":
                failed_noncritical.append(summary_step["id"])

        payload = {
            "timestamp": _now_iso(),
            "started_at": started_at,
            "status": "FAILED" if critical_failure else "OK",
            "include_summary": include_summary,
            "critical_failure": critical_failure,
            "failed_critical": failed_critical,
            "failed_noncritical": failed_noncritical,
            "steps": steps,
        }
        _write_status(payload)
        if critical_failure:
            _log(f"managed truth refresh FAILED critical={', '.join(failed_critical)}")
            return 1
        if failed_noncritical:
            _log(f"managed truth refresh OK with noncritical failures: {', '.join(failed_noncritical)}")
        else:
            _log("managed truth refresh OK")
        return 0
    finally:
        lock.release()


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh canonical Argus governance and oversight truth surfaces")
    parser.add_argument(
        "--include-summary",
        action="store_true",
        help="Send the Discord summary after the refresh completes",
    )
    parser.add_argument(
        "--accept-existing-age-s",
        type=int,
        default=DEFAULT_ACCEPT_EXISTING_AGE_S,
        help="If another refresh is already running, accept a recent OK status younger than this age",
    )
    args = parser.parse_args()
    raise SystemExit(
        refresh_managed_truth(
            include_summary=bool(args.include_summary),
            accept_existing_age_s=max(int(args.accept_existing_age_s), 0),
        )
    )


if __name__ == "__main__":
    main()
