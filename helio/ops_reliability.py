"""Build operations reliability evidence for the capital ladder.

The capital ladder should not depend on an operator remembering to attest
"ops were clean." This module reduces existing runtime evidence into the
small set of fields the ladder gates on:

    python -m helio.ops_reliability
    python -m helio.ops_reliability --json

Missing or stale critical evidence is treated as an incident. The goal is a
boring report that can be scheduled daily and trusted by the real-money
boundary.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

REPO = Path(__file__).resolve().parents[1]
LOGS_DIR = REPO / "argus_flow" / "logs"

PHANTOM_ANNOTATIONS = REPO / "ops" / "reports" / "system_audit" / "phantom_trade_annotations.csv"
PHANTOM_RESOLUTIONS = REPO / "argus_flow" / "configs" / "phantom_trade_resolutions.json"
CANONICAL_RECONCILE = LOGS_DIR / "canonical_reconcile.json"
RECONCILIATION_REPORT = LOGS_DIR / "reconciliation_report.json"
BROKER_DRIFT_STATE = LOGS_DIR / "_risk" / "broker_drift_state.json"
RISK_OVERSIGHT_REPORT = LOGS_DIR / "risk_oversight_report.json"
WATCHDOG_LOG = LOGS_DIR / "watchdog_managed.log"
CANONICAL_FAILOVER_LOG = LOGS_DIR / "canonical_fills_failover.jsonl"

OUTPUT_PATH = LOGS_DIR / "ops_reliability_report.json"
HISTORY_PATH = LOGS_DIR / "ops_reliability_history.jsonl"

DEFAULT_WINDOW_DAYS = 30
CRITICAL_SOURCE_MAX_AGE = timedelta(hours=36)
WATCHDOG_LOG_MAX_AGE = timedelta(hours=6)
KILL_SWITCH_DRILL_MAX_AGE = timedelta(days=60)
MAX_LOG_LINES = 20_000

_TS_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
)
_LIQUIDATION_ALERT_RE = re.compile(r"\b(FORCED\s+LIQUIDATION|LIQUIDATION|LIQUIDATED)\b")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        match = _TS_RE.search(text)
        if match:
            text = match.group("ts")
        text = text.replace("Z", "+00:00").replace(" ", "T")
        if len(text) >= 5 and text[-5] in {"+", "-"} and ":" not in text[-5:]:
            text = text[:-2] + ":" + text[-2:]
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _in_window(ts: datetime | None, now: datetime, days: int, *, unknown_is_recent: bool = False) -> bool:
    if ts is None:
        return unknown_is_recent
    return now - timedelta(days=days) <= ts <= now + timedelta(minutes=5)


def _read_json(path: Path) -> tuple[dict[str, Any], str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return {}, "missing"
    except json.JSONDecodeError as exc:
        return {}, f"json_error: {exc}"
    except OSError as exc:
        return {}, f"read_error: {exc}"


def _read_csv_rows(path: Path) -> tuple[list[dict[str, str]], str | None]:
    try:
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f)), None
    except FileNotFoundError:
        return [], "missing"
    except OSError as exc:
        return [], f"read_error: {exc}"


def _tail_lines(path: Path, max_lines: int = MAX_LOG_LINES) -> tuple[list[str], str | None]:
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            return list(deque(f, maxlen=max_lines)), None
    except FileNotFoundError:
        return [], "missing"
    except OSError as exc:
        return [], f"read_error: {exc}"


def _append_incident(
    incidents: list[dict[str, Any]],
    *,
    kind: str,
    source: str,
    severity: str,
    timestamp: datetime | None = None,
    details: str = "",
    count: int | None = None,
) -> None:
    row: dict[str, Any] = {
        "kind": kind,
        "source": source,
        "severity": severity,
        "timestamp": _iso(timestamp),
    }
    if details:
        row["details"] = details
    if count is not None:
        row["count"] = count
    incidents.append(row)


def _source_record(
    *,
    path: Path,
    exists: bool,
    timestamp: datetime | None,
    now: datetime,
    error: str | None = None,
    critical: bool = False,
) -> dict[str, Any]:
    age_hours = None
    if timestamp is not None:
        age_hours = round((now - timestamp).total_seconds() / 3600.0, 2)
    stale = critical and (timestamp is None or now - timestamp > CRITICAL_SOURCE_MAX_AGE)
    return {
        "path": _rel(path),
        "exists": exists,
        "timestamp": _iso(timestamp),
        "age_hours": age_hours,
        "critical": critical,
        "stale": stale,
        "error": error,
    }


def _source_is_bad(source: dict[str, Any]) -> bool:
    return bool(source.get("critical") and (not source.get("exists") or source.get("stale") or source.get("error")))


def _phantom_row_resolved(row: dict[str, Any], resolutions: Iterable[dict[str, Any]]) -> bool:
    row_strategy = str(row.get("strategy") or "")
    row_source = str(row.get("source_file") or "")
    row_ts = _parse_ts(row.get("ts"))
    for resolution in resolutions:
        if row_strategy != str(resolution.get("strategy") or ""):
            continue
        source_file = str(resolution.get("source_file") or "")
        if source_file and source_file != row_source:
            continue
        resolved_after = _parse_ts(resolution.get("resolved_after"))
        if resolved_after is None:
            continue
        if row_ts is None or row_ts <= resolved_after:
            return True
    return False


def _count_phantom_rows(
    rows: Iterable[dict[str, Any]],
    now: datetime,
    window_days: int,
    resolutions: Iterable[dict[str, Any]] | None = None,
) -> tuple[int, list[dict[str, Any]], int]:
    count = 0
    resolved_count = 0
    examples: list[dict[str, Any]] = []
    resolutions = list(resolutions or [])
    for row in rows:
        annotation = str(row.get("annotation") or "").upper()
        if "PHANTOM" not in annotation:
            continue
        ts = _parse_ts(row.get("ts"))
        if not _in_window(ts, now, window_days, unknown_is_recent=True):
            continue
        if _phantom_row_resolved(row, resolutions):
            resolved_count += 1
            continue
        count += 1
        if len(examples) < 5:
            examples.append({
                "strategy": row.get("strategy"),
                "symbol": row.get("symbol"),
                "timestamp": _iso(ts),
                "source_file": row.get("source_file"),
            })
    return count, examples, resolved_count


def _parse_tripped_flag(value: Any) -> bool:
    """Safely interpret a 'tripped' field that came from JSON.

    Defensive against manual edits to broker_drift_state.json. ``bool("false")``
    is True in Python (any non-empty string is truthy), so a manual edit that
    writes a string instead of a boolean would silently re-trip the gate.
    Handle the common cases explicitly.
    """
    if value is True:
        return True
    if value is False or value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() == "true"
    if isinstance(value, (int, float)):
        return bool(value)
    # Unknown shape — fail closed (treat as tripped).
    return True


def _latest_ts_from_lines(lines: Iterable[str]) -> datetime | None:
    """Return the most recent parseable timestamp from a log tail.

    Used to detect a frozen watchdog: if the last logged event is older
    than ``WATCHDOG_LOG_MAX_AGE``, the cascade counter is unreliable
    because nothing has been observed in the meantime.
    """
    latest: datetime | None = None
    for line in lines:
        ts = _parse_ts(line)
        if ts is None:
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest


def _count_watchdog_events(lines: Iterable[str], now: datetime, window_days: int) -> tuple[int, list[dict[str, Any]]]:
    """Count TWS/Gateway cascade classes once per day to avoid noisy heartbeat
    lines turning one outage into hundreds of incidents."""
    events: dict[tuple[date, str], dict[str, Any]] = {}
    for line in lines:
        ts = _parse_ts(line)
        if not _in_window(ts, now, window_days):
            continue
        upper = line.upper()
        kind = ""
        if "MAX RESTARTS" in upper:
            kind = "max_restarts"
        elif "PORT=FALSE" in upper and ("GATEWAY=UP" in upper or "TWS" in upper):
            kind = "tws_api_port_false"
        elif ("TWS" in upper or "IB GATEWAY" in upper or "IBGATEWAY" in upper) and (
            "NOT RUNNING" in upper or "UNREACHABLE" in upper or "DOWN" in upper
        ):
            kind = "tws_down"
        if not kind or ts is None:
            continue
        key = (ts.date(), kind)
        events.setdefault(key, {
            "kind": kind,
            "timestamp": _iso(ts),
            "details": line.strip()[:240],
        })
    ordered = [events[k] for k in sorted(events)]
    return len(ordered), ordered


def _count_margin_alerts(
    *,
    risk_report: dict[str, Any],
    risk_ts: datetime | None,
    watchdog_lines: Iterable[str],
    now: datetime,
    window_days: int,
) -> tuple[int, list[dict[str, Any]]]:
    events: dict[tuple[date, str], dict[str, Any]] = {}

    text = json.dumps(risk_report, default=str).upper() if risk_report else ""
    if risk_report and risk_ts and _in_window(risk_ts, now, window_days):
        status = str(risk_report.get("status") or risk_report.get("level") or "").upper()
        if "MARGIN" in text or _LIQUIDATION_ALERT_RE.search(text):
            events[(risk_ts.date(), "risk_oversight_margin")] = {
                "kind": "risk_oversight_margin",
                "timestamp": _iso(risk_ts),
                "details": status or "risk report margin alert",
            }

    for line in watchdog_lines:
        ts = _parse_ts(line)
        if not _in_window(ts, now, window_days):
            continue
        upper = line.upper()
        if "MARGIN" not in upper and not _LIQUIDATION_ALERT_RE.search(upper):
            continue
        if ts is None:
            continue
        events[(ts.date(), "watchdog_margin")] = {
            "kind": "watchdog_margin",
            "timestamp": _iso(ts),
            "details": line.strip()[:240],
        }

    ordered = [events[k] for k in sorted(events)]
    return len(ordered), ordered


def _count_jsonl_events(path: Path, now: datetime, window_days: int) -> tuple[int, list[dict[str, Any]], str | None]:
    rows: list[dict[str, Any]] = []
    error = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return 0, rows, "missing"
    except OSError as exc:
        return 0, rows, f"read_error: {exc}"

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            error = "jsonl_error"
            continue
        ts = _parse_ts(row.get("ts") or row.get("timestamp") or row.get("generated_at"))
        if not _in_window(ts, now, window_days, unknown_is_recent=True):
            continue
        if len(rows) < 5:
            rows.append({"timestamp": _iso(ts), "event": row})
    return len(rows), rows, error


def _latest_by_date(records: Iterable[dict[str, Any]], key: str) -> dict[date, bool]:
    latest: dict[date, tuple[datetime, bool]] = {}
    for record in records:
        ts = _parse_ts(record.get("generated_at") or record.get("timestamp") or record.get("ts"))
        if ts is None:
            continue
        current = latest.get(ts.date())
        if current is None or ts >= current[0]:
            latest[ts.date()] = (ts, bool(record.get(key)))
    return {day: value for day, (_, value) in latest.items()}


def _consecutive_true_days(
    history: Iterable[dict[str, Any]],
    key: str,
    current: dict[str, Any],
    now: datetime,
) -> int:
    statuses = _latest_by_date(history, key)
    statuses[now.date()] = bool(current.get(key))

    streak = 0
    day = now.date()
    while statuses.get(day) is True:
        streak += 1
        day = day - timedelta(days=1)
    return streak


def _load_history(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        return []

    rows: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _append_history(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "generated_at": report.get("generated_at"),
        "clean_current": report.get("clean_current"),
        "broker_reconcile_passed": report.get("broker_reconcile_passed"),
        "silent_failures_30d": report.get("silent_failures_30d"),
        "phantom_fills_30d": report.get("phantom_fills_30d"),
        "tws_cascades_30d": report.get("tws_cascades_30d"),
        "margin_alerts_30d": report.get("margin_alerts_30d"),
        "open_p0_incidents": report.get("open_p0_incidents"),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, sort_keys=True) + "\n")


def _canonical_timestamp(report: dict[str, Any]) -> datetime | None:
    return _parse_ts(report.get("checked_at") or report.get("generated_at") or report.get("timestamp"))


def _reconciliation_timestamp(report: dict[str, Any]) -> datetime | None:
    return _parse_ts(report.get("generated_at") or report.get("checked_at") or report.get("timestamp"))


def _risk_timestamp(report: dict[str, Any]) -> datetime | None:
    return _parse_ts(report.get("timestamp") or report.get("generated_at") or report.get("checked_at"))


def _broker_timestamp(report: dict[str, Any]) -> datetime | None:
    return _parse_ts(report.get("ts") or report.get("timestamp") or report.get("generated_at"))


def _drill_record_passed(text: str) -> bool:
    """Decide whether a drill evidence blob represents a real PASS.

    Prefers structured JSON fields (``pass`` or ``status``) so a WARN/FAIL
    drill doesn't accidentally clear the gate just because the substring
    ``PASS`` appears in a descriptive note. Falls back to a stricter
    substring check for plain-log evidence.

    Strips a leading UTF-8 BOM before parsing — PowerShell's default
    ``Out-File -Encoding utf8`` writes the BOM, and the kill-switch drill
    evidence is produced by PowerShell.
    """
    if text.startswith("﻿"):
        text = text.lstrip("﻿")
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        upper = text.upper()
        # Plain-log fallback: require an explicit pass marker, not the
        # word "PASS" anywhere in the body.
        return any(
            token in upper
            for token in ("DRILL_OK", "STATUS=PASS", "STATUS: PASS", "RESULT=PASS", "RESULT: PASS")
        )
    if isinstance(record, dict):
        if "pass" in record:
            return bool(record.get("pass"))
        status = str(record.get("status") or "").strip().upper()
        if status in {"PASS", "PASSED", "SUCCESS", "OK"}:
            return True
        if status in {"WARN", "FAIL", "FAILED", "ERROR"}:
            return False
    return False


def _detect_kill_switch_tested(now: datetime) -> tuple[bool, dict[str, Any] | None]:
    patterns = [
        LOGS_DIR / "*kill_switch*drill*.json",
        LOGS_DIR / "*kill_switch*drill*.jsonl",
        LOGS_DIR / "*kill_switch*drill*.log",
        LOGS_DIR / "*drill*kill*.json",
        LOGS_DIR / "*drill*kill*.log",
        REPO / "ops" / "reports" / "system_audit" / "*kill_switch*drill*",
    ]
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(pattern.parent.glob(pattern.name))

    latest: tuple[datetime, Path, str] | None = None
    for path in candidates:
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if now - mtime > KILL_SWITCH_DRILL_MAX_AGE:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not _drill_record_passed(text):
            continue
        if latest is None or mtime > latest[0]:
            latest = (mtime, path, text[:240])

    if latest is None:
        return False, None
    ts, path, excerpt = latest
    return True, {"path": _rel(path), "timestamp": _iso(ts), "excerpt": excerpt}


def build_report(
    *,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    history_path: Path | str | None = None,
) -> dict[str, Any]:
    now = (now or _utc_now()).astimezone(timezone.utc)
    history = _load_history(Path(history_path) if history_path is not None else HISTORY_PATH)
    incidents: list[dict[str, Any]] = []
    sources: dict[str, dict[str, Any]] = {}

    phantom_rows, phantom_error = _read_csv_rows(PHANTOM_ANNOTATIONS)
    phantom_resolutions, phantom_resolution_error = _read_json(PHANTOM_RESOLUTIONS)
    resolution_rows = list(phantom_resolutions.get("resolutions") or [])
    phantom_count, phantom_examples, resolved_phantom_count = _count_phantom_rows(
        phantom_rows,
        now,
        window_days,
        resolution_rows,
    )
    sources["phantom_trade_annotations"] = {
        "path": _rel(PHANTOM_ANNOTATIONS),
        "exists": PHANTOM_ANNOTATIONS.exists(),
        "error": phantom_error,
        "rows": len(phantom_rows),
    }
    sources["phantom_trade_resolutions"] = {
        "path": _rel(PHANTOM_RESOLUTIONS),
        "exists": PHANTOM_RESOLUTIONS.exists(),
        "error": phantom_resolution_error,
        "resolutions": len(resolution_rows),
    }
    if phantom_count:
        _append_incident(
            incidents,
            kind="phantom_fills",
            source="phantom_trade_annotations",
            severity="P0",
            details="phantom fills present in promotion audit annotations",
            count=phantom_count,
        )

    canonical, canonical_error = _read_json(CANONICAL_RECONCILE)
    canonical_ts = _canonical_timestamp(canonical)
    canonical_source = _source_record(
        path=CANONICAL_RECONCILE,
        exists=CANONICAL_RECONCILE.exists(),
        timestamp=canonical_ts,
        now=now,
        error=canonical_error,
        critical=True,
    )
    sources["canonical_reconcile"] = canonical_source
    canonical_drift_count = int(canonical.get("drift_count") or 0)
    canonical_clean = canonical_drift_count == 0 and not _source_is_bad(canonical_source)
    if _source_is_bad(canonical_source):
        _append_incident(
            incidents,
            kind="critical_source_bad",
            source="canonical_reconcile",
            severity="P0",
            timestamp=canonical_ts,
            details="critical reconcile report missing, stale, or unreadable",
        )
    elif canonical_drift_count:
        _append_incident(
            incidents,
            kind="canonical_reconcile_drift",
            source="canonical_reconcile",
            severity="P0",
            timestamp=canonical_ts,
            details=f"{canonical_drift_count} strategies have canonical-vs-csv count drift",
            count=canonical_drift_count,
        )

    reconciliation, reconciliation_error = _read_json(RECONCILIATION_REPORT)
    reconciliation_ts = _reconciliation_timestamp(reconciliation)
    reconciliation_source = _source_record(
        path=RECONCILIATION_REPORT,
        exists=RECONCILIATION_REPORT.exists(),
        timestamp=reconciliation_ts,
        now=now,
        error=reconciliation_error,
        critical=True,
    )
    sources["reconciliation_report"] = reconciliation_source
    rec_summary = reconciliation.get("summary") or {}
    drift_strategies = list(rec_summary.get("drift_strategies") or [])
    all_reconciled = bool(rec_summary.get("all_reconciled")) and not _source_is_bad(reconciliation_source)
    if _source_is_bad(reconciliation_source):
        _append_incident(
            incidents,
            kind="critical_source_bad",
            source="reconciliation_report",
            severity="P0",
            timestamp=reconciliation_ts,
            details="critical reconciliation report missing, stale, or unreadable",
        )
    elif not all_reconciled:
        _append_incident(
            incidents,
            kind="reconciliation_drift",
            source="reconciliation_report",
            severity="P0",
            timestamp=reconciliation_ts,
            details="canonical fills do not reconcile with per-strategy trade logs",
            count=len(drift_strategies),
        )

    broker, broker_error = _read_json(BROKER_DRIFT_STATE)
    broker_ts = _broker_timestamp(broker)
    broker_source = _source_record(
        path=BROKER_DRIFT_STATE,
        exists=BROKER_DRIFT_STATE.exists(),
        timestamp=broker_ts,
        now=now,
        error=broker_error,
        critical=True,
    )
    sources["broker_drift_state"] = broker_source
    broker_tripped = _parse_tripped_flag(broker.get("tripped"))
    broker_reconcile_clean = not broker_tripped and not _source_is_bad(broker_source)
    if _source_is_bad(broker_source):
        _append_incident(
            incidents,
            kind="critical_source_bad",
            source="broker_drift_state",
            severity="P0",
            timestamp=broker_ts,
            details="broker drift state missing, stale, or unreadable",
        )
    elif broker_tripped:
        _append_incident(
            incidents,
            kind="broker_drift_tripped",
            source="broker_drift_state",
            severity="P0",
            timestamp=broker_ts,
            details=f"broker divergence_pct={broker.get('divergence_pct')}",
        )

    risk, risk_error = _read_json(RISK_OVERSIGHT_REPORT)
    risk_ts = _risk_timestamp(risk)
    risk_source = _source_record(
        path=RISK_OVERSIGHT_REPORT,
        exists=RISK_OVERSIGHT_REPORT.exists(),
        timestamp=risk_ts,
        now=now,
        error=risk_error,
        critical=True,
    )
    sources["risk_oversight_report"] = risk_source
    risk_level = str(risk.get("level") or risk.get("status") or risk.get("risk_level") or "").upper()
    if _source_is_bad(risk_source):
        _append_incident(
            incidents,
            kind="critical_source_bad",
            source="risk_oversight_report",
            severity="P0",
            timestamp=risk_ts,
            details="risk oversight report missing, stale, or unreadable",
        )
    elif risk_level not in {"", "GREEN"}:
        _append_incident(
            incidents,
            kind="risk_oversight_not_green",
            source="risk_oversight_report",
            severity="P0" if risk_level in {"RED", "CRITICAL"} else "P1",
            timestamp=risk_ts,
            details=f"risk level is {risk_level}",
        )

    watchdog_lines, watchdog_error = _tail_lines(WATCHDOG_LOG)
    watchdog_latest_ts = _latest_ts_from_lines(watchdog_lines)
    watchdog_log_age_hours = None
    if watchdog_latest_ts is not None:
        watchdog_log_age_hours = round((now - watchdog_latest_ts).total_seconds() / 3600.0, 2)
    watchdog_stale = (
        WATCHDOG_LOG.exists()
        and (watchdog_latest_ts is None or now - watchdog_latest_ts > WATCHDOG_LOG_MAX_AGE)
    )
    sources["watchdog_managed_log"] = {
        "path": _rel(WATCHDOG_LOG),
        "exists": WATCHDOG_LOG.exists(),
        "error": watchdog_error,
        "lines_scanned": len(watchdog_lines),
        "latest_event_ts": _iso(watchdog_latest_ts),
        "age_hours": watchdog_log_age_hours,
        "stale": watchdog_stale,
    }
    if watchdog_stale:
        _append_incident(
            incidents,
            kind="watchdog_log_stale",
            source="watchdog_managed_log",
            severity="P0",
            timestamp=watchdog_latest_ts,
            details=(
                "watchdog log has no entries within the freshness window; "
                "tws_cascades count may be incomplete because nothing is observing"
            ),
        )
    tws_count, tws_examples = _count_watchdog_events(watchdog_lines, now, window_days)
    if tws_count:
        _append_incident(
            incidents,
            kind="tws_cascades",
            source="watchdog_managed_log",
            severity="P0",
            details="watchdog observed TWS/Gateway cascade conditions",
            count=tws_count,
        )

    margin_count, margin_examples = _count_margin_alerts(
        risk_report=risk,
        risk_ts=risk_ts,
        watchdog_lines=watchdog_lines,
        now=now,
        window_days=window_days,
    )
    if margin_count:
        _append_incident(
            incidents,
            kind="margin_alerts",
            source="risk_oversight_report/watchdog",
            severity="P0",
            details="margin or liquidation alert evidence in recent window",
            count=margin_count,
        )

    failover_count, failover_examples, failover_error = _count_jsonl_events(CANONICAL_FAILOVER_LOG, now, window_days)
    sources["canonical_fills_failover"] = {
        "path": _rel(CANONICAL_FAILOVER_LOG),
        "exists": CANONICAL_FAILOVER_LOG.exists(),
        "error": failover_error,
        "recent_rows": failover_count,
    }
    if failover_count:
        _append_incident(
            incidents,
            kind="canonical_failover_rows",
            source="canonical_fills_failover",
            severity="P0",
            details="canonical fill failover rows were written in the recent window",
            count=failover_count,
        )

    kill_switch_tested, kill_switch_evidence = _detect_kill_switch_tested(now)
    sources["kill_switch_drill"] = kill_switch_evidence or {
        "exists": False,
        "lookback_days": KILL_SWITCH_DRILL_MAX_AGE.days,
    }

    silent_failures = canonical_drift_count + len(drift_strategies) + failover_count
    if _source_is_bad(canonical_source):
        silent_failures += 1
    if _source_is_bad(reconciliation_source):
        silent_failures += 1

    broker_reconcile_passed = canonical_clean and all_reconciled and broker_reconcile_clean
    open_p0 = sum(1 for incident in incidents if incident.get("severity") == "P0")

    # Bug #1 fix (2026-05-13 audit): clean_current decouples from the 30-day
    # rolling counts. Previously `tws_count == 0` (which is 30-day rolling)
    # meant the streak couldn't start until every historical cascade aged
    # out — pushing SMOKE_5K eligibility ~8 days later than necessary.
    # Stage-promotion gates (max_tws_cascades_30d, etc.) still use the
    # 30-day count; only the daily streak math uses today's count.
    tws_today_count, _ = _count_watchdog_events(watchdog_lines, now, window_days=1)
    margin_today_count, _ = _count_margin_alerts(
        risk_report=risk,
        risk_ts=risk_ts,
        watchdog_lines=watchdog_lines,
        now=now,
        window_days=1,
    )
    phantom_today_count, _, _ = _count_phantom_rows(
        phantom_rows, now, window_days=1, resolutions=resolution_rows,
    )
    failover_today_count, _, _ = _count_jsonl_events(CANONICAL_FAILOVER_LOG, now, window_days=1)
    silent_failures_today = canonical_drift_count + len(drift_strategies) + failover_today_count
    if _source_is_bad(canonical_source):
        silent_failures_today += 1
    if _source_is_bad(reconciliation_source):
        silent_failures_today += 1

    # Open P0 incidents whose kind is a 30-day-rolling count don't reflect
    # CURRENT state — they reflect historical evidence still inside the
    # rolling window. Exclude them from clean_current so the streak math
    # measures "is today operationally clean?" not "has the window ever
    # been dirty?". Stage gates still see the full open_p0 count.
    _ROLLING_30D_KINDS = {
        "tws_cascades", "margin_alerts", "phantom_fills", "canonical_failover_rows",
    }
    open_p0_today = sum(
        1 for incident in incidents
        if incident.get("severity") == "P0"
        and incident.get("kind") not in _ROLLING_30D_KINDS
    )

    clean_current = (
        silent_failures_today == 0
        and phantom_today_count == 0
        and tws_today_count == 0
        and margin_today_count == 0
        and broker_reconcile_passed
        and open_p0_today == 0
    )

    current_snapshot = {
        "generated_at": now.isoformat(),
        "clean_current": clean_current,
        "broker_reconcile_passed": broker_reconcile_passed,
    }

    report = {
        "generated_at": now.isoformat(),
        "window_days": window_days,
        "clean_ops_days": _consecutive_true_days(history, "clean_current", current_snapshot, now),
        "silent_failures_30d": silent_failures,
        "phantom_fills_30d": phantom_count,
        "historical_resolved_phantom_fills_30d": resolved_phantom_count,
        "tws_cascades_30d": tws_count,
        "margin_alerts_30d": margin_count,
        "broker_reconcile_passed_days": _consecutive_true_days(history, "broker_reconcile_passed", current_snapshot, now),
        "kill_switch_tested": kill_switch_tested,
        "open_p0_incidents": open_p0,
        "clean_current": clean_current,
        "broker_reconcile_passed": broker_reconcile_passed,
        "examples": {
            "phantom_fills": phantom_examples,
            "tws_cascades": tws_examples,
            "margin_alerts": margin_examples,
            "canonical_failover": failover_examples,
        },
        "incidents": incidents,
        "sources": sources,
    }
    return report


def _print_report(report: dict[str, Any]) -> None:
    print(
        "Ops reliability -- "
        f"clean_ops_days={report['clean_ops_days']} "
        f"broker_reconcile_passed_days={report['broker_reconcile_passed_days']} "
        f"open_p0_incidents={report['open_p0_incidents']}"
    )
    print(
        "Window: "
        f"{report['window_days']}d | "
        f"silent={report['silent_failures_30d']} "
        f"phantom={report['phantom_fills_30d']} "
        f"tws={report['tws_cascades_30d']} "
        f"margin={report['margin_alerts_30d']} "
        f"kill_switch_tested={report['kill_switch_tested']}"
    )
    for incident in report.get("incidents", [])[:8]:
        count = f" x{incident['count']}" if incident.get("count") is not None else ""
        details = f" -- {incident['details']}" if incident.get("details") else ""
        print(f"  - {incident['severity']} {incident['kind']}{count} [{incident['source']}]{details}")
    extra = len(report.get("incidents", [])) - 8
    if extra > 0:
        print(f"  - +{extra} more incidents")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print JSON report")
    parser.add_argument("--output", default=str(OUTPUT_PATH), help="Where to write report JSON")
    parser.add_argument("--history", default=str(HISTORY_PATH), help="JSONL history path for streaks")
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS, help="Recent evidence window")
    parser.add_argument("--no-history-append", action="store_true", help="Do not append this run to history")
    args = parser.parse_args(argv)

    report = build_report(window_days=args.window_days, history_path=args.history)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    if not args.no_history_append:
        _append_history(Path(args.history), report)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_report(report)
        print(f"Wrote: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
