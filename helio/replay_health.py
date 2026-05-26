"""Pre-trade replay-health bridge (Creative agent #1).

Connects helio/replay_harness.py to helio/ibkr_execution.submit_bracket.

WHY THIS EXISTS
===============
Tonight's 4-out-of-5 strategy candidate kills (overnight_drift,
credit_spread, sell_in_may, turn_of_quarter) showed how hard it is to
get a new strategy right pre-deployment. The other side of the same
problem is: even strategies that pass the disciplined gate can have
their LIVE behavior drift from their backtest. The 2026-05-19 CADJPY
incident (JPY 5-decimal lmtPrice rejected) and the 2026-04-27 GLD
sizing-bug-at-$22-cap both produced fills the backtest never would
have made. They were diagnosed POST-incident from log triage.

The Pre-Trade Replay Bridge inverts that flow:
  - A nightly cron runs the replay vs canonical_fills diff for each
    active strategy and writes argus_flow/logs/replay_health.json
  - submit_bracket reads that file at entry time
  - If a strategy is flagged DRIFT (replay-vs-ledger mismatches > 0
    over the trailing N trades) or the check is stale (>max_age_hours),
    new entries are refused with a clear reject_reason
  - Exits + position management are NOT blocked — only NEW entries,
    so existing trades close out cleanly

DESIGN — fail-closed in the right places, fail-open in others
=============================================================
Refusing all trades when the replay bridge breaks would be worse than
the original problem (catastrophic stop). So:

  - DRIFT detected → REFUSE new entries (the actual goal)
  - replay_health.json missing → ALLOW (don't break fleet at boot
    before first nightly run has populated the file)
  - replay_health.json older than max_age_hours → REFUSE +
    WARN (operator must investigate or disable manually)
  - REPLAY_BRIDGE_DISABLED=1 env var set → ALLOW (operator escape
    hatch for incidents)
  - Strategy not listed in replay_health.json → ALLOW with WARN
    (new strategy that hasn't been evaluated yet)
  - File read error → ALLOW with WARN (don't break fleet on a JSON typo)

The asymmetry: blocking is reserved for the case where we have
POSITIVE EVIDENCE of drift. Absence of evidence is not evidence of
absence (don't block on unknowns).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[1]
DEFAULT_HEALTH_PATH = REPO / "argus_flow" / "logs" / "replay_health.json"

# How recent the health-check must be before we trust it as a fresh signal.
# 36h gives one full cron-cycle slack (nightly + 12h buffer).
DEFAULT_MAX_AGE_HOURS = 36.0

# Maximum REPLAY_ONLY + TICKER_DIVERGENT count over the trailing-N-trades
# window. 0 = strict (any mismatch blocks). 1 = tolerates one anomaly.
DEFAULT_MAX_MISMATCH = 0

# Strategies the bridge should evaluate. Conservatively, only strategies
# with a working replay function in helio.replay_harness. We can add
# more entries as more replay functions are written.
SUPPORTED_STRATEGIES: tuple[str, ...] = (
    "forge_xs_momentum",
    "forge_xs_momentum_sectors",
    "forge_xs_momentum_style",
    "forge_xs_momentum_legacy15",
    "forge_xs_momentum_style_top3",
    "forge_xs_momentum_legacy15_regime",
    "forge_xs_momentum_global47",
    "forge_gld_pm_long",
)

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReplayHealthCheck:
    allow: bool
    reason: str          # "ok" / "drift" / "stale" / "disabled" / "absent" / "unread"
    detail: str = ""
    n_mismatches: int = 0
    age_hours: float = 0.0
    strategy: Optional[str] = None


# ── Health file read ────────────────────────────────────────────────────

def _read_health_file(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _file_age_hours(path: Path) -> float:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return float("inf")
    return (datetime.now(timezone.utc).timestamp() - mtime) / 3600.0


# ── Public guard ────────────────────────────────────────────────────────

def check_replay_health(
    strategy_label: str,
    *,
    path: Path = DEFAULT_HEALTH_PATH,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    max_mismatch: int = DEFAULT_MAX_MISMATCH,
) -> ReplayHealthCheck:
    """Pre-trade guard: should this strategy be allowed to submit
    new entries right now?

    Called from helio.ibkr_execution.submit_bracket BEFORE any broker
    round-trip. Returns ReplayHealthCheck(allow, reason, ...).

    See module docstring for the fail-open vs fail-closed rationale.
    """
    if not strategy_label:
        return ReplayHealthCheck(
            allow=True, reason="absent", strategy=strategy_label,
            detail="no strategy_label provided — bridge skipped",
        )

    # Operator escape hatch
    if os.environ.get("REPLAY_BRIDGE_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return ReplayHealthCheck(
            allow=True, reason="disabled", strategy=strategy_label,
            detail="REPLAY_BRIDGE_DISABLED env var set",
        )

    # Only enforce on strategies that have a registered replay function
    if strategy_label not in SUPPORTED_STRATEGIES:
        return ReplayHealthCheck(
            allow=True, reason="absent", strategy=strategy_label,
            detail="strategy not in SUPPORTED_STRATEGIES list",
        )

    raw = _read_health_file(path)
    if raw is None:
        # File missing or unreadable — allow (don't break fleet at boot)
        return ReplayHealthCheck(
            allow=True, reason="unread", strategy=strategy_label,
            detail=f"replay_health.json missing or unreadable at {path}",
        )

    # File staleness — REFUSE if too old. Operator must investigate.
    age_h = _file_age_hours(path)
    if age_h > max_age_hours:
        return ReplayHealthCheck(
            allow=False, reason="stale", strategy=strategy_label,
            detail=f"replay_health.json is {age_h:.1f}h old (max {max_age_hours:.1f}h) — "
                   f"nightly cron likely stopped; investigate before trading",
            age_hours=age_h,
        )

    per_strat = (raw.get("strategies") or {}).get(strategy_label)
    if per_strat is None:
        return ReplayHealthCheck(
            allow=True, reason="absent", strategy=strategy_label,
            detail="strategy not yet in replay_health.json (first-time evaluation pending)",
            age_hours=age_h,
        )

    # Warming state — fleet just reset, replay events haven't had time
    # to fill yet. ALLOW (the alternative is blocking all trades on a
    # fresh fleet, which is the wrong default).
    status = per_strat.get("status", "ok")
    if status == "warming":
        return ReplayHealthCheck(
            allow=True, reason="warming", strategy=strategy_label,
            detail=f"fleet is {per_strat.get('days_since_epoch', 0):.1f}d "
                   f"into post-reset window; replay-vs-ledger comparison "
                   f"not yet meaningful",
            age_hours=age_h,
        )

    # The "block" cases — actual drift evidence
    n_mismatch = int(per_strat.get("n_blocking_mismatches", 0))
    if n_mismatch > max_mismatch:
        return ReplayHealthCheck(
            allow=False, reason="drift", strategy=strategy_label,
            detail=(f"{n_mismatch} replay-vs-ledger blocking mismatches "
                    f"(threshold {max_mismatch}); see {path}"),
            n_mismatches=n_mismatch,
            age_hours=age_h,
        )

    return ReplayHealthCheck(
        allow=True, reason="ok", strategy=strategy_label,
        detail=f"clean ({n_mismatch} mismatches at {age_h:.1f}h)",
        n_mismatches=n_mismatch,
        age_hours=age_h,
    )


# ── Health-file writer (called by the nightly cron) ────────────────────

@dataclass
class StrategyHealth:
    strategy: str
    n_replay_events: int
    n_ledger_fills: int
    n_matched: int
    n_mismatches: int
    n_blocking_mismatches: int   # REPLAY_ONLY + TICKER_DIVERGENT (the "bad" kinds)
    mismatch_counts: dict[str, int] = field(default_factory=dict)
    last_check_ts: str = ""
    status: str = "ok"           # "ok" / "drift" / "warming" / "error"
    error: Optional[str] = None
    epoch_id: Optional[str] = None
    epoch_started_at: Optional[str] = None
    days_since_epoch: float = 0.0


# Strategies need this many days of post-epoch live evidence before a
# replay-vs-ledger mismatch is treated as DRIFT rather than WARMING.
# Monthly-rebalance strategies need at least one rebalance cycle to
# produce any fills; daily strategies need a few cycles to be honest.
WARMING_THRESHOLD_DAYS = 35.0


def _get_current_epoch() -> tuple[Optional[str], Optional[datetime]]:
    """Read the current evidence epoch from argus_flow/configs/evidence_epoch.json.
    Returns (epoch_id, started_at) or (None, None) if unreadable."""
    epoch_path = REPO / "argus_flow" / "configs" / "evidence_epoch.json"
    try:
        raw = json.loads(epoch_path.read_text(encoding="utf-8"))
        current_id = raw.get("current_epoch_id")
        for e in raw.get("epochs", []):
            if e.get("id") == current_id:
                started_str = e.get("started_at")
                if not started_str:
                    return current_id, None
                started = datetime.fromisoformat(started_str.replace("Z", "+00:00"))
                return current_id, started
    except (OSError, json.JSONDecodeError, ValueError, KeyError):
        return None, None
    return None, None


def _filter_replay_events_to_epoch(events: list, epoch_start: datetime) -> list:
    """Keep only replay events with ts >= epoch_start."""
    out = []
    for e in events:
        ts_str = getattr(e, "ts", "")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            try:
                ts = datetime.strptime(ts_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        if ts >= epoch_start:
            out.append(e)
    return out


def compute_strategy_health(strategy_label: str, *, period: str = "1y") -> StrategyHealth:
    """Run the replay + diff for one strategy. Filters both replay events
    and ledger fills to the current evidence epoch so a freshly-reset
    fleet doesn't show false-positive DRIFT against historical-but-quarantined
    canonical_fills."""
    epoch_id, epoch_start = _get_current_epoch()
    sh = StrategyHealth(
        strategy=strategy_label,
        n_replay_events=0, n_ledger_fills=0, n_matched=0,
        n_mismatches=0, n_blocking_mismatches=0,
        last_check_ts=datetime.now(timezone.utc).isoformat(),
        epoch_id=epoch_id,
        epoch_started_at=epoch_start.isoformat() if epoch_start else None,
    )
    if epoch_start is not None:
        sh.days_since_epoch = (datetime.now(timezone.utc) - epoch_start).total_seconds() / 86400.0

    try:
        from helio.replay_harness import (
            replay_xs_momentum, replay_gld_pm_long,
            diff_against_canonical_fills,
        )
        if strategy_label.startswith("forge_xs_momentum"):
            events = replay_xs_momentum(strategy_label, period=period)
        elif strategy_label == "forge_gld_pm_long":
            events = replay_gld_pm_long(period="60d")
        else:
            sh.status = "error"
            sh.error = f"no replay function registered for {strategy_label}"
            return sh

        # Filter replay events to post-epoch — the canonical_fills ledger
        # is wiped at each epoch reset, so comparing pre-epoch replay events
        # to an empty post-epoch ledger always shows DRIFT.
        if epoch_start is not None:
            events = _filter_replay_events_to_epoch(events, epoch_start)

        diff = diff_against_canonical_fills(events, strategy=strategy_label)
    except Exception as exc:
        sh.status = "error"
        sh.error = f"replay+diff failed: {exc!r}"
        return sh

    sh.n_replay_events = int(diff.get("n_replay_events", 0))
    sh.n_ledger_fills = int(diff.get("n_ledger_fills", 0))
    sh.n_matched = int(diff.get("n_matched", 0))
    sh.n_mismatches = int(diff.get("n_mismatches", 0))
    sh.mismatch_counts = dict(diff.get("mismatch_counts", {}))
    sh.n_blocking_mismatches = (
        sh.mismatch_counts.get("REPLAY_ONLY", 0)
        + sh.mismatch_counts.get("TICKER_DIVERGENT", 0)
    )

    # Status decision tree:
    # - Fleet too new since epoch (< WARMING_THRESHOLD_DAYS): WARMING
    #   (replay events have had no time to produce fills yet)
    # - No blocking mismatches: OK
    # - Blocking mismatches exist: DRIFT
    if sh.days_since_epoch < WARMING_THRESHOLD_DAYS:
        sh.status = "warming"
    elif sh.n_blocking_mismatches > 0:
        sh.status = "drift"
    else:
        sh.status = "ok"
    return sh


def write_health_file(
    healths: list[StrategyHealth],
    *,
    path: Path = DEFAULT_HEALTH_PATH,
) -> None:
    """Write the health-check artifact that submit_bracket reads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "n_strategies": len(healths),
        "strategies": {
            h.strategy: {
                "n_replay_events": h.n_replay_events,
                "n_ledger_fills": h.n_ledger_fills,
                "n_matched": h.n_matched,
                "n_mismatches": h.n_mismatches,
                "n_blocking_mismatches": h.n_blocking_mismatches,
                "mismatch_counts": h.mismatch_counts,
                "status": h.status,
                "error": h.error,
                "last_check_ts": h.last_check_ts,
                "epoch_id": h.epoch_id,
                "epoch_started_at": h.epoch_started_at,
                "days_since_epoch": round(h.days_since_epoch, 2),
            }
            for h in healths
        },
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
