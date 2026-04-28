"""Auto-applying allocator (Layer 4 enforcement, MVP with safety rails).

Reads decision-engine ACTIONs and updates allocation_factors.json automatically
under strict guardrails. ALL changes audit-logged regardless of mode.

DESIGN — defaults to DRY-RUN. Enable real apply via:
   {"auto_apply_enabled": true} in allocation_factors.json

Safety rails (always on):
   1. SCALE_UP and KILL are NEVER auto-applied (require explicit operator click)
      Reason: scaling up amplifies risk; killing flatlines a strategy outright.
      Both deserve manual review.
   2. Only HOLD and REDUCE actions get auto-considered for apply.
   3. 24h cooldown per strategy — max one change per strategy per day.
   4. If HALT.flag or FLATTEN_EOD.flag is set, NO auto-changes.
   5. If circuit_breaker tier != OK, NO auto-changes.
   6. Audit log written to argus_flow/logs/_risk/allocator_audit.jsonl

Wired hourly into managed_truth_loop. Operator can also invoke manually:
   python -m ops.auto_allocator --dry-run    # always dry-run regardless of config
   python -m ops.auto_allocator              # respect config flag
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ALLOC_PATH = REPO / "argus_flow" / "configs" / "allocation_factors.json"
AUDIT_PATH = REPO / "argus_flow" / "logs" / "_risk" / "allocator_audit.jsonl"
HALT_PATH = REPO / "argus_flow" / "logs" / "HALT.flag"
FLATTEN_PATH = REPO / "argus_flow" / "logs" / "FLATTEN_EOD.flag"
CB_PATH = REPO / "argus_flow" / "logs" / "_risk" / "circuit_breaker_state.json"

COOLDOWN_HOURS = 24
DASHBOARD_URL = "http://localhost:8080"

# ACTION -> recommended factor. NULL = never auto-apply.
RECOMMENDATION = {
    "SCALE_UP": None,   # manual only — scaling up is risky
    "HOLD":     1.0,    # auto-OK
    "REDUCE":   0.5,    # auto-OK (conservative)
    "KILL":     None,   # manual only — terminal action
    "IGNORE":   None,   # no recommendation
}


def _load_alloc() -> dict:
    if ALLOC_PATH.exists():
        try: return json.loads(ALLOC_PATH.read_text(encoding="utf-8"))
        except: pass
    return {"factors": {}}


def _save_alloc(cfg: dict) -> None:
    ALLOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALLOC_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _audit(record: dict) -> None:
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    record["ts"] = datetime.now(timezone.utc).isoformat()
    with AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _last_change_ts(strategy: str) -> datetime | None:
    """Walk audit log backwards to find most recent applied change for this strategy."""
    if not AUDIT_PATH.exists():
        return None
    try:
        # Read last N lines (cheap: file grows slowly)
        lines = AUDIT_PATH.read_text(encoding="utf-8").strip().splitlines()
        for line in reversed(lines[-200:]):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("strategy") == strategy and r.get("applied"):
                return datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
        return None
    except Exception:
        return None


def main(force_dry_run: bool = False) -> int:
    cfg = _load_alloc()
    factors = cfg.setdefault("factors", {})
    auto_enabled = bool(cfg.get("auto_apply_enabled", False))
    effective_dry_run = force_dry_run or not auto_enabled

    # Safety: halt / flatten / circuit-breaker checks
    skip_reason = None
    if HALT_PATH.exists():
        skip_reason = "HALT.flag present"
    elif FLATTEN_PATH.exists():
        skip_reason = "FLATTEN_EOD.flag present"
    elif CB_PATH.exists():
        try:
            cb = json.loads(CB_PATH.read_text(encoding="utf-8"))
            if cb.get("current_tier", "OK") != "OK":
                skip_reason = f"circuit_breaker tier={cb.get('current_tier')}"
        except Exception:
            pass
    if skip_reason and not effective_dry_run:
        print(f"SKIP auto-apply: {skip_reason} (would still log dry-run records)")
        # Still log the skip
        _audit({"event": "skip", "reason": skip_reason})
        return 0

    # Pull strategy_actions (the recommendation source)
    try:
        with urllib.request.urlopen(f"{DASHBOARD_URL}/api/strategy_actions?window_days=30", timeout=10) as r:
            actions = json.loads(r.read())
    except Exception as e:
        print(f"FAIL: cannot read strategy_actions: {e}")
        return 1

    now = datetime.now(timezone.utc)
    n_considered = 0
    n_applied = 0
    n_skipped_cooldown = 0
    n_skipped_manual = 0

    for s in actions.get("strategies", []):
        strat = s["strategy"]
        action = s["action"]
        rec = RECOMMENDATION.get(action)
        n_considered += 1

        if rec is None:
            n_skipped_manual += 1
            _audit({"event": "skip_manual_only", "strategy": strat, "action": action})
            continue

        # Get current factor
        candidates = [strat, strat.replace("forge_", ""), "forge_" + strat]
        cur = 1.0
        cur_key = strat
        for k in candidates:
            if k in factors:
                cur = factors[k]
                cur_key = k
                break

        if abs(cur - rec) < 0.01:
            # Already at target
            continue

        # Cooldown check
        last_ts = _last_change_ts(strat)
        if last_ts and (now - last_ts) < timedelta(hours=COOLDOWN_HOURS):
            n_skipped_cooldown += 1
            _audit({"event": "skip_cooldown", "strategy": strat, "current": cur, "recommended": rec, "hours_since_last": (now - last_ts).total_seconds() / 3600})
            continue

        # Apply (or dry-run record)
        record = {
            "event": "would_apply" if effective_dry_run else "applied",
            "strategy": strat,
            "key_used": cur_key,
            "action": action,
            "current_factor": cur,
            "new_factor": rec,
            "applied": not effective_dry_run,
            "reason": s.get("reason", ""),
        }
        _audit(record)

        if not effective_dry_run:
            factors[cur_key] = rec
            n_applied += 1
            print(f"  APPLIED {strat}: {cur} -> {rec} (action={action})")
        else:
            print(f"  WOULD APPLY {strat}: {cur} -> {rec} (action={action}) [dry-run]")

    # Persist factors if any changes made
    if n_applied > 0:
        cfg["factors"] = factors
        cfg["last_updated"] = now.isoformat()
        cfg.setdefault("version", "v1_2026-04-28")
        _save_alloc(cfg)

    print(f"OK: considered={n_considered}, applied={n_applied} (dry_run={effective_dry_run}), "
          f"skipped_cooldown={n_skipped_cooldown}, skipped_manual_only={n_skipped_manual}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run regardless of config")
    args = parser.parse_args()
    sys.exit(main(force_dry_run=args.dry_run))
