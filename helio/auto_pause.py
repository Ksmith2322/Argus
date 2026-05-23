"""Auto-pause recommendation engine — daily history + persistent-FAIL detector.

Built 2026-05-23 as the action layer on top of live_gate_monitor. Each daily
run of live_gate_monitor appends a snapshot to history; this module reads
the history and detects strategies with K consecutive FAIL days.

INTENTIONALLY ALERT-ONLY. Does NOT:
    - Modify allocation_factors.json
    - Write HALT.flag (fleet-wide)
    - Stop any runner

Instead WRITES auto_pause_recommendations.json which the operator reviews.
The operator then decides whether to set allocation_factor=0 manually.

This separation keeps destructive actions human-gated while ensuring the
operator gets a structured, audited record of when each strategy first
hit FAIL status, how many consecutive days, and what the historical PFs
looked like leading up to it.

Files this module manages:
    argus_flow/logs/cohort_gate_history.jsonl   (append-only daily log)
    argus_flow/logs/auto_pause_recommendations.json  (latest alerts)

CLI:
    python -m helio.auto_pause                  # snapshot current status, emit alerts
    python -m helio.auto_pause --history-only   # show history without snapshotting

Exit codes:
    0  no alerts
    1  WARNING-tier alerts present
    2  PAUSE-tier alerts present (K consecutive FAIL days)
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


_REPO = Path(__file__).resolve().parents[1]
LOG_DIR = _REPO / "argus_flow" / "logs"
HISTORY_PATH = LOG_DIR / "cohort_gate_history.jsonl"
RECOMMENDATIONS_PATH = LOG_DIR / "auto_pause_recommendations.json"

# How many consecutive FAIL days before recommending pause
DEFAULT_FAIL_DAYS_FOR_PAUSE = 5
# How many consecutive WARNING days before flagging
DEFAULT_WARNING_DAYS_FOR_FLAG = 3


@dataclass
class StrategyAlert:
    """Per-strategy auto-pause recommendation."""
    strategy: str
    alert_tier: str            # OK / WARNING / PAUSE_RECOMMENDED
    current_verdict: str
    consecutive_days_at_verdict: int
    threshold_days: int
    first_seen_at_verdict: Optional[str]
    last_live_pf: Optional[float]
    baseline_ci_lower: Optional[float]
    recommendation: str

    def to_dict(self) -> dict:
        return asdict(self)


def append_history(report: dict, *, path: Path = HISTORY_PATH) -> None:
    """Append the cohort gate status report to the daily history JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # One row per snapshot: timestamp + per-strategy status
    row = {
        "ts": report.get("generated_at", datetime.now(timezone.utc).isoformat()),
        "baseline_version": report.get("baseline_version", "?"),
        "verdict_counts": report.get("verdict_counts", {}),
        "strategies": [
            {
                "strategy": s["strategy"],
                "verdict": s["verdict"],
                "live_pf_30trades": s.get("live_pf_30trades"),
                "live_pf_90days": s.get("live_pf_90days"),
                "n_live_trades": s.get("n_live_trades", 0),
                "pct_of_ci_lower": s.get("pct_of_ci_lower"),
            }
            for s in report.get("strategies", [])
        ],
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def load_history(*, path: Path = HISTORY_PATH) -> list[dict]:
    """Read the JSONL history. Returns [] on missing file."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _consecutive_days_at_verdict(
    history: list[dict],
    strategy: str,
    verdicts: set[str],
) -> tuple[int, Optional[str]]:
    """Walk history backward from latest; count consecutive days where this
    strategy's verdict was in `verdicts`. Return (count, first_seen_ts)."""
    if not history:
        return (0, None)
    # Sort by ts descending
    sorted_history = sorted(history, key=lambda r: r.get("ts", ""), reverse=True)
    count = 0
    first_seen = None
    for row in sorted_history:
        for s in row.get("strategies", []):
            if s.get("strategy") != strategy:
                continue
            if s.get("verdict") in verdicts:
                count += 1
                first_seen = row.get("ts")
            else:
                return (count, first_seen)
            break  # found this strategy in this row, move to next row
    return (count, first_seen)


def compute_alerts(
    *,
    history: list[dict],
    fail_days_for_pause: int = DEFAULT_FAIL_DAYS_FOR_PAUSE,
    warning_days_for_flag: int = DEFAULT_WARNING_DAYS_FOR_FLAG,
) -> list[StrategyAlert]:
    """For each strategy in the most recent snapshot, determine its alert
    tier based on persistent-FAIL detection."""
    if not history:
        return []
    latest = sorted(history, key=lambda r: r.get("ts", ""))[-1]
    alerts = []
    for s in latest.get("strategies", []):
        strategy = s["strategy"]
        verdict = s.get("verdict", "")
        live_pf = s.get("live_pf_30trades") or s.get("live_pf_90days")
        # Find historical baseline_ci_lower from the most recent baseline_version
        baseline_ci_lower = s.get("baseline_ci_lower")

        # Count consecutive FAIL days
        fail_days, fail_first_seen = _consecutive_days_at_verdict(
            history, strategy, {"FAIL"},
        )
        # Count consecutive WARNING-or-worse days
        warning_days, warning_first_seen = _consecutive_days_at_verdict(
            history, strategy, {"WARNING", "FAIL"},
        )

        tier = "OK"
        recommendation = "Continue running at current allocation."
        threshold = 0
        first_seen = None

        if verdict == "FAIL" and fail_days >= fail_days_for_pause:
            tier = "PAUSE_RECOMMENDED"
            threshold = fail_days_for_pause
            first_seen = fail_first_seen
            recommendation = (
                f"Strategy has been in FAIL status for {fail_days} consecutive "
                f"days (threshold: {fail_days_for_pause}). RECOMMEND setting "
                f"allocation_factor to 0 in argus_flow/configs/allocation_factors.json. "
                f"Operator review the live PF trajectory before action."
            )
        elif verdict == "FAIL":
            tier = "WARNING"
            threshold = fail_days_for_pause
            first_seen = fail_first_seen
            recommendation = (
                f"Strategy hit FAIL today (day {fail_days} of {fail_days_for_pause}). "
                f"Monitor closely; if persists, pause recommendation triggers in "
                f"{fail_days_for_pause - fail_days} more day(s)."
            )
        elif verdict == "WARNING" and warning_days >= warning_days_for_flag:
            tier = "WARNING"
            threshold = warning_days_for_flag
            first_seen = warning_first_seen
            recommendation = (
                f"Strategy has been in WARNING status for {warning_days} consecutive "
                f"days. Live PF is between 0.70-0.85 of baseline CI lower bound. "
                f"Operator review the trajectory."
            )
        elif verdict == "INSUFFICIENT_N":
            tier = "OK"
            recommendation = "Insufficient live trades for evaluation. Waiting for n>=20."
        elif verdict == "PASS_GATE":
            tier = "OK"
            recommendation = "Live PF above warning threshold. Strategy operating as expected."

        alerts.append(StrategyAlert(
            strategy=strategy,
            alert_tier=tier,
            current_verdict=verdict,
            consecutive_days_at_verdict=fail_days if verdict == "FAIL" else warning_days,
            threshold_days=threshold,
            first_seen_at_verdict=first_seen,
            last_live_pf=live_pf,
            baseline_ci_lower=baseline_ci_lower,
            recommendation=recommendation,
        ))
    return alerts


def write_recommendations(
    alerts: list[StrategyAlert],
    *,
    path: Path = RECOMMENDATIONS_PATH,
) -> Path:
    """Persist the recommendations JSON for operator review."""
    path.parent.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for a in alerts:
        counts[a.alert_tier] = counts.get(a.alert_tier, 0) + 1
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "alert_tier_counts": counts,
        "alerts": [a.to_dict() for a in alerts],
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def run(
    *,
    snapshot: bool = True,
    fail_days_for_pause: int = DEFAULT_FAIL_DAYS_FOR_PAUSE,
    warning_days_for_flag: int = DEFAULT_WARNING_DAYS_FOR_FLAG,
) -> int:
    """One-shot run: snapshot status, append to history, compute alerts.

    Returns exit code: 0 (OK), 1 (WARNING tier), 2 (PAUSE_RECOMMENDED).
    """
    if snapshot:
        # Take a fresh snapshot via live_gate_monitor
        from helio.live_gate_monitor import evaluate_cohort
        report = evaluate_cohort()
        append_history(report)
    history = load_history()
    alerts = compute_alerts(
        history=history,
        fail_days_for_pause=fail_days_for_pause,
        warning_days_for_flag=warning_days_for_flag,
    )
    write_recommendations(alerts)
    # Exit code reflects worst tier
    if any(a.alert_tier == "PAUSE_RECOMMENDED" for a in alerts):
        return 2
    if any(a.alert_tier == "WARNING" for a in alerts):
        return 1
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-only", action="store_true",
                        help="Don't snapshot; just compute alerts from existing history")
    parser.add_argument("--fail-days", type=int, default=DEFAULT_FAIL_DAYS_FOR_PAUSE,
                        help="Consecutive FAIL days before recommending pause")
    parser.add_argument("--warning-days", type=int, default=DEFAULT_WARNING_DAYS_FOR_FLAG,
                        help="Consecutive WARNING days before flagging")
    args = parser.parse_args(argv)
    rc = run(
        snapshot=not args.history_only,
        fail_days_for_pause=args.fail_days,
        warning_days_for_flag=args.warning_days,
    )
    # Pretty-print the latest alerts
    if RECOMMENDATIONS_PATH.exists():
        data = json.loads(RECOMMENDATIONS_PATH.read_text(encoding="utf-8"))
        counts = data.get("alert_tier_counts", {})
        print(f"=== Auto-pause recommendations ===")
        print(f"Tier counts: {counts}")
        print()
        for a in data.get("alerts", []):
            symbol = {"OK": " ", "WARNING": "!", "PAUSE_RECOMMENDED": "X"}.get(
                a["alert_tier"], "?")
            print(f"  [{symbol}] {a['strategy']:<28} {a['alert_tier']:<18} "
                  f"verdict={a['current_verdict']:<16} "
                  f"days={a['consecutive_days_at_verdict']}/{a['threshold_days']}")
            if a["alert_tier"] != "OK":
                print(f"      {a['recommendation']}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
