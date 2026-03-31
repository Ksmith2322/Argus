"""Alert Escalation v2 - canonical incident state + Discord notifications.

Builds one durable alert surface for the dashboard and Discord:
- active issues
- manual-action items
- report freshness
- recent open/resolved alert events

Usage:
    python -m argus_flow.ops.alert_escalation_v2
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

REPO = Path(__file__).resolve().parents[2]
LOGS = REPO / "argus_flow" / "logs"
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
ALERT_COOLDOWN_S = 3600
ALERT_HISTORY_PATH = LOGS / "alert_history.json"
ALERT_STATE_PATH = LOGS / "alert_state.json"
ALERT_EVENTS_PATH = LOGS / "alert_events.jsonl"

SEVERITY_RANK = {"INFO": 0, "WARNING": 1, "HIGH": 2, "CRITICAL": 3}
SEVERITY_COLOR = {
    "INFO": 0x00D4FF,
    "WARNING": 0xFFA500,
    "HIGH": 0xFF8C00,
    "CRITICAL": 0xFF0000,
    "RESOLVED": 0x00FF88,
}
MANUAL_SEVERITIES = {"HIGH", "CRITICAL"}

REPORT_SPECS = [
    {
        "id": "position_monitor",
        "label": "Position Monitor",
        "path": LOGS / "position_monitor.json",
        "fresh_s": 900,
        "manual_when_stale": True,
    },
    {
        "id": "risk_oversight",
        "label": "Risk Oversight",
        "path": LOGS / "risk_oversight_report.json",
        "fresh_s": 900,
        "manual_when_stale": True,
    },
    {
        "id": "promotion_gate",
        "label": "Promotion Gate",
        "path": LOGS / "promotion_gate_report.json",
        "fresh_s": 36 * 3600,
        "manual_when_stale": False,
    },
    {
        "id": "artifact_divergence",
        "label": "Artifact Divergence",
        "path": LOGS / "artifact_divergence_report.json",
        "fresh_s": 36 * 3600,
        "manual_when_stale": False,
    },
    {
        "id": "divergence",
        "label": "Divergence Guard",
        "path": LOGS / "divergence_report.json",
        "fresh_s": 36 * 3600,
        "manual_when_stale": False,
    },
    {
        "id": "kill_discipline",
        "label": "Kill Discipline",
        "path": LOGS / "kill_discipline_report.json",
        "fresh_s": 36 * 3600,
        "manual_when_stale": False,
    },
]


def _load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _append_events(events: list[dict[str, Any]]) -> None:
    if not events:
        return
    ALERT_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERT_EVENTS_PATH, "a", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, default=str) + "\n")


def _file_age_s(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        return int(max(0.0, time.time() - path.stat().st_mtime))
    except OSError:
        return None


def _issue(
    *,
    key: str,
    category: str,
    scope: str,
    severity: str,
    message: str,
    source_report: str,
    requires_manual_action: bool = False,
    detail: str = "",
    report_age_s: int | None = None,
) -> dict[str, Any]:
    sev = severity.upper()
    return {
        "key": key,
        "category": category,
        "scope": scope,
        "severity": sev,
        "message": message,
        "detail": detail,
        "source_report": source_report,
        "report_age_s": report_age_s,
        "requires_manual_action": requires_manual_action or sev in MANUAL_SEVERITIES,
        "color": SEVERITY_COLOR.get(sev, SEVERITY_COLOR["WARNING"]),
    }


def _iter_report_items(report: dict | list | None, *, runner_key: str = "runners") -> list[dict]:
    if report is None:
        return []
    if isinstance(report, list):
        return [item for item in report if isinstance(item, dict)]
    if isinstance(report, dict):
        if runner_key in report and isinstance(report.get(runner_key), list):
            return [item for item in report.get(runner_key, []) if isinstance(item, dict)]
        if "pairs" in report and isinstance(report.get("pairs"), list):
            return [item for item in report.get("pairs", []) if isinstance(item, dict)]
        return [report]
    return []


def _check_report_freshness() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    freshness: list[dict[str, Any]] = []
    for spec in REPORT_SPECS:
        age = _file_age_s(spec["path"])
        exists = spec["path"].exists()
        if age is None:
            freshness.append(
                {
                    "id": spec["id"],
                    "label": spec["label"],
                    "path": str(spec["path"]),
                    "exists": False,
                    "age_s": None,
                    "fresh_s": spec["fresh_s"],
                    "status": "MISSING",
                }
            )
            issues.append(
                _issue(
                    key=f"report_freshness:{spec['id']}:missing",
                    category="report_freshness",
                    scope=spec["label"],
                    severity="HIGH" if spec["manual_when_stale"] else "WARNING",
                    message=f"{spec['label']} report missing",
                    source_report=spec["path"].name,
                    requires_manual_action=spec["manual_when_stale"],
                )
            )
            continue

        status = "FRESH" if age <= spec["fresh_s"] else "STALE"
        freshness.append(
            {
                "id": spec["id"],
                "label": spec["label"],
                "path": str(spec["path"]),
                "exists": exists,
                "age_s": age,
                "fresh_s": spec["fresh_s"],
                "status": status,
            }
        )
        if status == "STALE":
            issues.append(
                _issue(
                    key=f"report_freshness:{spec['id']}:stale",
                    category="report_freshness",
                    scope=spec["label"],
                    severity="HIGH" if spec["manual_when_stale"] else "WARNING",
                    message=f"{spec['label']} report is stale ({age}s old, budget {spec['fresh_s']}s)",
                    source_report=spec["path"].name,
                    requires_manual_action=spec["manual_when_stale"],
                    report_age_s=age,
                )
            )
    freshness.sort(key=lambda item: (item["status"] != "STALE", item["status"] == "FRESH", item["label"]))
    return issues, freshness


def _check_kill_discipline() -> list[dict[str, Any]]:
    path = LOGS / "kill_discipline_report.json"
    data = _load_json(path)
    if data is None:
        return []
    issues: list[dict[str, Any]] = []
    for item in _iter_report_items(data):
        verdict = str(item.get("verdict", "")).upper()
        if verdict not in {"KILL", "WATCH"}:
            continue
        pair = str(item.get("pair", item.get("symbol", "unknown")))
        reason = str(item.get("reason", item.get("message", "no reason provided")))
        severity = "CRITICAL" if verdict == "KILL" else "WARNING"
        issues.append(
            _issue(
                key=f"kill_discipline:{pair}:{verdict}",
                category="kill_discipline",
                scope=pair,
                severity=severity,
                message=f"{verdict} triggered for {pair}: {reason}",
                detail=reason,
                source_report=path.name,
                requires_manual_action=verdict == "KILL",
                report_age_s=_file_age_s(path),
            )
        )
    return issues


def _check_divergence() -> list[dict[str, Any]]:
    path = LOGS / "divergence_report.json"
    data = _load_json(path)
    if data is None:
        return []
    issues: list[dict[str, Any]] = []
    for item in _iter_report_items(data):
        verdict = str(item.get("verdict", item.get("status", ""))).upper()
        if verdict not in {"KILL", "WATCH"}:
            continue
        pair = str(item.get("pair", item.get("symbol", item.get("name", "unknown"))))
        reason = str(item.get("reason", item.get("message", "no reason provided")))
        severity = "HIGH" if verdict == "KILL" else "WARNING"
        issues.append(
            _issue(
                key=f"divergence:{pair}:{verdict}",
                category="divergence",
                scope=pair,
                severity=severity,
                message=f"Divergence {verdict} for {pair}: {reason}",
                detail=reason,
                source_report=path.name,
                requires_manual_action=verdict == "KILL",
                report_age_s=_file_age_s(path),
            )
        )
    return issues


def _check_artifact_divergence() -> list[dict[str, Any]]:
    path = LOGS / "artifact_divergence_report.json"
    data = _load_json(path)
    if data is None:
        return []
    issues: list[dict[str, Any]] = []
    for item in _iter_report_items(data):
        status = str(item.get("status", "")).upper()
        if status not in {"DIVERGENT", "DEGRADED"}:
            continue
        pair = str(item.get("pair", item.get("symbol", item.get("name", "unknown"))))
        failed_checks = [check for check in item.get("checks", []) if isinstance(check, dict) and not check.get("passed")]
        first = failed_checks[0] if failed_checks else {}
        lead = str(first.get("name", "issue")).replace("_", " ")
        detail = str(first.get("detail", item.get("details", "")))
        manual = any(str(check.get("severity", "")).upper() == "FAIL" for check in failed_checks)
        severity = "HIGH" if manual or status == "DIVERGENT" else "WARNING"
        issues.append(
            _issue(
                key=f"artifact_divergence:{pair}:{status}",
                category="artifact_divergence",
                scope=pair,
                severity=severity,
                message=f"Artifact {status} for {pair}: {lead} - {detail}",
                detail=detail,
                source_report=path.name,
                requires_manual_action=manual,
                report_age_s=_file_age_s(path),
            )
        )
    return issues


def _check_risk_oversight() -> list[dict[str, Any]]:
    path = LOGS / "risk_oversight_report.json"
    data = _load_json(path)
    if data is None:
        return []
    issues: list[dict[str, Any]] = []
    for item in _iter_report_items(data):
        level = str(item.get("level", item.get("status", ""))).upper()
        if level not in {"RED", "YELLOW"}:
            continue
        metric = str(item.get("metric", item.get("check", "fleet")))
        message = str(item.get("message", "risk oversight alert"))
        recommendations = item.get("recommendations", [])
        rec_line = ""
        if isinstance(recommendations, list) and recommendations:
            rec_line = f" | next: {recommendations[0]}"
        severity = "CRITICAL" if level == "RED" else "WARNING"
        issues.append(
            _issue(
                key=f"risk_oversight:{metric}:{level}",
                category="risk_oversight",
                scope=metric,
                severity=severity,
                message=f"Risk {level} - {metric}: {message}{rec_line}",
                detail=message,
                source_report=path.name,
                requires_manual_action=level == "RED" or bool(recommendations),
                report_age_s=_file_age_s(path),
            )
        )
    return issues


def _check_position_monitor() -> list[dict[str, Any]]:
    path = LOGS / "position_monitor.json"
    data = _load_json(path)
    if data is None or not isinstance(data, dict):
        return []
    issues: list[dict[str, Any]] = []
    age = _file_age_s(path)

    if not data.get("ibkr_connected", False):
        issues.append(
            _issue(
                key="position_monitor:fleet:broker_disconnected",
                category="position_monitor",
                scope="fleet",
                severity="CRITICAL",
                message="Broker truth is unavailable: position monitor reports disconnected IBKR",
                source_report=path.name,
                requires_manual_action=True,
                report_age_s=age,
            )
        )

    for runner in data.get("runners", []):
        if not isinstance(runner, dict):
            continue
        severity = str(runner.get("severity", "OK")).upper()
        if severity == "OK":
            continue
        name = str(runner.get("name", "unknown"))
        runner_pos = str(runner.get("runner_position", "UNKNOWN"))
        broker_pos = str(runner.get("ibkr_position", "UNKNOWN"))
        recon = str(runner.get("broker_reconciliation", ""))
        detail = (
            f"runner={runner_pos}, broker={broker_pos}, hb_age={runner.get('heartbeat_age_s', '?')}s"
            + (f", recon={recon}" if recon else "")
        )
        issues.append(
            _issue(
                key=f"position_monitor:{name}:{severity}",
                category="position_monitor",
                scope=name,
                severity="CRITICAL" if severity == "CRITICAL" else ("HIGH" if severity in {"MISMATCH", "ERROR"} else "WARNING"),
                message=f"{severity} for {name}: {detail}",
                detail=detail,
                source_report=path.name,
                requires_manual_action=severity in {"CRITICAL", "MISMATCH", "ERROR"} or bool(runner.get("state_read_error")),
                report_age_s=age,
            )
        )

    return issues


def _load_alert_history() -> dict[str, float]:
    data = _load_json(ALERT_HISTORY_PATH)
    return data if isinstance(data, dict) else {}


def _save_alert_history(history: dict[str, float]) -> None:
    _write_json(ALERT_HISTORY_PATH, history)


def _load_alert_state() -> dict[str, Any]:
    data = _load_json(ALERT_STATE_PATH)
    return data if isinstance(data, dict) else {}


def _is_cooled_down(history: dict[str, float], key: str, now_ts: float) -> bool:
    last_ts = float(history.get(key, 0) or 0)
    return (now_ts - last_ts) < ALERT_COOLDOWN_S


def _sort_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        issues,
        key=lambda item: (
            -SEVERITY_RANK.get(str(item.get("severity", "WARNING")).upper(), 1),
            str(item.get("category", "")),
            str(item.get("scope", "")),
        ),
    )


def run() -> None:
    """Collect incidents, update alert state, and notify Discord."""
    from argus_flow.ops.discord_alerts import send_discord

    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    history = _load_alert_history()
    previous_state = _load_alert_state()
    previous_active = {
        str(issue.get("key")): issue
        for issue in previous_state.get("active_issues", [])
        if isinstance(issue, dict) and issue.get("key")
    }

    current_issues: list[dict[str, Any]] = []
    report_freshness_issues, report_freshness = _check_report_freshness()
    current_issues.extend(report_freshness_issues)
    for checker in (
        _check_position_monitor,
        _check_risk_oversight,
        _check_kill_discipline,
        _check_divergence,
        _check_artifact_divergence,
    ):
        try:
            current_issues.extend(checker())
        except Exception as exc:
            current_issues.append(
                _issue(
                    key=f"internal:{checker.__name__}",
                    category="internal",
                    scope=checker.__name__,
                    severity="WARNING",
                    message=f"{checker.__name__} failed during alert evaluation: {exc}",
                    detail=str(exc),
                    source_report="alert_escalation_v2.py",
                )
            )

    current_map = {issue["key"]: issue for issue in _sort_issues(current_issues)}
    opened = [current_map[key] for key in current_map if key not in previous_active]
    resolved = [previous_active[key] for key in previous_active if key not in current_map]
    reminders = [
        issue
        for key, issue in current_map.items()
        if key in previous_active and not _is_cooled_down(history, key, now_ts)
    ]

    events: list[dict[str, Any]] = []
    for issue in opened:
        events.append(
            {
                "ts": now.isoformat(),
                "kind": "opened",
                "key": issue["key"],
                "category": issue["category"],
                "scope": issue["scope"],
                "severity": issue["severity"],
                "requires_manual_action": issue["requires_manual_action"],
                "message": issue["message"],
            }
        )
    for issue in resolved:
        events.append(
            {
                "ts": now.isoformat(),
                "kind": "resolved",
                "key": issue["key"],
                "category": issue.get("category", ""),
                "scope": issue.get("scope", ""),
                "severity": issue.get("severity", "RESOLVED"),
                "requires_manual_action": False,
                "message": issue.get("message", ""),
            }
        )
    for issue in reminders:
        events.append(
            {
                "ts": now.isoformat(),
                "kind": "reminder",
                "key": issue["key"],
                "category": issue["category"],
                "scope": issue["scope"],
                "severity": issue["severity"],
                "requires_manual_action": issue["requires_manual_action"],
                "message": issue["message"],
            }
        )

    notifications: list[dict[str, Any]] = []
    notifications.extend(opened)
    notifications.extend(reminders)
    for issue in resolved:
        notifications.append(
            {
                "key": issue.get("key"),
                "category": issue.get("category", "resolved"),
                "scope": issue.get("scope", ""),
                "severity": "RESOLVED",
                "message": f"Resolved: {issue.get('message', '')}",
                "color": SEVERITY_COLOR["RESOLVED"],
                "requires_manual_action": False,
            }
        )

    if notifications:
        embeds = []
        for issue in notifications:
            severity = str(issue.get("severity", "WARNING")).upper()
            title_prefix = "Resolved" if severity == "RESOLVED" else "Issue"
            manual = " | MANUAL ACTION" if issue.get("requires_manual_action") else ""
            embeds.append(
                {
                    "title": f"Argus {title_prefix}: {issue.get('category', '').replace('_', ' ').title()}",
                    "description": f"{issue.get('message', '')}{manual}",
                    "color": int(issue.get("color", SEVERITY_COLOR["WARNING"])),
                    "timestamp": now.isoformat(),
                    "fields": [
                        {"name": "Scope", "value": str(issue.get("scope", "fleet")), "inline": True},
                        {"name": "Severity", "value": severity, "inline": True},
                    ],
                }
            )
        for i in range(0, len(embeds), 10):
            batch = embeds[i : i + 10]
            ok = send_discord(
                content=f"**Argus Alert Escalation** - {now.strftime('%Y-%m-%d %H:%M UTC')}",
                embeds=batch,
            )
            if ok:
                print(f"  Sent {len(batch)} alert embed(s) to Discord.")
            elif WEBHOOK_URL:
                print("  Failed to send Discord alert batch.")

    for issue in opened + reminders:
        history[str(issue['key'])] = now_ts
    history["_last_run"] = now.isoformat()
    _save_alert_history(history)

    active_issues = list(current_map.values())
    manual_actions = [issue for issue in active_issues if issue.get("requires_manual_action")]
    max_severity = "OK"
    if active_issues:
        max_issue = max(active_issues, key=lambda item: SEVERITY_RANK.get(item.get("severity", "WARNING"), 1))
        max_severity = str(max_issue.get("severity", "WARNING"))

    state = {
        "timestamp": now.isoformat(),
        "webhook_configured": bool(WEBHOOK_URL),
        "summary": {
            "active_issues": len(active_issues),
            "manual_actions": len(manual_actions),
            "opened_this_run": len(opened),
            "resolved_this_run": len(resolved),
            "reminders_this_run": len(reminders),
            "max_severity": max_severity,
        },
        "active_issues": active_issues,
        "manual_actions": manual_actions,
        "resolved_this_run": resolved,
        "report_freshness": report_freshness,
    }
    _write_json(ALERT_STATE_PATH, state)
    _append_events(events)

    print(
        f"[{now.isoformat()}] active={len(active_issues)} opened={len(opened)} "
        f"resolved={len(resolved)} manual={len(manual_actions)} max_severity={max_severity}"
    )


if __name__ == "__main__":
    run()
