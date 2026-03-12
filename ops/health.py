#!/usr/bin/env python3
"""
ops/health.py  --  Phase 16 Health Check Framework

Continuous health checks that feed into runtime-mode escalation.
Each check returns a HealthResult; the runner calls run_health_checks()
once per tick and the framework auto-escalates mode when triggers fire.

Health checks (Phase 16 spec):
  - feed staleness          (no fresh tick within threshold)
  - fill latency            (order-submit to fill exceeds threshold)
  - consecutive losses      (auto-throttle trigger)
  - slippage anomaly        (> p99 slippage)
  - invariant violation     (positions vs fills mismatch, etc.)

Safe-mode trigger mapping:
  feed_stale           -> OBSERVATION_ONLY
  fill_latency_high    -> NO_NEW_ENTRY
  consecutive_losses   -> REDUCE_ONLY
  slippage_anomaly     -> NO_NEW_ENTRY
  invariant_violation  -> OBSERVATION_ONLY
"""

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import runtime_mode


# ---------------------------------------------------------------------------
# Health result schema
# ---------------------------------------------------------------------------

@dataclass
class HealthResult:
    """Result of a single health check."""
    name: str
    ok: bool
    detail: str = ""
    value: Any = None
    threshold: Any = None
    ts: float = field(default_factory=time.time)


@dataclass
class HealthReport:
    """Aggregate of all health checks for a single tick."""
    results: List[HealthResult] = field(default_factory=list)
    ts: float = field(default_factory=time.time)
    escalated: bool = False
    prev_mode: str = ""
    new_mode: str = ""

    @property
    def all_ok(self) -> bool:
        return all(r.ok for r in self.results)

    @property
    def failed(self) -> List[HealthResult]:
        return [r for r in self.results if not r.ok]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.ts)),
            "all_ok": self.all_ok,
            "escalated": self.escalated,
            "prev_mode": self.prev_mode,
            "new_mode": self.new_mode,
            "checks": [
                {
                    "name": r.name,
                    "ok": r.ok,
                    "detail": r.detail,
                    "value": r.value,
                    "threshold": r.threshold,
                }
                for r in self.results
            ],
        }


# ---------------------------------------------------------------------------
# Safe-mode trigger mapping
# ---------------------------------------------------------------------------

# check_name -> mode to escalate to when that check fails
_TRIGGER_MAP: Dict[str, str] = {
    "feed_stale": runtime_mode.OBSERVATION_ONLY,
    "fill_latency": runtime_mode.NO_NEW_ENTRY,
    "consecutive_losses": runtime_mode.REDUCE_ONLY,
    "slippage_anomaly": runtime_mode.NO_NEW_ENTRY,
    "invariant_violation": runtime_mode.OBSERVATION_ONLY,
}


# ---------------------------------------------------------------------------
# Individual health checks
# ---------------------------------------------------------------------------

def check_feed_staleness(
    last_tick_epoch: float,
    threshold_s: float = 120.0,
) -> HealthResult:
    """Fail if no tick received within threshold_s seconds."""
    age = time.time() - last_tick_epoch if last_tick_epoch > 0 else float("inf")
    ok = age <= threshold_s
    return HealthResult(
        name="feed_stale",
        ok=ok,
        detail=f"tick_age={age:.1f}s threshold={threshold_s}s",
        value=round(age, 1),
        threshold=threshold_s,
    )


def check_fill_latency(
    last_submit_ts: float,
    last_fill_ts: float,
    threshold_s: float = 30.0,
) -> HealthResult:
    """Fail if latest fill took longer than threshold_s after order submit."""
    if last_submit_ts <= 0 or last_fill_ts <= 0:
        return HealthResult(name="fill_latency", ok=True, detail="no_pending_order")
    latency = last_fill_ts - last_submit_ts
    if latency < 0:
        # fill before submit means stale data; treat as ok
        return HealthResult(name="fill_latency", ok=True, detail="fill_before_submit")
    ok = latency <= threshold_s
    return HealthResult(
        name="fill_latency",
        ok=ok,
        detail=f"latency={latency:.2f}s threshold={threshold_s}s",
        value=round(latency, 2),
        threshold=threshold_s,
    )


def check_consecutive_losses(
    consecutive_losses: int,
    threshold: int = 5,
) -> HealthResult:
    """Fail if consecutive losing trades exceeds threshold."""
    ok = consecutive_losses < threshold
    return HealthResult(
        name="consecutive_losses",
        ok=ok,
        detail=f"consecutive_losses={consecutive_losses} threshold={threshold}",
        value=consecutive_losses,
        threshold=threshold,
    )


def check_slippage_anomaly(
    last_slippage_bps: float,
    p99_threshold_bps: float = 50.0,
) -> HealthResult:
    """Fail if last fill slippage exceeds p99 threshold."""
    if last_slippage_bps is None or last_slippage_bps < 0:
        return HealthResult(name="slippage_anomaly", ok=True, detail="no_slippage_data")
    ok = abs(last_slippage_bps) <= p99_threshold_bps
    return HealthResult(
        name="slippage_anomaly",
        ok=ok,
        detail=f"slippage={last_slippage_bps:.1f}bps p99_threshold={p99_threshold_bps}bps",
        value=round(last_slippage_bps, 1),
        threshold=p99_threshold_bps,
    )


def check_invariant(
    positions_match_fills: bool,
    detail: str = "",
) -> HealthResult:
    """Fail if a critical invariant is violated."""
    return HealthResult(
        name="invariant_violation",
        ok=positions_match_fills,
        detail=detail or ("ok" if positions_match_fills else "positions_fills_mismatch"),
        value=positions_match_fills,
        threshold=True,
    )


# ---------------------------------------------------------------------------
# Aggregate runner
# ---------------------------------------------------------------------------

def run_health_checks(
    *,
    last_tick_epoch: float = 0,
    feed_stale_threshold_s: float = 120.0,
    last_submit_ts: float = 0,
    last_fill_ts: float = 0,
    fill_latency_threshold_s: float = 30.0,
    consecutive_losses: int = 0,
    consecutive_loss_threshold: int = 5,
    last_slippage_bps: Optional[float] = None,
    slippage_p99_bps: float = 50.0,
    positions_match_fills: bool = True,
    invariant_detail: str = "",
    mode_file: Optional[str] = None,
    auto_escalate: bool = True,
) -> HealthReport:
    """Run all health checks and optionally escalate runtime mode.

    Returns HealthReport with results and escalation info.
    """
    results: List[HealthResult] = []

    results.append(check_feed_staleness(last_tick_epoch, feed_stale_threshold_s))
    results.append(check_fill_latency(last_submit_ts, last_fill_ts, fill_latency_threshold_s))
    results.append(check_consecutive_losses(consecutive_losses, consecutive_loss_threshold))
    results.append(check_slippage_anomaly(
        last_slippage_bps if last_slippage_bps is not None else -1,
        slippage_p99_bps,
    ))
    results.append(check_invariant(positions_match_fills, invariant_detail))

    report = HealthReport(results=results, ts=time.time())

    if auto_escalate and not report.all_ok:
        # Find the most restrictive trigger among all failing checks
        worst_mode = runtime_mode.FULL
        worst_reason_parts: List[str] = []
        for r in report.failed:
            target = _TRIGGER_MAP.get(r.name)
            if target and runtime_mode.is_more_restrictive(target, worst_mode):
                worst_mode = target
            if target:
                worst_reason_parts.append(f"{r.name}:{r.detail}")

        if worst_mode != runtime_mode.FULL:
            reason = "; ".join(worst_reason_parts)
            changed, prev, new = runtime_mode.transition(
                worst_mode,
                reason=reason,
                triggered_by="health_check",
                mode_file=mode_file,
            )
            report.escalated = changed
            report.prev_mode = prev
            report.new_mode = new

    return report


# ---------------------------------------------------------------------------
# Health log persistence (append-only JSONL)
# ---------------------------------------------------------------------------

def _health_log_path(log_dir: Optional[str] = None) -> str:
    if log_dir:
        return os.path.join(log_dir, "health_checks.jsonl")
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "ops", "logs", "health_checks.jsonl")


def log_health_report(report: HealthReport, log_dir: Optional[str] = None) -> None:
    """Append health report to JSONL log (best-effort)."""
    if report.all_ok:
        return  # only log failures to keep file manageable
    path = _health_log_path(log_dir)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    try:
        with open(path, "a") as f:
            f.write(json.dumps(report.to_dict()) + "\n")
    except Exception:
        pass
