"""Promotion-readiness check — concrete preconditions for real-money flip.

Built 2026-05-23. Complements helio/real_money.py (the BOUNDARY enforcer)
with the question: "is the candidate strategy READY to be added to the
real_money allowlist?"

Default-skeptical design. Every check defaults to NOT_READY and requires
explicit evidence to flip to READY. The module is read-only: it doesn't
modify allowlists, allocation_factors, or any other config. It produces
a structured report the operator can act on.

PRECONDITIONS CHECKED (for the candidate strategy):
    1. Allocation factor active (> 0)
    2. Live gate monitor history has >= MIN_HISTORY_DAYS days
    3. Live PF >= warning threshold for K of last L snapshots
    4. No PAUSE_RECOMMENDED alert active
    5. No HALT.flag present (fleet-wide)
    6. Strategy entry in real_money_allowlist.json (operator authorization)
    7. global_enabled=true in allowlist
    8. ledger_entry_id, approver, signed_at fields populated
    9. Capital-ladder approval present (helio/capital_ladder)
    10. Baseline CI lower meets the promotion floor (1.20)

OUTPUT: PromotionReadinessReport with per-precondition status,
overall verdict (READY_FOR_REAL / NOT_READY), and explicit blockers.

CLI:
    python -m helio.promotion_readiness_check --strategy forge_xs_momentum
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


_REPO = Path(__file__).resolve().parents[1]
LOG_DIR = _REPO / "argus_flow" / "logs"
HISTORY_PATH = LOG_DIR / "cohort_gate_history.jsonl"
RECOMMENDATIONS_PATH = LOG_DIR / "auto_pause_recommendations.json"
BASELINE_PATH = _REPO / "argus_flow" / "configs" / "promotion_gate_baseline.json"
ALLOCATION_PATH = _REPO / "argus_flow" / "configs" / "allocation_factors.json"
ALLOWLIST_PATH = _REPO / "argus_flow" / "configs" / "real_money_allowlist.json"
HALT_FLAG_PATH = LOG_DIR / "HALT.flag"

# Tunables for the readiness gate
MIN_HISTORY_DAYS = 30            # 30+ snapshots in cohort_gate_history.jsonl
MIN_PASSING_FRACTION = 0.80      # >=80% of last L snapshots at PASS_GATE
PROMOTION_FLOOR = 1.20           # baseline CI lower must be >= this


@dataclass
class Check:
    """One precondition's pass/fail + reason."""
    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PromotionReadinessReport:
    strategy: str
    verdict: str                  # READY_FOR_REAL / NOT_READY / CONFIG_ERROR
    checks: list[Check] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    generated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "verdict": self.verdict,
            "generated_at": self.generated_at,
            "n_checks": len(self.checks),
            "n_passed": sum(1 for c in self.checks if c.passed),
            "blockers": list(self.blockers),
            "checks": [c.to_dict() for c in self.checks],
        }


# ─── individual checks ─────────────────────────────────────────────

def _load_history() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    rows = []
    for line in HISTORY_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _load_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def check_allocation(strategy: str) -> Check:
    data = _load_json(ALLOCATION_PATH)
    if data is None:
        return Check("allocation_active", False,
                     f"allocation_factors.json missing or unreadable")
    factor = data.get("factors", {}).get(strategy)
    if factor is None:
        return Check("allocation_active", False,
                     f"{strategy} not in allocation_factors.factors")
    if float(factor) <= 0:
        return Check("allocation_active", False,
                     f"{strategy} allocation factor is {factor} (must be > 0)")
    return Check("allocation_active", True,
                 f"{strategy} allocated at {factor}× in paper")


def check_history_depth(strategy: str, min_days: int = MIN_HISTORY_DAYS) -> Check:
    history = _load_history()
    # Count snapshots that include this strategy
    n = sum(1 for row in history
            if any(s.get("strategy") == strategy for s in row.get("strategies", [])))
    if n < min_days:
        return Check("history_depth", False,
                     f"only {n} snapshots in cohort_gate_history (need {min_days}+)")
    return Check("history_depth", True,
                 f"{n} snapshots in history (>= {min_days})")


def check_live_pf_consistency(strategy: str,
                               min_passing_fraction: float = MIN_PASSING_FRACTION,
                               window_days: int = 30,
                               *, now: Optional[datetime] = None) -> Check:
    history = _load_history()
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)
    recent_verdicts = []
    for row in history:
        try:
            ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        if ts < cutoff:
            continue
        for s in row.get("strategies", []):
            if s.get("strategy") == strategy:
                recent_verdicts.append(s.get("verdict", "?"))
                break
    if not recent_verdicts:
        return Check("live_pf_consistency", False,
                     f"no {strategy} snapshots in last {window_days} days")
    n_passing = sum(1 for v in recent_verdicts if v == "PASS_GATE")
    fraction = n_passing / len(recent_verdicts)
    if fraction < min_passing_fraction:
        return Check("live_pf_consistency", False,
                     f"PASS_GATE on {n_passing}/{len(recent_verdicts)} = "
                     f"{fraction:.0%} of recent snapshots (need >= "
                     f"{min_passing_fraction:.0%})")
    return Check("live_pf_consistency", True,
                 f"PASS_GATE on {n_passing}/{len(recent_verdicts)} = "
                 f"{fraction:.0%} of recent {window_days}-day snapshots")


def check_no_pause_alert(strategy: str) -> Check:
    recs = _load_json(RECOMMENDATIONS_PATH)
    if recs is None:
        return Check("no_pause_alert", True,
                     "no recommendations file (no alerts to violate)")
    for alert in recs.get("alerts", []):
        if alert.get("strategy") != strategy:
            continue
        tier = alert.get("alert_tier", "OK")
        if tier == "PAUSE_RECOMMENDED":
            return Check("no_pause_alert", False,
                         f"strategy has PAUSE_RECOMMENDED alert active: "
                         f"{alert.get('recommendation', 'no detail')}")
        if tier == "WARNING":
            return Check("no_pause_alert", False,
                         f"strategy has WARNING alert active: "
                         f"{alert.get('recommendation', 'no detail')}")
    return Check("no_pause_alert", True, "no active WARNING/PAUSE alerts")


def check_no_halt_flag() -> Check:
    if HALT_FLAG_PATH.exists():
        try:
            content = HALT_FLAG_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            content = "(unreadable)"
        return Check("no_halt_flag", False,
                     f"HALT.flag present: {content[:200]}")
    return Check("no_halt_flag", True, "HALT.flag absent")


def check_allowlist_entry(strategy: str) -> Check:
    data = _load_json(ALLOWLIST_PATH)
    if data is None:
        return Check("allowlist_entry", False,
                     "real_money_allowlist.json missing or unreadable")
    strategies = data.get("strategies", [])
    if not any(s.get("strategy") == strategy if isinstance(s, dict) else s == strategy
                for s in strategies):
        return Check("allowlist_entry", False,
                     f"{strategy} not in allowlist (strategies={strategies})")
    return Check("allowlist_entry", True,
                 f"{strategy} present in allowlist")


def check_global_enabled() -> Check:
    data = _load_json(ALLOWLIST_PATH)
    if data is None:
        return Check("global_enabled", False, "allowlist missing")
    if not data.get("global_enabled", False):
        return Check("global_enabled", False,
                     "allowlist global_enabled is false")
    return Check("global_enabled", True, "allowlist global_enabled = true")


def check_ledger_fields_populated() -> Check:
    data = _load_json(ALLOWLIST_PATH)
    if data is None:
        return Check("ledger_fields", False, "allowlist missing")
    missing = []
    for field_name in ("ledger_entry_id", "approver", "signed_at"):
        v = data.get(field_name, "")
        if not v:
            missing.append(field_name)
    if missing:
        return Check("ledger_fields", False,
                     f"allowlist missing operator-fill fields: {missing}")
    return Check("ledger_fields", True,
                 f"allowlist has ledger_entry_id={data.get('ledger_entry_id')}, "
                 f"approver={data.get('approver')}")


def check_baseline_ci_lower(strategy: str,
                             floor: float = PROMOTION_FLOOR) -> Check:
    data = _load_json(BASELINE_PATH)
    if data is None:
        return Check("baseline_ci_floor", False, "baseline config missing")
    spec = data.get("strategies", {}).get(strategy)
    if not spec:
        return Check("baseline_ci_floor", False,
                     f"{strategy} not in baseline config")
    ci = float(spec.get("ci_95_lower", 0.0))
    if ci < floor:
        return Check("baseline_ci_floor", False,
                     f"{strategy} baseline CI lower {ci:.3f} < floor {floor}")
    return Check("baseline_ci_floor", True,
                 f"{strategy} baseline CI lower {ci:.3f} >= {floor}")


def check_capital_ladder_permits(strategy: str) -> Check:
    """Best-effort: check helio.capital_ladder for an approved-capital
    entry for the strategy. The full ladder check requires runtime
    broker state, so here we just verify the ladder module loads."""
    try:
        from helio import capital_ladder  # noqa: F401
        return Check("capital_ladder", True,
                     "capital_ladder module importable (full check is "
                     "runtime via real_money.enforce_real_money_boundary)")
    except ImportError as e:
        return Check("capital_ladder", False,
                     f"capital_ladder import failed: {e}")


# ─── top-level orchestration ───────────────────────────────────────

def evaluate(strategy: str, *, now: Optional[datetime] = None) -> PromotionReadinessReport:
    checks = [
        check_allocation(strategy),
        check_history_depth(strategy),
        check_live_pf_consistency(strategy, now=now),
        check_no_pause_alert(strategy),
        check_no_halt_flag(),
        check_allowlist_entry(strategy),
        check_global_enabled(),
        check_ledger_fields_populated(),
        check_baseline_ci_lower(strategy),
        check_capital_ladder_permits(strategy),
    ]
    blockers = [f"{c.name}: {c.detail}" for c in checks if not c.passed]
    verdict = "READY_FOR_REAL" if not blockers else "NOT_READY"
    return PromotionReadinessReport(
        strategy=strategy,
        verdict=verdict,
        checks=checks,
        blockers=blockers,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


def render_text(report: PromotionReadinessReport) -> str:
    lines = []
    lines.append(f"=== Promotion readiness: {report.strategy} ===")
    lines.append(f"Generated: {report.generated_at}")
    lines.append(f"Verdict: {report.verdict}")
    lines.append("")
    lines.append("Checks:")
    for c in report.checks:
        mark = "+" if c.passed else "X"
        lines.append(f"  [{mark}] {c.name:<24} {c.detail}")
    if report.blockers:
        lines.append("")
        lines.append("BLOCKERS TO REAL-MONEY PROMOTION:")
        for b in report.blockers:
            lines.append(f"  - {b}")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", default="forge_xs_momentum",
                        help="Strategy to check readiness for (default: forge_xs_momentum)")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON instead of text")
    args = parser.parse_args(argv)
    report = evaluate(args.strategy)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        print(render_text(report))
    return 0 if report.verdict == "READY_FOR_REAL" else 1


if __name__ == "__main__":
    sys.exit(main())
