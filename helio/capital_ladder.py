"""Capital ladder gate for moving Argus from proof engine to capital vehicle.

The ladder answers one question mechanically:

    "How much real capital has the bot earned the right to control today?"

It does not try to discover alpha. Other modules do that. This module
combines their outputs with operations evidence and returns the highest
capital stage whose gates are all satisfied. Missing evidence is a blocker,
not an invitation to assume the best.

Primary entry points:

    python -m helio.capital_ladder
    python -m helio.capital_ladder --json
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]

CONFIG_PATH = REPO / "argus_flow" / "configs" / "capital_ladder.json"
MANUAL_EVIDENCE_PATH = REPO / "argus_flow" / "configs" / "capital_ladder_evidence.json"
ROI_PROOF_CSV = REPO / "ops" / "reports" / "system_audit" / "strategy_roi_proof.csv"
OPS_RELIABILITY_REPORT = REPO / "argus_flow" / "logs" / "ops_reliability_report.json"
FLEET_CORRELATION_REPORT = REPO / "argus_flow" / "logs" / "fleet_correlation_report.json"
OUTPUT_PATH = REPO / "argus_flow" / "logs" / "capital_ladder_report.json"


DEFAULT_CONFIG: dict[str, Any] = {
    "version": "fallback",
    "total_capital_reference_usd": 100_000,
    "strategy_clusters": {},
    "stages": [
        {
            "name": "RESEARCH_FREEZE",
            "approved_capital_usd": 0,
            "description": "No live capital approved; collect evidence only.",
            "criteria": {},
        }
    ],
}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    """Load the capital ladder policy. Missing config falls back to a single
    zero-capital stage so callers fail closed."""
    p = Path(path) if path is not None else CONFIG_PATH
    data = _read_json(p)
    if not data:
        return DEFAULT_CONFIG
    if "stages" not in data:
        data["stages"] = DEFAULT_CONFIG["stages"]
    if "strategy_clusters" not in data:
        data["strategy_clusters"] = {}
    return data


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    if text.lower() == "inf":
        return float("inf")
    if text.startswith("INSUFFICIENT_SAMPLE"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _safe_int(value: Any) -> int | None:
    f = _safe_float(value)
    if f is None:
        return None
    return int(f)


def _safe_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def _load_roi_strategy_evidence(path: Path) -> dict[str, dict[str, Any]]:
    """Read strategy evidence from the ROI proof CSV if present."""
    if not path.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    try:
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                strategy = str(row.get("strategy") or "").strip()
                if not strategy:
                    continue
                n = _safe_int(row.get("n")) or 0
                live_days = _safe_float(row.get("days_observed"))
                live_ir = _safe_float(row.get("information_ratio"))
                live_ir_low_sample = _safe_float(row.get("information_ratio_low_sample"))
                paper_ir = _safe_float(row.get("paper_information_ratio"))
                paper_live_delta = _safe_float(row.get("paper_live_ir_delta_abs"))
                avg_notional = _safe_float(row.get("avg_notional_usd"))
                dd_usd = _safe_float(row.get("max_drawdown_usd"))
                dd_pct = _safe_float(row.get("max_drawdown_pct"))
                if dd_pct is None and avg_notional and avg_notional > 0 and dd_usd is not None:
                    dd_pct = abs(dd_usd) / avg_notional * 100.0
                out[strategy] = {
                    "strategy": strategy,
                    "real_fill_trades": n,
                    "live_days": live_days,
                    "live_ir": live_ir,
                    "live_ir_low_sample": live_ir_low_sample,
                    "paper_ir": paper_ir,
                    "paper_live_ir_delta_abs": paper_live_delta,
                    "profit_factor": _safe_float(row.get("profit_factor")),
                    "spy_coverage_pct": _safe_float(row.get("spy_coverage_pct")),
                    "max_drawdown_pct": dd_pct,
                    "max_drawdown_usd": dd_usd,
                    "capacity_test_usd": _safe_float(row.get("capacity_test_usd")),
                    "verdict": row.get("verdict") or "",
                    "source": "strategy_roi_proof.csv",
                }
    except OSError:
        return {}
    return out


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def build_current_evidence(
    *,
    config: dict[str, Any] | None = None,
    roi_csv_path: Path | str | None = None,
    manual_evidence_path: Path | str | None = None,
    ops_report_path: Path | str | None = None,
    correlation_report_path: Path | str | None = None,
) -> dict[str, Any]:
    """Build current ladder evidence from available reports plus optional
    manual overrides.

    The optional manual evidence file is intentionally separate from runtime
    logs because some gates are operator-attested: capacity tests, paper/live
    divergence, kill-switch drills, and broker reconciliation streaks.
    """
    cfg = config or load_config()
    roi_path = Path(roi_csv_path) if roi_csv_path is not None else ROI_PROOF_CSV
    manual_path = Path(manual_evidence_path) if manual_evidence_path is not None else MANUAL_EVIDENCE_PATH
    ops_path = Path(ops_report_path) if ops_report_path is not None else OPS_RELIABILITY_REPORT
    corr_path = Path(correlation_report_path) if correlation_report_path is not None else FLEET_CORRELATION_REPORT

    strategy_clusters = cfg.get("strategy_clusters") or {}
    strategies = _load_roi_strategy_evidence(roi_path)
    for name, cluster in strategy_clusters.items():
        if name in strategies:
            strategies[name]["cluster"] = cluster

    manual = _read_json(manual_path)
    for name, row in (manual.get("strategies") or {}).items():
        existing = strategies.get(name, {"strategy": name})
        merged = _deep_merge(existing, row if isinstance(row, dict) else {})
        if "cluster" not in merged and name in strategy_clusters:
            merged["cluster"] = strategy_clusters[name]
        strategies[name] = merged

    ops = _read_json(ops_path)
    ops = _deep_merge(ops, manual.get("ops") or {})

    portfolio = dict(manual.get("portfolio") or {})
    corr = _read_json(corr_path)
    diversification = dict(manual.get("diversification") or {})
    if corr:
        high_pairs = corr.get("high_correlation_pairs") or []
        max_corr = None
        for pair in high_pairs:
            val = _safe_float(pair.get("correlation"))
            if val is not None:
                max_corr = max(abs(val), max_corr or 0.0)
        summary = corr.get("summary") or {}
        diversification = _deep_merge({
            "effective_independent_bets": summary.get("effective_independent_bets"),
            "max_pairwise_corr_abs": max_corr,
            "source": "fleet_correlation_report.json",
        }, diversification)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config_version": cfg.get("version", ""),
        "strategies": strategies,
        "ops": ops,
        "portfolio": portfolio,
        "diversification": diversification,
        "sources": {
            "roi_csv": str(roi_path),
            "manual_evidence": str(manual_path),
            "ops_report": str(ops_path),
            "correlation_report": str(corr_path),
        },
    }


def _metric_at_least(blockers: list[str], label: str, actual: Any, required: float) -> None:
    val = _safe_float(actual)
    if val is None:
        blockers.append(f"missing {label}; need >= {required}")
    elif val < required:
        blockers.append(f"{label} {val:g} < {required:g}")


def _metric_at_most(blockers: list[str], label: str, actual: Any, required: float) -> None:
    val = _safe_float(actual)
    if val is None:
        blockers.append(f"missing {label}; need <= {required}")
    elif val > required:
        blockers.append(f"{label} {val:g} > {required:g}")


def _bool_required(blockers: list[str], label: str, actual: Any) -> None:
    val = _safe_bool(actual)
    if val is not True:
        blockers.append(f"{label} is not true")


def _effective_live_ir(strategy: dict[str, Any], criteria: dict[str, Any]) -> Any:
    """Return the IR value the stage should gate on.

    Default: strict ``live_ir`` (n >= CONTINUATION_N). Smoke-only stages
    can set ``allow_low_sample_ir: true`` to fall back to
    ``live_ir_low_sample`` (n >= SMOKE_N) when the strict IR is absent.
    The low-sample IR must never be silently accepted at LIVE_10K and
    above — those stages omit the flag, so this function returns the
    strict (possibly missing) value.
    """
    strict = strategy.get("live_ir")
    if _safe_float(strict) is not None:
        return strict
    if criteria.get("allow_low_sample_ir"):
        return strategy.get("live_ir_low_sample")
    return strict


def _strategy_blockers(strategy: dict[str, Any], criteria: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    effective_ir = _effective_live_ir(strategy, criteria)
    checks = [
        ("min_real_fills_per_strategy", "real_fill_trades", True, strategy.get("real_fill_trades")),
        ("min_live_days_per_strategy", "live_days", True, strategy.get("live_days")),
        ("min_strategy_live_ir", "live_ir", True, effective_ir),
        ("max_paper_live_ir_delta", "paper_live_ir_delta_abs", False, strategy.get("paper_live_ir_delta_abs")),
        ("min_spy_coverage_pct", "spy_coverage_pct", True, strategy.get("spy_coverage_pct")),
        ("max_strategy_drawdown_pct", "max_drawdown_pct", False, strategy.get("max_drawdown_pct")),
        ("min_capacity_test_usd", "capacity_test_usd", True, strategy.get("capacity_test_usd")),
    ]
    for criterion_key, label, is_min, actual in checks:
        if criterion_key not in criteria:
            continue
        required = float(criteria[criterion_key])
        if is_min:
            _metric_at_least(blockers, label, actual, required)
        else:
            _metric_at_most(blockers, label, actual, required)
    if criteria.get("require_cluster", True) and not strategy.get("cluster"):
        blockers.append("missing cluster")
    return blockers


def _qualified_strategies(evidence: dict[str, Any], criteria: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    qualified: list[dict[str, Any]] = []
    failed: dict[str, list[str]] = {}
    for name, raw in sorted((evidence.get("strategies") or {}).items()):
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        row.setdefault("strategy", name)
        blockers = _strategy_blockers(row, criteria)
        if blockers:
            failed[name] = blockers
        else:
            qualified.append(row)
    return qualified, failed


def stage_blockers(stage: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    """Return blockers for one stage. Empty means the stage is passed."""
    criteria = stage.get("criteria") or {}
    if not criteria:
        return []

    blockers: list[str] = []
    qualified, failed = _qualified_strategies(evidence, criteria)

    min_strats = int(criteria.get("min_qualified_strategies", 0) or 0)
    if len(qualified) < min_strats:
        blockers.append(
            f"qualified_strategies {len(qualified)}/{min_strats}; "
            f"failed={_summarize_failed_strategies(failed)}"
        )

    min_clusters = int(criteria.get("min_clusters", 0) or 0)
    if min_clusters:
        clusters = {str(s.get("cluster")) for s in qualified if s.get("cluster")}
        if len(clusters) < min_clusters:
            blockers.append(f"qualified_clusters {len(clusters)}/{min_clusters}")

    portfolio = evidence.get("portfolio") or {}
    if "min_fleet_live_ir" in criteria:
        _metric_at_least(blockers, "portfolio.fleet_live_ir", portfolio.get("fleet_live_ir"), criteria["min_fleet_live_ir"])
    if "max_fleet_drawdown_pct" in criteria:
        _metric_at_most(blockers, "portfolio.fleet_max_drawdown_pct", portfolio.get("fleet_max_drawdown_pct"), criteria["max_fleet_drawdown_pct"])
    if "max_worst_single_day_pct" in criteria:
        _metric_at_most(blockers, "portfolio.worst_single_day_pct", portfolio.get("worst_single_day_pct"), criteria["max_worst_single_day_pct"])

    ops = evidence.get("ops") or {}
    ops_checks = [
        ("min_clean_ops_days", "clean_ops_days", True),
        ("max_silent_failures_30d", "silent_failures_30d", False),
        ("max_phantom_fills_30d", "phantom_fills_30d", False),
        ("max_tws_cascades_30d", "tws_cascades_30d", False),
        ("max_margin_alerts_30d", "margin_alerts_30d", False),
        ("min_broker_reconcile_passed_days", "broker_reconcile_passed_days", True),
    ]
    for criterion_key, evidence_key, is_min in ops_checks:
        if criterion_key not in criteria:
            continue
        label = f"ops.{evidence_key}"
        if is_min:
            _metric_at_least(blockers, label, ops.get(evidence_key), criteria[criterion_key])
        else:
            _metric_at_most(blockers, label, ops.get(evidence_key), criteria[criterion_key])

    if criteria.get("require_kill_switch_tested"):
        _bool_required(blockers, "ops.kill_switch_tested", ops.get("kill_switch_tested"))
    if criteria.get("require_no_open_p0_incidents"):
        _metric_at_most(blockers, "ops.open_p0_incidents", ops.get("open_p0_incidents"), 0)

    diversification = evidence.get("diversification") or {}
    if "max_pairwise_corr_abs" in criteria:
        _metric_at_most(
            blockers,
            "diversification.max_pairwise_corr_abs",
            diversification.get("max_pairwise_corr_abs"),
            criteria["max_pairwise_corr_abs"],
        )
    if "min_effective_independent_bets" in criteria:
        _metric_at_least(
            blockers,
            "diversification.effective_independent_bets",
            diversification.get("effective_independent_bets"),
            criteria["min_effective_independent_bets"],
        )

    return blockers


def _summarize_failed_strategies(failed: dict[str, list[str]], limit: int = 3) -> str:
    if not failed:
        return "none"
    parts = []
    for name, reasons in list(failed.items())[:limit]:
        parts.append(f"{name}: {reasons[0] if reasons else 'blocked'}")
    extra = len(failed) - len(parts)
    if extra > 0:
        parts.append(f"+{extra} more")
    return "; ".join(parts)


def evaluate_ladder(
    evidence: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate every stage and return the highest passed capital cap."""
    cfg = config or load_config()
    stages = sorted(cfg.get("stages") or DEFAULT_CONFIG["stages"], key=lambda s: float(s.get("approved_capital_usd", 0)))
    stage_results = []
    highest_passed: dict[str, Any] | None = None
    for stage in stages:
        blockers = stage_blockers(stage, evidence)
        passed = len(blockers) == 0
        result = {
            "name": stage.get("name"),
            "approved_capital_usd": float(stage.get("approved_capital_usd", 0)),
            "passed": passed,
            "blockers": blockers,
        }
        stage_results.append(result)
        if passed:
            highest_passed = result

    if highest_passed is None:
        highest_passed = {
            "name": "NO_STAGE_PASSED",
            "approved_capital_usd": 0.0,
            "passed": False,
            "blockers": ["no stage passed"],
        }

    next_stage = None
    for result in stage_results:
        if result["approved_capital_usd"] > highest_passed["approved_capital_usd"]:
            next_stage = result
            break

    current_criteria = {}
    for stage in stages:
        if stage.get("name") == highest_passed["name"]:
            current_criteria = stage.get("criteria") or {}
            break
    if current_criteria:
        qualified, failed = _qualified_strategies(evidence, current_criteria)
    else:
        qualified, failed = [], {}

    approved = float(highest_passed["approved_capital_usd"])
    total_ref = float(cfg.get("total_capital_reference_usd", 100_000) or 100_000)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config_version": cfg.get("version", ""),
        "total_capital_reference_usd": total_ref,
        "approved_capital_usd": approved,
        "approved_capital_pct": round(approved / total_ref, 4) if total_ref > 0 else 0.0,
        "current_stage": highest_passed["name"],
        "next_stage": next_stage,
        "stage_results": stage_results,
        "qualified_strategies_current_stage": [s.get("strategy") for s in qualified],
        "failed_strategies_current_stage": failed,
        "evidence_generated_at": evidence.get("generated_at"),
        "sources": evidence.get("sources", {}),
    }


def evaluate_current_ladder() -> dict[str, Any]:
    cfg = load_config()
    evidence = build_current_evidence(config=cfg)
    return evaluate_ladder(evidence, cfg)


def approved_bot_capital_usd() -> float:
    """Return approved bot capital, failing closed to 0 on any error."""
    try:
        report = evaluate_current_ladder()
        return float(report.get("approved_capital_usd", 0.0) or 0.0)
    except Exception:
        return 0.0


def _print_report(report: dict[str, Any]) -> None:
    print(f"Capital ladder -- approved ${report['approved_capital_usd']:,.0f} "
          f"({report['current_stage']})")
    print(f"Generated: {report['generated_at']}")
    if report.get("next_stage"):
        ns = report["next_stage"]
        print(f"Next stage: {ns['name']} (${ns['approved_capital_usd']:,.0f})")
        for blocker in ns.get("blockers", []):
            print(f"  - {blocker}")
    else:
        print("Top stage reached.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print JSON report")
    parser.add_argument("--output", default=str(OUTPUT_PATH), help="Where to write report JSON")
    args = parser.parse_args(argv)

    report = evaluate_current_ladder()
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_report(report)
        print(f"Wrote: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
