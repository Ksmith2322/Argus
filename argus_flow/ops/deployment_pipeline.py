"""Managed staged deployment registry.

Builds the authoritative deployment_registry.json used by the dashboard,
launchers, and runtime surfaces.

Stages:
- discovery (future)
- watcher (observe only)
- paper/QA
- real/PROD
- quarantine
- killed
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from argus_flow.ops.demotion_check import check_demotion, recommend_risk_pct, resolve_quarantine
from argus_flow.ops.fleet_registry import (
    CONFIGS_DIR,
    DEPLOYMENT_REGISTRY_FILE,
    RISK_POLICY_DEFAULTS,
    STAGE_DISCOVERY,
    STAGE_KILLED,
    STAGE_ORDER,
    STAGE_PAPER,
    STAGE_QUARANTINE,
    STAGE_REAL,
    STAGE_WATCHER,
    discover_managed_runners,
    is_launchable_stage,
    is_real_money_stage,
    normalize_stage,
    stage_account,
    stage_display_label,
    stage_execution_mode,
    stage_rank,
)
from argus_flow.ops.generate_live_config import generate_live_config

REPO = Path(__file__).resolve().parents[2]
LOGS_DIR = REPO / "argus_flow" / "logs"
PROMOTION_GATE_FILE = LOGS_DIR / "promotion_gate_report.json"
KILL_DISCIPLINE_FILE = LOGS_DIR / "kill_discipline_report.json"
DIVERGENCE_FILE = LOGS_DIR / "divergence_report.json"
GOVERNANCE_MAX_AGE_S = 30 * 60

WATCHER_MIN_DAYS = 7
WATCHER_MIN_SIGNALS = 50
WATCHER_MIN_SIGNALS_PER_DAY = 1.0
WATCHER_DATA_MAX_AGE_DAYS = 7.0
QA_MIN_DAYS = 14


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _report_age_s(path: Path, report: dict | None) -> float | None:
    ts = _parse_ts(report.get("timestamp")) if isinstance(report, dict) else None
    now = datetime.now(timezone.utc)
    if ts is not None:
        return max(0.0, (now - ts).total_seconds())
    if not path.exists():
        return None
    try:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None
    return max(0.0, (now - modified).total_seconds())


def _report_status(label: str, path: Path, report: dict | None, fresh_s: int = GOVERNANCE_MAX_AGE_S) -> dict:
    age_s = _report_age_s(path, report)
    exists = path.exists()
    status = "FRESH"
    if report is None and not exists:
        status = "MISSING"
    elif age_s is None or age_s > fresh_s:
        status = "STALE"
    return {
        "label": label,
        "path": str(path.relative_to(REPO)),
        "timestamp": report.get("timestamp") if isinstance(report, dict) else "",
        "age_s": None if age_s is None else int(age_s),
        "fresh_s": fresh_s,
        "status": status,
        "fresh": status == "FRESH",
    }


def _load_previous_registry() -> tuple[dict[str, str], dict[str, dict]]:
    report = _load_json(DEPLOYMENT_REGISTRY_FILE)
    if not isinstance(report, dict):
        return {}, {}
    stage_map: dict[str, str] = {}
    entry_map: dict[str, dict] = {}
    for entry in report.get("runners", []):
        if not isinstance(entry, dict):
            continue
        config_file = str(entry.get("config_file", "") or "")
        stage = normalize_stage(entry.get("current_stage"))
        if config_file:
            entry_map[config_file] = entry
        if config_file and stage:
            stage_map[config_file] = stage
    return stage_map, entry_map


def _find_report_entry(report: dict | None, symbol: str) -> dict | None:
    if not isinstance(report, dict):
        return None
    for entry in report.get("runners", []):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("symbol", "")).upper() == symbol.upper():
            return entry
    return None


def _parse_ts(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_signals(log_dir: str) -> list[dict]:
    path = REPO / log_dir / "signals.csv"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _load_trades(log_dir: str) -> list[dict]:
    path = REPO / log_dir / "trades.csv"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _valid_trades(rows: list[dict]) -> list[dict]:
    return [row for row in rows if str(row.get("experiment_valid", "")).lower() == "true"]


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pnl_field(rows: list[dict]) -> str:
    if rows and "pnl_pips" in rows[0]:
        return "pnl_pips"
    return "pnl_pts"


def _max_drawdown(pnls: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return round(max_dd, 4)


def _profit_factor(pnls: list[float]) -> float:
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    if gross_loss < 1e-9:
        return float("inf") if gross_win > 0 else 0.0
    return round(gross_win / gross_loss, 4)


def _model_drawdown(config_path: Path) -> float:
    cfg = _load_json(config_path)
    if not isinstance(cfg, dict):
        return 10.0
    risk = cfg.get("risk", {}) if isinstance(cfg.get("risk"), dict) else {}
    stop_pips = _safe_float(risk.get("stop_pips", 0))
    if stop_pips > 0:
        return stop_pips * 3
    stop_bps = _safe_float(risk.get("stop_bps", 0))
    if stop_bps > 0:
        return stop_bps * 3
    return 10.0


def _load_walkforward_status(log_dir: str) -> tuple[str, str, dict]:
    report = _load_json(REPO / log_dir / "walkforward_report.json")
    if not isinstance(report, dict):
        return "MISSING", "walkforward_report.json missing", {}
    summary = report.get("summary", {}) if isinstance(report.get("summary", {}), dict) else {}
    status = str(report.get("status", summary.get("status", "UNKNOWN"))).upper()
    rationale = str(summary.get("rationale", "") or "")
    return status, rationale or status, report


def _signal_metrics(log_dir: str, stage_entered_at: str, now: datetime) -> dict:
    stage_start = _parse_ts(stage_entered_at) or now
    rows = _load_signals(log_dir)
    entries = []
    for row in rows:
        ts = _parse_ts(row.get("ts"))
        if ts is None or ts < stage_start:
            continue
        if str(row.get("action", "")).upper() == "ENTRY":
            entries.append(row)
    residency_days = max((now - stage_start).total_seconds() / 86400.0, 0.0)
    return {
        "stage_entered_at": stage_start.isoformat(),
        "residency_days": round(residency_days, 2),
        "observed_signals": len(entries),
        "signals_per_day": round(len(entries) / max(residency_days, 1.0), 2),
    }


def _data_freshness_days(wf_report: dict) -> float | None:
    data_path_str = str(wf_report.get("data_path", "") or "").strip()
    if not data_path_str:
        return None
    data_path = Path(data_path_str)
    if not data_path.is_absolute():
        data_path = (REPO / data_path).resolve()
    if not data_path.exists():
        return None
    age_seconds = (datetime.now(timezone.utc) - datetime.fromtimestamp(data_path.stat().st_mtime, tz=timezone.utc)).total_seconds()
    return round(age_seconds / 86400.0, 2)


def _trade_metrics(runner: dict) -> dict:
    trades = _valid_trades(_load_trades(runner["log_dir"]))
    if not trades:
        return {
            "valid_trades": 0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "model_drawdown": _model_drawdown(REPO / runner["config_path"]),
        }
    pnl_field = _pnl_field(trades)
    pnls = [_safe_float(row.get(pnl_field, 0.0)) for row in trades]
    return {
        "valid_trades": len(trades),
        "profit_factor": _profit_factor(pnls),
        "max_drawdown": _max_drawdown(pnls),
        "model_drawdown": _model_drawdown(REPO / runner["config_path"]),
    }


def _scale_state(active_risk: float, stage: str, base_risk: float) -> str:
    if stage in (STAGE_DISCOVERY, STAGE_WATCHER, STAGE_KILLED):
        return "OBSERVE"
    if stage == STAGE_QUARANTINE:
        return "QUARANTINE"
    if active_risk <= 0 or active_risk <= base_risk + 1e-9:
        return "BASE"
    if active_risk <= 0.0075 + 1e-9:
        return "STEP_30"
    if active_risk <= 0.01 + 1e-9:
        return "STEP_60"
    if active_risk <= 0.015 + 1e-9:
        return "STEP_100"
    if active_risk <= 0.02 + 1e-9:
        return "STEP_200"
    if active_risk <= 0.03 + 1e-9:
        return "STEP_300"
    return "CUSTOM"


def _apply_shared_risk_progression(risk_policy: dict, runner: dict, stage: str) -> None:
    base_risk = float(risk_policy.get("base_risk_pct", RISK_POLICY_DEFAULTS["base_risk_pct"]) or RISK_POLICY_DEFAULTS["base_risk_pct"])
    if stage in (STAGE_DISCOVERY, STAGE_WATCHER, STAGE_KILLED):
        risk_policy["active_risk_pct"] = 0.0
        risk_policy["scale_state"] = _scale_state(0.0, stage, base_risk)
        return
    if stage == STAGE_QUARANTINE:
        risk_policy["active_risk_pct"] = 0.0025
        risk_policy["scale_state"] = _scale_state(0.0025, stage, base_risk)
        return

    metrics = _trade_metrics(runner)
    active = recommend_risk_pct(
        int(metrics.get("valid_trades", 0) or 0),
        float(metrics.get("profit_factor", 0.0) or 0.0),
        float(metrics.get("max_drawdown", 0.0) or 0.0),
        float(metrics.get("model_drawdown", 10.0) or 10.0),
        is_quarantined=False,
    )
    risk_policy["active_risk_pct"] = float(active)
    risk_policy["scale_state"] = _scale_state(float(active), stage, base_risk)


def _watcher_governance(symbol: str, kill_report: dict | None, divergence_report: dict | None) -> tuple[str, list[str]]:
    statuses: list[str] = []
    reasons: list[str] = []
    for report in (divergence_report, kill_report):
        entry = _find_report_entry(report, symbol)
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status", entry.get("verdict", "")) or "").upper()
        if status:
            statuses.append(status)
        reason = str(entry.get("reason", "") or "")
        if reason:
            reasons.append(reason)
    if "KILL" in statuses:
        return "KILL", reasons
    if "WATCH" in statuses:
        return "WATCH", reasons
    if "PASS" in statuses:
        return "PASS", reasons
    return "COLLECTING", reasons


def _sticky_stage(discovered_stage: str, previous_stage: str | None) -> str:
    prev = normalize_stage(previous_stage)
    if prev and stage_rank(prev) > stage_rank(discovered_stage):
        return prev
    return discovered_stage


def build_registry(auto_materialize_live: bool = True) -> dict:
    now = datetime.now(timezone.utc)
    previous_stage_map, previous_entry_map = _load_previous_registry()
    promotion_gate = _load_json(PROMOTION_GATE_FILE)
    kill_report = _load_json(KILL_DISCIPLINE_FILE)
    divergence_report = _load_json(DIVERGENCE_FILE)
    governance_inputs = {
        "promotion_gate": _report_status("Promotion Gate", PROMOTION_GATE_FILE, promotion_gate),
        "kill_discipline": _report_status("Kill Discipline", KILL_DISCIPLINE_FILE, kill_report),
        "divergence_guard": _report_status("Divergence Guard", DIVERGENCE_FILE, divergence_report),
    }
    promotion_gate_fresh = bool(governance_inputs["promotion_gate"]["fresh"])
    kill_report_fresh = bool(governance_inputs["kill_discipline"]["fresh"])
    divergence_report_fresh = bool(governance_inputs["divergence_guard"]["fresh"])
    runners = discover_managed_runners()
    registry_runners: list[dict] = []

    for runner in runners:
        config_file = runner["config_file"]
        current_stage = _sticky_stage(runner["current_stage"], previous_stage_map.get(config_file))
        previous = previous_entry_map.get(config_file, {})
        previous_stage = normalize_stage(previous.get("current_stage"))
        previous_entered_at = str(previous.get("stage_entered_at", "") or "")
        stage_entered_at = previous_entered_at if previous_stage == current_stage and previous_entered_at else now.isoformat()
        risk_policy = dict(runner.get("risk_policy", {}))
        risk_policy.setdefault("base_risk_pct", RISK_POLICY_DEFAULTS["base_risk_pct"])
        risk_policy.setdefault("active_risk_pct", risk_policy["base_risk_pct"])
        risk_policy.setdefault("earned_cap_pct", RISK_POLICY_DEFAULTS["earned_cap_pct"])
        risk_policy["manual_step_up_required"] = False

        next_stage = ""
        transition_ready = False
        transition_reason = ""
        live_config_file = runner.get("live_counterpart_config_file", "")
        promotion_verdict = ""
        demotion_verdict = ""
        governance_status = ""
        wf_status, wf_detail, wf_report = _load_walkforward_status(runner["log_dir"])
        signal_metrics = _signal_metrics(runner["log_dir"], stage_entered_at, now)

        if current_stage == STAGE_WATCHER:
            next_stage = STAGE_PAPER
            governance_reasons: list[str] = []
            if kill_report_fresh and divergence_report_fresh:
                governance_status, governance_reasons = _watcher_governance(runner["symbol"], kill_report, divergence_report)
            else:
                governance_status = "STALE"
                if not divergence_report_fresh:
                    governance_reasons.append("divergence guard stale")
                if not kill_report_fresh:
                    governance_reasons.append("kill discipline stale")
            data_age_days = _data_freshness_days(wf_report)
            watcher_blockers: list[str] = []
            if wf_status != "PASS":
                watcher_blockers.append(f"walk-forward {wf_status}: {wf_detail}")
            if signal_metrics["residency_days"] < WATCHER_MIN_DAYS:
                watcher_blockers.append(f"{signal_metrics['residency_days']:.1f}/{WATCHER_MIN_DAYS} watcher days")
            if signal_metrics["observed_signals"] < WATCHER_MIN_SIGNALS:
                watcher_blockers.append(f"{signal_metrics['observed_signals']}/{WATCHER_MIN_SIGNALS} observed signals")
            if signal_metrics["signals_per_day"] < WATCHER_MIN_SIGNALS_PER_DAY:
                watcher_blockers.append(f"{signal_metrics['signals_per_day']:.1f}/day signals")
            if data_age_days is None:
                watcher_blockers.append("historical data missing")
            elif data_age_days > WATCHER_DATA_MAX_AGE_DAYS:
                watcher_blockers.append(f"historical data {data_age_days:.1f}d old")
            if governance_status == "KILL":
                watcher_blockers.append("active governance KILL flag")
            elif governance_status == "STALE":
                watcher_blockers.extend(governance_reasons)

            if wf_status == "FAIL":
                current_stage = STAGE_KILLED
                next_stage = ""
                stage_entered_at = now.isoformat()
                transition_reason = f"Watcher retired after walk-forward FAIL ({wf_detail})"
            elif not watcher_blockers:
                transition_ready = True
                current_stage = STAGE_PAPER
                stage_entered_at = now.isoformat()
                transition_reason = "Watcher earned QA promotion"
            else:
                if governance_reasons and governance_status == "WATCH":
                    watcher_blockers.append(governance_reasons[0])
                transition_reason = "Watcher remains observe-only: " + "; ".join(watcher_blockers[:5])

        elif current_stage == STAGE_PAPER and not runner.get("live", False):
            next_stage = STAGE_REAL
            kill_entry = _find_report_entry(kill_report, runner["symbol"]) if kill_report_fresh else None
            kill_status = str((kill_entry or {}).get("status", (kill_entry or {}).get("verdict", "")) or "").upper()
            promotion_entry = _find_report_entry(promotion_gate, runner["symbol"]) if promotion_gate_fresh else None
            if promotion_gate_fresh and promotion_entry:
                promotion_verdict = str(promotion_entry.get("verdict", "") or "")
            elif not promotion_gate_fresh:
                promotion_verdict = "STALE"
            if kill_report_fresh and kill_status == "KILL":
                current_stage = STAGE_KILLED
                stage_entered_at = now.isoformat()
                transition_reason = f"QA killed by kill discipline: {kill_entry.get('reason', 'KILL flag active') if kill_entry else 'KILL flag active'}"
            elif wf_status == "FAIL":
                current_stage = STAGE_WATCHER
                stage_entered_at = now.isoformat()
                transition_reason = f"QA regressed to watcher after walk-forward FAIL ({wf_detail})"
            else:
                _apply_shared_risk_progression(risk_policy, runner, current_stage)
            if current_stage == STAGE_PAPER and not kill_report_fresh:
                transition_reason = "Paper lane waiting for fresh kill discipline report"
            elif current_stage == STAGE_PAPER and not promotion_gate_fresh:
                transition_reason = "Paper lane waiting for fresh promotion gate report"
            elif current_stage == STAGE_PAPER and promotion_verdict == "PROMOTE" and signal_metrics["residency_days"] >= QA_MIN_DAYS:
                transition_ready = True
                transition_reason = "QA lane earned prod promotion"
                live_path = CONFIGS_DIR / f"{runner['symbol'].lower()}_live_v1.json"
                if auto_materialize_live and not live_path.exists():
                    generated = generate_live_config(
                        REPO / runner["config_path"],
                        force=False,
                        risk_pct=risk_policy["base_risk_pct"],
                    )
                    if generated is not None:
                        live_path = generated
                if live_path.exists():
                    live_config_file = live_path.name
            elif current_stage == STAGE_PAPER and promotion_verdict == "PROMOTE":
                transition_reason = f"QA gate passed but residency is {signal_metrics['residency_days']:.1f}/{QA_MIN_DAYS} days"
            elif current_stage == STAGE_PAPER and promotion_verdict:
                transition_reason = f"Paper promotion gate: {promotion_verdict}"
            elif current_stage == STAGE_PAPER:
                transition_reason = "Paper lane waiting for promotion gate evidence"

        elif current_stage == STAGE_REAL:
            demotion_result = check_demotion({**runner, "current_stage": current_stage})
            demotion_verdict = str(demotion_result.get("verdict", "") or "")
            if demotion_verdict == "KILL":
                current_stage = STAGE_KILLED
                stage_entered_at = now.isoformat()
                transition_reason = "Prod retired: " + "; ".join(demotion_result.get("reasons", [])[:3])
            elif demotion_verdict == "QUARANTINE":
                current_stage = STAGE_QUARANTINE
                stage_entered_at = now.isoformat()
                transition_reason = "Prod quarantined: " + "; ".join(demotion_result.get("reasons", [])[:3])
            else:
                next_stage = "scale"
                _apply_shared_risk_progression(risk_policy, runner, current_stage)
                transition_reason = "Prod active under shared earned-risk progression"

        elif current_stage == STAGE_QUARANTINE:
            quarantine_result = resolve_quarantine(
                {
                    **runner,
                    "current_stage": current_stage,
                    "quarantine_start_ts": previous.get("quarantine_start_ts", previous.get("stage_entered_at", stage_entered_at)),
                }
            )
            demotion_verdict = str(quarantine_result.get("verdict", "") or "")
            if demotion_verdict == "REINSTATE":
                current_stage = STAGE_REAL
                stage_entered_at = now.isoformat()
                risk_policy["active_risk_pct"] = float(risk_policy.get("base_risk_pct", RISK_POLICY_DEFAULTS["base_risk_pct"]) or RISK_POLICY_DEFAULTS["base_risk_pct"])
                risk_policy["scale_state"] = "BASE"
                transition_reason = "Quarantine resolved: reinstated to prod at base risk"
            elif demotion_verdict == "DEMOTE_TO_QA":
                current_stage = STAGE_PAPER
                stage_entered_at = now.isoformat()
                risk_policy["active_risk_pct"] = float(risk_policy.get("base_risk_pct", RISK_POLICY_DEFAULTS["base_risk_pct"]) or RISK_POLICY_DEFAULTS["base_risk_pct"])
                risk_policy["scale_state"] = "BASE"
                transition_reason = "Quarantine resolved: demoted to QA, fresh cohort required to re-earn prod"
            elif demotion_verdict == "KILL":
                current_stage = STAGE_KILLED
                stage_entered_at = now.isoformat()
                transition_reason = "Quarantine timed out or breached model limits"
            else:
                _apply_shared_risk_progression(risk_policy, runner, current_stage)
                transition_reason = "Quarantine holding at reduced risk"

        else:
            risk_policy["active_risk_pct"] = 0.0
            risk_policy["scale_state"] = "OBSERVE"
            transition_reason = "Non-launchable stage"

        if current_stage not in (STAGE_PAPER, STAGE_REAL, STAGE_QUARANTINE):
            risk_policy["active_risk_pct"] = 0.0
            risk_policy["scale_state"] = _scale_state(
                0.0,
                current_stage,
                float(risk_policy.get("base_risk_pct", RISK_POLICY_DEFAULTS["base_risk_pct"]) or RISK_POLICY_DEFAULTS["base_risk_pct"]),
            )
        if current_stage != previous_stage:
            signal_metrics = _signal_metrics(runner["log_dir"], stage_entered_at, now)

        registry_runners.append(
            {
                **runner,
                "current_stage": current_stage,
                "previous_stage": previous_stage,
                "next_stage": next_stage,
                "transition_ready": transition_ready,
                "transition_reason": transition_reason,
                "promotion_verdict": promotion_verdict,
                "demotion_verdict": demotion_verdict,
                "governance_status": governance_status,
                "risk_policy": risk_policy,
                "live_counterpart_config_file": live_config_file,
                "stage_account": stage_account(current_stage),
                "execution_mode": stage_execution_mode(current_stage),
                "strategy_status": stage_display_label(current_stage),
                "launch_enabled": is_launchable_stage(current_stage),
                "stage_entered_at": stage_entered_at,
                "residency_days": signal_metrics["residency_days"],
                "observed_signals": signal_metrics["observed_signals"],
                "signals_per_day": signal_metrics["signals_per_day"],
                "quarantine_start_ts": stage_entered_at if current_stage == STAGE_QUARANTINE else "",
            }
        )

    # Re-discover after any auto-generated live configs so launcher sets stay current.
    discovered_again = {runner["config_file"]: runner for runner in discover_managed_runners()}
    for entry in registry_runners:
        live_config_file = entry.get("live_counterpart_config_file", "")
        if not live_config_file:
            maybe_live = discovered_again.get(f"{entry['symbol'].lower()}_live_v1.json")
            if maybe_live:
                entry["live_counterpart_config_file"] = maybe_live["config_file"]

    stage_counts = {
        STAGE_DISCOVERY: 0,
        STAGE_WATCHER: 0,
        STAGE_PAPER: 0,
        STAGE_REAL: 0,
        STAGE_QUARANTINE: 0,
        STAGE_KILLED: 0,
    }
    for entry in registry_runners:
        stage_counts[entry["current_stage"]] = stage_counts.get(entry["current_stage"], 0) + 1

    launcher = {
        "watcher_configs": [
            runner["config_path"]
            for runner in registry_runners
            if runner["current_stage"] == STAGE_WATCHER and runner["launch_enabled"]
        ],
        "paper_configs": [
            runner["config_path"]
            for runner in registry_runners
            if runner["current_stage"] in (STAGE_WATCHER, STAGE_PAPER) and not runner["live"] and runner["launch_enabled"]
        ],
        "qa_configs": [
            runner["config_path"]
            for runner in registry_runners
            if runner["current_stage"] == STAGE_PAPER and not runner["live"] and runner["launch_enabled"]
        ],
        "real_configs": [
            runner["config_path"]
            for runner in registry_runners
            if is_real_money_stage(runner["current_stage"]) and runner["live"] and runner["launch_enabled"]
        ],
    }

    report = {
        "timestamp": now.isoformat(),
        "inputs": governance_inputs,
        "risk_policy": dict(RISK_POLICY_DEFAULTS),
        "summary": {
            "discovery": stage_counts.get(STAGE_DISCOVERY, 0),
            "watcher": stage_counts.get(STAGE_WATCHER, 0),
            "paper": stage_counts.get(STAGE_PAPER, 0),
            "real": len(launcher["real_configs"]),
            "quarantine": stage_counts.get(STAGE_QUARANTINE, 0),
            "killed": stage_counts.get(STAGE_KILLED, 0),
            "ready_for_paper": sum(1 for runner in registry_runners if runner["current_stage"] == STAGE_PAPER and runner["previous_stage"] == STAGE_WATCHER),
            "ready_for_real": sum(1 for runner in registry_runners if runner["transition_ready"] and runner["next_stage"] == STAGE_REAL),
            "governance_ready": all(item["fresh"] for item in governance_inputs.values()),
            "stale_inputs": [name for name, item in governance_inputs.items() if not item["fresh"]],
        },
        "launcher": launcher,
        "runners": sorted(
            registry_runners,
            key=lambda item: (STAGE_ORDER.get(item["current_stage"], 99), item["name"], item.get("live", False)),
        ),
    }
    DEPLOYMENT_REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEPLOYMENT_REGISTRY_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def _emit_configs(report: dict, stages: list[str]) -> int:
    requested = {normalize_stage(stage) for stage in stages}
    launcher = report.get("launcher", {}) if isinstance(report.get("launcher", {}), dict) else {}
    if requested == {STAGE_REAL}:
        configs = launcher.get("real_configs", [])
    elif requested == {STAGE_WATCHER}:
        configs = launcher.get("watcher_configs", [])
    elif requested == {STAGE_PAPER}:
        configs = launcher.get("qa_configs", [])
    else:
        configs = launcher.get("paper_configs", [])
    for path in configs:
        print(path)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Argus deployment registry")
    parser.add_argument(
        "--emit-configs",
        type=str,
        default="",
        help="Print config paths for the requested stages, e.g. watcher,paper or real",
    )
    parser.add_argument(
        "--no-auto-live",
        action="store_true",
        help="Do not auto-materialize live configs when a paper runner reaches PROMOTE",
    )
    args = parser.parse_args()

    report = build_registry(auto_materialize_live=not args.no_auto_live)
    if args.emit_configs:
        stages = [part.strip() for part in args.emit_configs.split(",") if part.strip()]
        raise SystemExit(_emit_configs(report, stages))

    summary = report.get("summary", {})
    print("=" * 72)
    print(f"  Deployment Registry - {report['timestamp']}")
    print("=" * 72)
    print(
        f"  Watcher={summary.get('watcher', 0)}  "
        f"Paper={summary.get('paper', 0)}  "
        f"Real={summary.get('real', 0)}  "
        f"Quarantine={summary.get('quarantine', 0)}  "
        f"Killed={summary.get('killed', 0)}  "
        f"ReadyForReal={summary.get('ready_for_real', 0)}"
    )
    stale_inputs = summary.get("stale_inputs", [])
    if stale_inputs:
        print(f"  Governance=STALE ({', '.join(stale_inputs)})")
    else:
        print("  Governance=FRESH")
    print(f"  Saved: {DEPLOYMENT_REGISTRY_FILE}")


if __name__ == "__main__":
    main()
