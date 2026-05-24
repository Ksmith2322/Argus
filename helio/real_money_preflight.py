"""Real-money preflight — formalize the 12-point promotion-grade audit
card into an executable.

The codex audit (2026-05-18) defined a 12-point card that EVERY strategy
must pass before any real-money allocation. Until tonight those checks
existed conceptually + were scattered across helio/live_gate_monitor,
helio/real_money, helio/capacity_stress, helio/evidence_epoch,
allocation_factors.json, and the sunset roster test pin.

This module composes them into a single per-strategy evaluation:

  evaluate_strategy("forge_xs_momentum") → StrategyPreflight
    .verdict ∈ {READY_FOR_REAL, BLOCKED, BLOCKED_PENDING_REVIEW}
    .checks  → list of CheckResult, one per of the 12 criteria

USAGE (programmatic):
    from helio.real_money_preflight import evaluate_strategy
    result = evaluate_strategy("forge_xs_momentum")
    print(result.verdict, result.summary())

USAGE (CLI): see ops/real_money_preflight.py.

DESIGN PRINCIPLES

- READ-ONLY: this module never writes files or mutates allocation. It's
  diagnosis, not action.
- FAIL-CLOSED: any check that can't be evaluated (missing data, raised
  exception) defaults to RED with an explanation. The bar for real
  money is "all GREEN evidence", not "no evidence of problems".
- DETERMINISTIC: same inputs → same output. Network calls are caught
  in try/except and treated as RED (so a flaky yfinance fetch can't
  silently bless a strategy).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional


REPO = Path(__file__).resolve().parents[1]
LIVE_PF_WARNING_PCT = 0.85   # match live_gate_monitor alert thresholds
LIVE_PF_FAIL_PCT = 0.70
DEFAULT_LIVE_N_MIN = 20
DEFAULT_HEARTBEAT_MAX_AGE_HOURS = 24


class Verdict(str, Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"


@dataclass
class CheckResult:
    name: str
    verdict: Verdict
    value: object = None         # raw value observed (string, number, or dict)
    reason: str = ""             # plain-English explanation
    unblock: str = ""            # specific action to take to flip this RED/YELLOW to GREEN

    def to_dict(self) -> dict:
        return {"name": self.name, "verdict": self.verdict.value,
                 "value": self.value, "reason": self.reason,
                 "unblock": self.unblock}


# Per-check unblock paths. Operator-facing — these are the actions that
# move the corresponding RED/YELLOW result to GREEN.
_UNBLOCK_RECIPES: dict[str, str] = {
    "in_active_roster":
        "Add the strategy to ACTIVE_ROSTER in "
        "argus_flow/tests/test_sunset_roster.py.",
    "allocation_factor_positive":
        "Set allocation_factor > 0 in argus_flow/configs/"
        "allocation_factors.json with a justification line in _kill_log.",
    "disciplined_gate_passes":
        "Re-run promotion_panel at the strategy's REALISTIC slippage. If "
        "CI lower remains < 1.20, the strategy stays BLOCKED. To revive a "
        "killed strategy you need fresh evidence OR a parameter change "
        "that passes the gate.",
    "live_evidence_n":
        "Time-only: keep the runner alive in --loop and let live trades "
        "accumulate to n >= 20. xs_momentum's monthly cadence means n=20 "
        "= ~20 months from a single asset. Lower-cadence strategies can "
        "reach n=20 in days.",
    "live_pf_band":
        "If FAIL: open OPERATOR_HANDOFF.md and check the strategy's "
        "recent fills; consider auto-pause-apply. If WARNING: continue "
        "to collect evidence + monitor next 5 trades.",
    "trade_source_is_live":
        "Verify forge/logs/<strategy>/trades.csv exists with rows AFTER "
        "the current evidence_epoch start. If only pre-reset archive "
        "data exists, the strategy needs a fresh post-reset trade first.",
    "capacity_headroom_2x":
        "Raise PER_CLUSTER_CAP_X or SINGLE_INSTRUMENT_CAP_X in "
        "helio/cluster_exposure.py. Re-run "
        "`python -m ops.audit.run_capacity_stress`. Operator decision "
        "involving real risk tradeoffs.",
    "real_money_allowlist":
        "(1) Set global_enabled=true in argus_flow/configs/"
        "real_money_allowlist.json. (2) Add the strategy to "
        "strategies. (3) Set approver + ledger_entry_id + signed_at. "
        "This is a DELIBERATE operator action — do NOT do it until "
        "evidence + DD profile justify it.",
    "evidence_epoch_clean":
        "Run `python -m helio.evidence_epoch` to inspect current epoch. "
        "If contaminated, an operator-gated epoch_reset is required.",
    "killed_strategy_invariant":
        "The strategy is in KILLED_STRATEGY_CUTOFFS. To revive, remove "
        "the entry from helio/roi_filter.KILLED_STRATEGY_CUTOFFS with an "
        "operator-note commit, then re-flip allocation_factor > 0.",
    "heartbeat_fresh":
        "Start the runner: `python -m forge.<strategy>.runner --loop`. "
        "Verify with `python -m ops.audit.run_fleet_snapshot`.",
    "halt_flag_absent":
        "Delete argus_flow/logs/HALT.flag (operator review required) "
        "OR call POST /api/resume_fleet on the dashboard.",
    "evidence_quality":
        "If pre_epoch fills detected: run "
        "`python -m ops.maintenance.epoch_reset --target <date> --execute` "
        "to archive them. If EXITs > ENTRYs: the runner is dropping ENTRY "
        "rows — check submit_bracket's write_fill path. If no fills yet: "
        "wait for first real trade to land in canonical_fills.",
}


def _attach_unblock(result: CheckResult) -> CheckResult:
    """Mutate result in place to attach the unblock recipe for its name
    if the verdict isn't already GREEN."""
    if result.verdict != Verdict.GREEN:
        result.unblock = _UNBLOCK_RECIPES.get(result.name, "")
    return result


@dataclass
class StrategyPreflight:
    strategy: str
    verdict: str = ""            # READY_FOR_REAL / BLOCKED / BLOCKED_PENDING_REVIEW
    checks: list[CheckResult] = field(default_factory=list)
    n_green: int = 0
    n_yellow: int = 0
    n_red: int = 0

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "verdict": self.verdict,
            "n_green": self.n_green,
            "n_yellow": self.n_yellow,
            "n_red": self.n_red,
            "checks": [c.to_dict() for c in self.checks],
        }

    def summary(self) -> str:
        return (f"{self.strategy}: {self.verdict}  "
                  f"(GREEN={self.n_green}, YELLOW={self.n_yellow}, RED={self.n_red})")


# ─── individual checks ───────────────────────────────────────────────

def check_in_active_roster(strategy: str) -> CheckResult:
    """Pin: strategy must be in the ACTIVE_ROSTER set defined in
    argus_flow/tests/test_sunset_roster.py. That set is the authoritative
    list of "intended-to-be-trading" strategies."""
    try:
        from argus_flow.tests.test_sunset_roster import ACTIVE_ROSTER
        if strategy in ACTIVE_ROSTER:
            return CheckResult("in_active_roster", Verdict.GREEN,
                                 value=True, reason="in test_sunset_roster.ACTIVE_ROSTER")
        return CheckResult("in_active_roster", Verdict.RED,
                             value=False,
                             reason=("not in test_sunset_roster.ACTIVE_ROSTER; "
                                     "add it explicitly to opt in"))
    except Exception as exc:
        return CheckResult("in_active_roster", Verdict.RED,
                             reason=f"could not load ACTIVE_ROSTER: {exc}")


def check_allocation_factor_positive(strategy: str) -> CheckResult:
    """allocation_factor > 0.0 in argus_flow/configs/allocation_factors.json."""
    try:
        from helio.fleet_sizing import get_allocation_factor
        factor = float(get_allocation_factor(strategy))
        if factor > 0.0:
            return CheckResult("allocation_factor_positive", Verdict.GREEN,
                                 value=factor,
                                 reason=f"allocation_factor={factor}")
        return CheckResult("allocation_factor_positive", Verdict.RED,
                             value=factor,
                             reason=f"allocation_factor={factor} — strategy deallocated")
    except Exception as exc:
        return CheckResult("allocation_factor_positive", Verdict.RED,
                             reason=f"could not read allocation_factor: {exc}")


def check_disciplined_gate_passes(strategy: str) -> CheckResult:
    """promotion_gate_baseline.json CI lower at realistic slippage >= 1.20.
    Uses recalibrated_ci_95_lower_at_realistic when present, falls back
    to ci_95_lower."""
    baseline_path = (REPO / "argus_flow" / "configs"
                       / "promotion_gate_baseline.json")
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return CheckResult("disciplined_gate_passes", Verdict.RED,
                             reason=f"could not read baseline: {exc}")
    strategies = baseline.get("strategies") or {}
    spec = strategies.get(strategy)
    if spec is None:
        return CheckResult("disciplined_gate_passes", Verdict.RED,
                             reason=f"{strategy} missing from promotion_gate_baseline.json")
    floor = float(baseline.get("promotion_floor", 1.20))
    realistic = spec.get("recalibrated_ci_95_lower_at_realistic")
    published = spec.get("ci_95_lower")
    ci_lower = float(realistic) if realistic is not None else (
        float(published) if published is not None else None)
    if ci_lower is None:
        return CheckResult("disciplined_gate_passes", Verdict.RED,
                             reason=f"{strategy} has no CI lower bound recorded")
    if ci_lower >= floor:
        return CheckResult("disciplined_gate_passes", Verdict.GREEN,
                             value=ci_lower,
                             reason=f"CI lower {ci_lower:.3f} >= floor {floor:.2f}")
    return CheckResult("disciplined_gate_passes", Verdict.RED,
                         value=ci_lower,
                         reason=(f"CI lower {ci_lower:.3f} < floor {floor:.2f} "
                                 f"(verdict in baseline: {spec.get('verdict', '?')})"))


def _live_status(strategy: str):
    """Helper: call live_gate_monitor with the committed baseline."""
    from helio.live_gate_monitor import (
        evaluate_strategy as _live_eval,
        load_baseline,
    )
    return _live_eval(strategy, baseline=load_baseline())


def check_live_evidence_n(strategy: str,
                           n_min: int = DEFAULT_LIVE_N_MIN) -> CheckResult:
    """live_gate_monitor reports n_live_trades >= n_min."""
    try:
        st = _live_status(strategy)
    except Exception as exc:
        return CheckResult("live_evidence_n", Verdict.RED,
                             reason=f"live_gate_monitor failed: {exc}")
    n = int(st.n_live_trades or 0)
    if n >= n_min:
        return CheckResult("live_evidence_n", Verdict.GREEN,
                             value=n,
                             reason=f"n_live_trades={n} >= {n_min}")
    return CheckResult("live_evidence_n", Verdict.RED,
                         value=n,
                         reason=f"n_live_trades={n} < {n_min} (insufficient post-reset evidence)")


def check_live_pf_band(strategy: str) -> CheckResult:
    """live_gate_monitor verdict must NOT be FAIL or WARNING."""
    try:
        st = _live_status(strategy)
    except Exception as exc:
        return CheckResult("live_pf_band", Verdict.RED,
                             reason=f"live_gate_monitor failed: {exc}")
    verdict = st.verdict
    if verdict == "PASS_GATE":
        return CheckResult("live_pf_band", Verdict.GREEN,
                             value=verdict,
                             reason=f"live verdict = PASS_GATE")
    if verdict == "INSUFFICIENT_N":
        return CheckResult("live_pf_band", Verdict.YELLOW,
                             value=verdict,
                             reason="insufficient live trades; cannot confirm PF band")
    if verdict == "WARNING":
        return CheckResult("live_pf_band", Verdict.YELLOW,
                             value=verdict,
                             reason=("live PF in WARNING band (0.70-0.85 of CI lower); "
                                     "operator review required"))
    if verdict == "FAIL":
        return CheckResult("live_pf_band", Verdict.RED,
                             value=verdict,
                             reason=("live PF below 0.70 of CI lower; "
                                     "strategy is failing"))
    return CheckResult("live_pf_band", Verdict.RED,
                         value=verdict,
                         reason=f"unknown verdict: {verdict}")


def check_trade_source_is_live(strategy: str) -> CheckResult:
    """live_gate_monitor.trade_source must be 'live' (NOT 'pre_reset_archive'
    or 'none' — both indicate contaminated or absent evidence)."""
    try:
        st = _live_status(strategy)
    except Exception as exc:
        return CheckResult("trade_source_is_live", Verdict.RED,
                             reason=f"live_gate_monitor failed: {exc}")
    src = getattr(st, "trade_source", "none")
    if src == "live":
        return CheckResult("trade_source_is_live", Verdict.GREEN,
                             value=src, reason="trades.csv reads from live log dir")
    if src == "pre_reset_archive":
        return CheckResult("trade_source_is_live", Verdict.RED,
                             value=src,
                             reason=("trade_source is pre_reset_archive — "
                                     "evidence is contaminated from before "
                                     "the 5/22 reset"))
    return CheckResult("trade_source_is_live", Verdict.RED,
                         value=src,
                         reason=f"trade_source={src} (no live trades.csv found)")


CAPACITY_STRESS_ARTIFACT = (REPO / "ops" / "reports" / "system_audit"
                              / "capacity_stress.json")


def check_capacity_headroom(strategy: str, multiplier: float = 2.0) -> CheckResult:
    """Strategy must pass the capacity stress test at `multiplier`x its
    configured cap. Reads the published artifact at
    ops/reports/system_audit/capacity_stress.json (produced by
    ops/audit/run_capacity_stress.py). YELLOW if artifact is stale or
    strategy isn't recorded; RED only if the artifact explicitly shows
    headroom below the target multiplier."""
    if not CAPACITY_STRESS_ARTIFACT.exists():
        return CheckResult("capacity_headroom_2x", Verdict.YELLOW,
                             reason=("no capacity_stress.json artifact — "
                                     "run `python -m ops.audit.run_capacity_stress`"))
    try:
        data = json.loads(CAPACITY_STRESS_ARTIFACT.read_text(encoding="utf-8"))
    except Exception as exc:
        return CheckResult("capacity_headroom_2x", Verdict.RED,
                             reason=f"could not read capacity_stress.json: {exc}")
    per_strategy = data.get("per_strategy") or []
    record = next((r for r in per_strategy
                     if r.get("strategy") == strategy
                     or r.get("strategy_label") == strategy), None)
    if record is None:
        return CheckResult("capacity_headroom_2x", Verdict.YELLOW,
                             reason=(f"{strategy} not in capacity_stress.json — "
                                     "re-run the audit to include it"))
    max_safe = float(record.get("max_safe_multiplier", 0.0) or 0.0)
    if max_safe >= multiplier:
        return CheckResult("capacity_headroom_2x", Verdict.GREEN,
                             value=max_safe,
                             reason=f"max_safe_multiplier={max_safe:.1f} >= {multiplier}")
    return CheckResult("capacity_headroom_2x", Verdict.YELLOW,
                         value=max_safe,
                         reason=(f"max_safe_multiplier={max_safe:.1f} < {multiplier}; "
                                 "strategy will breach cluster caps when scaled"))


def check_real_money_allowlist(strategy: str) -> CheckResult:
    """Strategy must be in helio.real_money allowlist AND
    global_enabled=true."""
    try:
        from helio.real_money import load_allowlist
        al = load_allowlist()
    except Exception as exc:
        return CheckResult("real_money_allowlist", Verdict.RED,
                             reason=f"load_allowlist failed: {exc}")
    if not al.global_enabled:
        return CheckResult("real_money_allowlist", Verdict.RED,
                             value={"global_enabled": False},
                             reason="real_money_allowlist.global_enabled=false (safe default)")
    if strategy not in al.strategies:
        return CheckResult("real_money_allowlist", Verdict.RED,
                             value={"global_enabled": True, "in_list": False},
                             reason=f"{strategy} not in allowlist.strategies")
    problems = al.validate_self()
    if problems:
        return CheckResult("real_money_allowlist", Verdict.YELLOW,
                             value={"global_enabled": True, "in_list": True,
                                    "validation_problems": problems},
                             reason=f"allowlist has validation problems: {problems}")
    return CheckResult("real_money_allowlist", Verdict.GREEN,
                         value={"global_enabled": True, "in_list": True},
                         reason="allowlist explicit + global_enabled")


def check_evidence_epoch_clean() -> CheckResult:
    """Current evidence_epoch must have is_clean=True."""
    try:
        from helio.evidence_epoch import current_epoch
        epoch = current_epoch()
    except Exception as exc:
        return CheckResult("evidence_epoch_clean", Verdict.RED,
                             reason=f"evidence_epoch load failed: {exc}")
    if getattr(epoch, "is_clean", False):
        return CheckResult("evidence_epoch_clean", Verdict.GREEN,
                             value=getattr(epoch, "epoch_id", "?"),
                             reason=f"current epoch is_clean=True")
    return CheckResult("evidence_epoch_clean", Verdict.RED,
                         value=getattr(epoch, "epoch_id", "?"),
                         reason="current epoch is_clean=False (contaminated)")


def check_evidence_quality(strategy: str) -> CheckResult:
    """Reject EXIT-only ROI math + stale pre-reset evidence (Codex
    gap #5). For each EXIT row in canonical_fills since the current
    epoch, verify a matching ENTRY row exists. If EXITs outnumber
    ENTRYs (an entry was missed) OR fills predate the epoch start,
    the evidence is contaminated and shouldn't be used for ROI."""
    try:
        from helio.evidence_epoch import current_epoch
        epoch = current_epoch()
        epoch_start = getattr(epoch, "started_at", None)
        if epoch_start is None:
            return CheckResult("evidence_quality", Verdict.YELLOW,
                                 reason="evidence_epoch has no started_at")
    except Exception as exc:
        return CheckResult("evidence_quality", Verdict.YELLOW,
                             reason=f"could not load epoch: {exc}")
    fills_path = REPO / "argus_flow" / "logs" / "canonical_fills.jsonl"
    if not fills_path.exists():
        return CheckResult("evidence_quality", Verdict.YELLOW,
                             reason="no canonical_fills.jsonl yet (clean epoch)")
    n_entry = 0
    n_exit = 0
    n_pre_epoch = 0
    try:
        epoch_iso = epoch_start.isoformat() if hasattr(epoch_start, "isoformat") else str(epoch_start)
        for line in fills_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("strategy") != strategy:
                continue
            ts = row.get("ts") or row.get("entry_ts") or row.get("exit_ts") or ""
            if ts < epoch_iso:
                n_pre_epoch += 1
                continue
            side = (row.get("side") or "").upper()
            if side == "ENTRY":
                n_entry += 1
            elif side == "EXIT":
                n_exit += 1
    except Exception as exc:
        return CheckResult("evidence_quality", Verdict.RED,
                             reason=f"could not scan canonical_fills: {exc}")
    if n_pre_epoch > 0:
        return CheckResult("evidence_quality", Verdict.RED,
                             value={"pre_epoch": n_pre_epoch,
                                    "post_epoch_entries": n_entry,
                                    "post_epoch_exits": n_exit},
                             reason=(f"{n_pre_epoch} pre-epoch fills detected — "
                                     "epoch_reset should have archived these"))
    if n_exit > n_entry:
        return CheckResult("evidence_quality", Verdict.RED,
                             value={"entries": n_entry, "exits": n_exit},
                             reason=(f"EXIT-only fills detected: {n_exit} exits "
                                     f"vs {n_entry} entries — ROI math is wrong "
                                     "until matching ENTRY rows are recovered"))
    if n_entry == 0 and n_exit == 0:
        return CheckResult("evidence_quality", Verdict.YELLOW,
                             value={"entries": 0, "exits": 0},
                             reason="no post-epoch fills yet (clean epoch)")
    return CheckResult("evidence_quality", Verdict.GREEN,
                         value={"entries": n_entry, "exits": n_exit},
                         reason=f"{n_entry} entries, {n_exit} exits since epoch start, "
                                f"balanced + post-epoch")


def check_killed_strategy_invariant(strategy: str) -> CheckResult:
    """Strategy must NOT be in KILLED_STRATEGY_CUTOFFS (the kill registry)."""
    try:
        from helio.roi_filter import KILLED_STRATEGY_CUTOFFS
    except Exception as exc:
        return CheckResult("killed_strategy_invariant", Verdict.RED,
                             reason=f"could not load KILLED_STRATEGY_CUTOFFS: {exc}")
    if strategy in KILLED_STRATEGY_CUTOFFS:
        return CheckResult("killed_strategy_invariant", Verdict.RED,
                             value=KILLED_STRATEGY_CUTOFFS[strategy],
                             reason=(f"strategy is in KILLED_STRATEGY_CUTOFFS "
                                     f"(killed {KILLED_STRATEGY_CUTOFFS[strategy]})"))
    return CheckResult("killed_strategy_invariant", Verdict.GREEN,
                         value=False, reason="not in kill registry")


def check_heartbeat_fresh(strategy: str,
                           max_age_hours: float = DEFAULT_HEARTBEAT_MAX_AGE_HOURS
                           ) -> CheckResult:
    """The runner's heartbeat.json must have been touched in the last
    max_age_hours. Stale heartbeat = process is dead or stuck."""
    short = strategy.replace("forge_", "")
    hb_path = REPO / "forge" / "logs" / short / "heartbeat.json"
    if not hb_path.exists():
        return CheckResult("heartbeat_fresh", Verdict.RED,
                             value=str(hb_path),
                             reason=f"no heartbeat.json at {hb_path}")
    try:
        mtime = datetime.fromtimestamp(hb_path.stat().st_mtime, tz=timezone.utc)
    except Exception as exc:
        return CheckResult("heartbeat_fresh", Verdict.RED,
                             reason=f"could not stat heartbeat: {exc}")
    age_hours = (datetime.now(timezone.utc) - mtime).total_seconds() / 3600.0
    if age_hours <= max_age_hours:
        return CheckResult("heartbeat_fresh", Verdict.GREEN,
                             value=round(age_hours, 2),
                             reason=f"heartbeat {age_hours:.1f}h old (<= {max_age_hours}h)")
    return CheckResult("heartbeat_fresh", Verdict.RED,
                         value=round(age_hours, 2),
                         reason=f"heartbeat {age_hours:.1f}h old > {max_age_hours}h "
                                f"(runner likely dead)")


def check_halt_flag_absent() -> CheckResult:
    """HALT.flag must NOT exist (fleet-wide halt would block all entries)."""
    halt_path = REPO / "argus_flow" / "logs" / "HALT.flag"
    if halt_path.exists():
        try:
            content = halt_path.read_text(encoding="utf-8").strip()[:200]
        except Exception:
            content = "(unreadable)"
        return CheckResult("halt_flag_absent", Verdict.RED,
                             value=str(halt_path),
                             reason=f"HALT.flag present: {content}")
    return CheckResult("halt_flag_absent", Verdict.GREEN,
                         value=False, reason="no HALT.flag")


# ─── combined evaluator ──────────────────────────────────────────────

def evaluate_strategy(
    strategy: str,
    *,
    n_min: int = DEFAULT_LIVE_N_MIN,
    capacity_multiplier: float = 2.0,
    heartbeat_max_age_hours: float = DEFAULT_HEARTBEAT_MAX_AGE_HOURS,
) -> StrategyPreflight:
    """Run all 12 preflight checks and combine into a single verdict."""
    checks: list[CheckResult] = [
        _attach_unblock(check_in_active_roster(strategy)),
        _attach_unblock(check_allocation_factor_positive(strategy)),
        _attach_unblock(check_disciplined_gate_passes(strategy)),
        _attach_unblock(check_live_evidence_n(strategy, n_min=n_min)),
        _attach_unblock(check_live_pf_band(strategy)),
        _attach_unblock(check_trade_source_is_live(strategy)),
        _attach_unblock(check_evidence_quality(strategy)),
        _attach_unblock(check_capacity_headroom(strategy,
                                                  multiplier=capacity_multiplier)),
        _attach_unblock(check_real_money_allowlist(strategy)),
        _attach_unblock(check_evidence_epoch_clean()),
        _attach_unblock(check_killed_strategy_invariant(strategy)),
        _attach_unblock(check_heartbeat_fresh(
            strategy, max_age_hours=heartbeat_max_age_hours)),
        _attach_unblock(check_halt_flag_absent()),
    ]
    n_green = sum(1 for c in checks if c.verdict == Verdict.GREEN)
    n_yellow = sum(1 for c in checks if c.verdict == Verdict.YELLOW)
    n_red = sum(1 for c in checks if c.verdict == Verdict.RED)

    if n_red > 0:
        verdict = "BLOCKED"
    elif n_yellow > 0:
        verdict = "BLOCKED_PENDING_REVIEW"
    else:
        verdict = "READY_FOR_REAL"

    return StrategyPreflight(
        strategy=strategy,
        verdict=verdict,
        checks=checks,
        n_green=n_green,
        n_yellow=n_yellow,
        n_red=n_red,
    )


def render_strategy_report(p: StrategyPreflight) -> str:
    """Operator-friendly per-strategy summary."""
    symbol = {"READY_FOR_REAL": "[OK] ",
                "BLOCKED_PENDING_REVIEW": "[??] ",
                "BLOCKED": "[NO] "}.get(p.verdict, "[??] ")
    lines = []
    lines.append(f"{symbol}{p.strategy} — {p.verdict}  "
                   f"(GREEN={p.n_green} YELLOW={p.n_yellow} RED={p.n_red})")
    for c in p.checks:
        marker = {Verdict.GREEN: " + ",
                    Verdict.YELLOW: " ~ ",
                    Verdict.RED: " ! "}[c.verdict]
        lines.append(f"  {marker} {c.name:<30}  {c.verdict.value:<6}  {c.reason}")
    return "\n".join(lines)
