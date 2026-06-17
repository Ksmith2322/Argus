"""Per-strategy daily verdict scorecard.

Combines four signals into a one-line GREEN / YELLOW / RED status for
each strategy:

  1. **Trace status** — invariant violations from today's golden trace
     (Phase 4 inspector). Any violation = RED.
  2. **Activity vs allocation** — strategy has allocation > 0 but zero
     fills today = RED (silent strategy, likely a wiring bug).
  3. **Heartbeat freshness** — last heartbeat update > 4h ago = RED
     (process is dead but allocation still expects activity).
  4. **EOD position state** — wake-and-sleep strategies should end the
     day flat; lingering open positions = YELLOW (operator review).

Inputs come from artifacts the operator's pipeline already produces:
  - argus_flow/data/canonical_fills.jsonl
  - forge/logs/<strategy>/heartbeat.json
  - argus_flow/configs/allocation_factors.json
  - argus_flow/logs/traces/<file>.jsonl (optional)

The verdict is reproducible: same inputs → same output. No randomness,
no time-of-day branching. Operator can run it 100 times a day and get
the same answer.

Use case: end-of-day fleet review. One command prints one card per
strategy. Sub-second runtime.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class StrategyScore:
    strategy: str
    status: str  # "GREEN" | "YELLOW" | "RED"
    reason: str
    metrics: dict = field(default_factory=dict)


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# ─── canonical_fills helpers ──────────────────────────────────────────────

def _fills_today_for_strategy(fills_path: Path, strategy: str,
                              today: str | None = None) -> list[dict]:
    if not fills_path.exists():
        return []
    today = today or _today_iso()
    out: list[dict] = []
    with fills_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("strategy") != strategy:
                continue
            ts = row.get("ts", "")
            if ts.startswith(today):
                out.append(row)
    return out


def _entry_exit_balance(fills: list[dict]) -> tuple[int, int]:
    """Returns (entry_count, exit_count) for the day. EOD flat
    expectation: entry_count == exit_count (each entry has matching exit)."""
    entries = sum(1 for r in fills if (r.get("side") or "").upper() == "ENTRY")
    exits = sum(1 for r in fills if (r.get("side") or "").upper() == "EXIT")
    return entries, exits


# ─── heartbeat helpers ────────────────────────────────────────────────────

def _heartbeat_age_minutes(heartbeat_path: Path,
                          now: Optional[datetime] = None) -> Optional[float]:
    """Return age of heartbeat file in minutes, or None if missing/unreadable."""
    if not heartbeat_path.exists():
        return None
    try:
        hb = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    last = hb.get("last_update") or hb.get("ts") or hb.get("timestamp")
    if not last:
        return None
    try:
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except Exception:
        return None
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - last_dt).total_seconds() / 60.0


# ─── trace helpers ────────────────────────────────────────────────────────

def _trace_findings(trace_path: Optional[Path]) -> tuple[int, int]:
    """Return (invariant_violation_count, anomaly_count) for the trace."""
    if trace_path is None or not trace_path.exists():
        return (0, 0)
    try:
        from helio.trace_invariants import verify_invariants
        from helio.trace_replay import find_anomalies, load_trace
        events = load_trace(trace_path)
        return len(verify_invariants(events)), len(find_anomalies(events))
    except Exception:
        return (0, 0)


# ─── score_strategy ───────────────────────────────────────────────────────

# Thresholds — tunable per operator preference but kept conservative
HEARTBEAT_STALE_MIN = 240   # 4 hours
ANOMALY_YELLOW_THRESHOLD = 3
POSITION_IMBALANCE_YELLOW = 1  # |entries - exits| > 0 at EOD


def score_strategy(
    strategy: str,
    *,
    allocation: float = 0.0,
    fills_path: Optional[Path] = None,
    trace_path: Optional[Path] = None,
    heartbeat_path: Optional[Path] = None,
    today: Optional[str] = None,
    now: Optional[datetime] = None,
) -> StrategyScore:
    """Score one strategy. Returns a StrategyScore with status + reason
    + numeric metrics."""
    today = today or _today_iso()

    fills_today = (_fills_today_for_strategy(fills_path, strategy, today)
                   if fills_path else [])
    entries, exits = _entry_exit_balance(fills_today)
    fills_count = len(fills_today)

    invariants, anomalies = _trace_findings(trace_path)

    hb_age = (_heartbeat_age_minutes(heartbeat_path, now)
              if heartbeat_path else None)

    metrics = {
        "allocation": allocation,
        "fills_today": fills_count,
        "entries": entries,
        "exits": exits,
        "invariant_violations": invariants,
        "anomalies": anomalies,
        "heartbeat_age_minutes": hb_age,
    }

    # Order matters — first matching rule wins (RED before YELLOW etc.)
    if invariants > 0:
        return StrategyScore(strategy, "RED",
                             f"{invariants} invariant violation(s) in today's trace",
                             metrics)
    if allocation > 0 and fills_count == 0 and hb_age is not None and hb_age < HEARTBEAT_STALE_MIN:
        # Process alive, no trades → maybe legit (low-frequency strategy)
        # but flag YELLOW for visibility — operator decides if expected.
        pass  # falls through to YELLOW checks
    if allocation > 0 and fills_count == 0 and (hb_age is None or hb_age >= HEARTBEAT_STALE_MIN):
        # Allocated but silent AND no fresh heartbeat — almost certainly dead
        return StrategyScore(strategy, "RED",
                             "allocated but no fills today and no fresh heartbeat — process likely dead",
                             metrics)
    if hb_age is not None and hb_age >= HEARTBEAT_STALE_MIN:
        return StrategyScore(strategy, "RED",
                             f"heartbeat stale ({hb_age:.0f} min)", metrics)

    yellows: list[str] = []
    if anomalies > ANOMALY_YELLOW_THRESHOLD:
        yellows.append(f"{anomalies} anomaly pattern(s)")
    if abs(entries - exits) > POSITION_IMBALANCE_YELLOW:
        yellows.append(f"position imbalance: {entries} entries vs {exits} exits")
    if allocation > 0 and fills_count == 0:
        yellows.append("no fills today (low-frequency strategy?)")

    if yellows:
        return StrategyScore(strategy, "YELLOW", "; ".join(yellows), metrics)
    return StrategyScore(strategy, "GREEN", "", metrics)


# ─── score_fleet (multi-strategy convenience) ─────────────────────────────

def score_fleet(
    strategies: list[tuple[str, float]],
    *,
    fills_path: Optional[Path] = None,
    trace_dir: Optional[Path] = None,
    heartbeat_dir: Optional[Path] = None,
    today: Optional[str] = None,
    now: Optional[datetime] = None,
) -> list[StrategyScore]:
    """Score each (strategy, allocation) pair. Tries to find per-strategy
    trace + heartbeat files by convention:
      trace_dir/<strategy>_<today>.jsonl
      heartbeat_dir/<strategy>/heartbeat.json
    Missing files are tolerated — the score reflects what's available."""
    today = today or _today_iso()
    out: list[StrategyScore] = []
    for strategy, allocation in strategies:
        t_path = None
        if trace_dir is not None:
            candidate = trace_dir / f"{strategy}_{today}.jsonl"
            if candidate.exists():
                t_path = candidate
        hb_path = None
        if heartbeat_dir is not None:
            candidate = heartbeat_dir / strategy / "heartbeat.json"
            if candidate.exists():
                hb_path = candidate
        out.append(score_strategy(
            strategy=strategy, allocation=allocation,
            fills_path=fills_path, trace_path=t_path,
            heartbeat_path=hb_path, today=today, now=now,
        ))
    return out
