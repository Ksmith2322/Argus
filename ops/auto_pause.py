"""ops/auto_pause.py — operator CLI for the auto-pause loop.

Two modes:

1. **Default (recommend)**: snapshot the live gate, append to the cohort
   history, compute alerts, write recommendations. No allocation changes.
   This is the daily-cron mode — fires the Discord alert on fresh
   PAUSE_RECOMMENDED tiers.

       python -m ops.auto_pause

2. **--apply**: read the most recent recommendations file and convert
   PAUSE_RECOMMENDED entries into actual allocation_factor=0.0 flips.
   Requires --confirm to actually write — without it, prints what would
   change and exits 0 (dry-run). Logs the action to
   allocation_factors.json's `_kill_log` and fires a Discord
   confirmation.

       python -m ops.auto_pause --apply              # dry-run
       python -m ops.auto_pause --apply --confirm    # actually flip

Why a separate --apply step instead of fully automated?

The existing helio.auto_pause module deliberately keeps destructive
actions human-gated (see its docstring). That decision was made because
auto-pause based on live PF degradation is sensitive to:
  - small-n sample noise (n=20 is statistically thin)
  - regime artifacts (a brief drawdown != edge collapse)
  - data-source bugs we've been burned by before (the live_gate_monitor
    silent-fallback bug, fixed 2026-05-23)
This CLI gives the operator a structured way to act on the
recommendations without manually editing allocation_factors.json, while
still keeping the human in the loop.

Cron suggestion (Windows scheduled task):
  Daily at 22:00 UTC (post-market-close), run:
      C:\\Argus\\.venv\\Scripts\\python.exe -m ops.auto_pause
  This snapshots the day's gate verdicts and posts Discord if anyone
  crosses the threshold. Operator then runs `--apply --confirm` after
  reviewing the recommendations file.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


_REPO = Path(__file__).resolve().parents[1]
ALLOCATION_FACTORS_PATH = _REPO / "argus_flow" / "configs" / "allocation_factors.json"
RECOMMENDATIONS_PATH = _REPO / "argus_flow" / "logs" / "auto_pause_recommendations.json"
APPLY_EVENTS_PATH = _REPO / "argus_flow" / "logs" / "auto_pause_apply_events.jsonl"


def _load_recommendations() -> dict:
    if not RECOMMENDATIONS_PATH.exists():
        raise FileNotFoundError(
            f"no recommendations file at {RECOMMENDATIONS_PATH}. Run "
            f"`python -m ops.auto_pause` (without --apply) first to "
            f"snapshot the cohort gate."
        )
    return json.loads(RECOMMENDATIONS_PATH.read_text(encoding="utf-8"))


def _load_allocation_factors() -> dict:
    return json.loads(ALLOCATION_FACTORS_PATH.read_text(encoding="utf-8"))


def _save_allocation_factors(cfg: dict) -> None:
    ALLOCATION_FACTORS_PATH.write_text(
        json.dumps(cfg, indent=2, default=str), encoding="utf-8"
    )


def _append_event(event: dict) -> None:
    APPLY_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(APPLY_EVENTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")


def apply_recommendations(*, confirm: bool = False) -> dict:
    """Read the recommendations file, flip allocation_factor to 0.0 for
    every PAUSE_RECOMMENDED entry. Returns a structured result.

    When confirm=False, prints the planned changes and writes a
    NO_CHANGE_DRY_RUN event per candidate. When True, persists the
    allocation_factors.json + _kill_log entry + Discord confirmation.
    """
    recs = _load_recommendations()
    factors_cfg = _load_allocation_factors()
    factors = factors_cfg.get("factors", {})

    candidates = [
        a for a in recs.get("alerts", [])
        if a.get("alert_tier") == "PAUSE_RECOMMENDED"
    ]
    ts = datetime.now(timezone.utc).isoformat()
    actions: list[dict] = []
    any_change = False

    for a in candidates:
        strategy = a["strategy"]
        prior = float(factors.get(strategy, 1.0))
        action = {
            "ts": ts,
            "strategy": strategy,
            "prior_factor": prior,
            "consecutive_days": a.get("consecutive_days_at_verdict"),
            "verdict": a.get("current_verdict"),
            "live_pf": a.get("last_live_pf"),
            "ci_lower": a.get("baseline_ci_lower"),
        }
        if prior <= 0.0:
            action["action"] = "ALREADY_ZERO"
            action["new_factor"] = prior
            action["note"] = "PAUSE_RECOMMENDED but allocation already 0.0; no-op"
        elif not confirm:
            action["action"] = "DRY_RUN_WOULD_PAUSE"
            action["new_factor"] = 0.0
            action["note"] = "would flip on --confirm"
        else:
            factors[strategy] = 0.0
            any_change = True
            action["action"] = "PAUSED"
            action["new_factor"] = 0.0
            action["note"] = (f"flipped {prior} -> 0.0 via ops.auto_pause "
                                f"on {a.get('consecutive_days_at_verdict')} "
                                f"consecutive FAIL days")
        actions.append(action)
        _append_event(action)

    if any_change:
        # Persist + audit
        factors_cfg["factors"] = factors
        prior_log = factors_cfg.get("_kill_log") or []
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        paused = [act["strategy"] for act in actions if act["action"] == "PAUSED"]
        prior_log.append(
            f"{date_str}: AUTO-PAUSE-APPLIED — {', '.join(paused)} flipped to "
            f"0.0 via ops.auto_pause --apply --confirm after persistent FAIL "
            f"verdict from helio.live_gate_monitor / helio.auto_pause. "
            f"Operator must un-pause explicitly after a disciplined-gate "
            f"re-run at realistic slippage."
        )
        factors_cfg["_kill_log"] = prior_log
        factors_cfg["version"] = factors_cfg.get("version", "") + ".autopause_applied"
        factors_cfg["last_updated"] = ts
        _save_allocation_factors(factors_cfg)

        # Post Discord confirmation (best-effort)
        try:
            from ops.notify import send_discord
            lines = ["**AUTO-PAUSE APPLIED via `--apply --confirm`**"]
            for act in actions:
                if act["action"] == "PAUSED":
                    lines.append(
                        f"- `{act['strategy']}`: {act['prior_factor']} -> 0.0 "
                        f"({act['consecutive_days']} FAIL days, live_pf={act['live_pf']})"
                    )
            send_discord("\n".join(lines))
        except Exception:
            pass

    return {
        "generated_at": ts,
        "confirm": confirm,
        "n_candidates": len(candidates),
        "n_paused": sum(1 for a in actions if a["action"] == "PAUSED"),
        "n_dry_run": sum(1 for a in actions if a["action"] == "DRY_RUN_WOULD_PAUSE"),
        "n_already_zero": sum(1 for a in actions if a["action"] == "ALREADY_ZERO"),
        "actions": actions,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                          help="Apply pending PAUSE_RECOMMENDED entries to "
                               "allocation_factors.json (requires --confirm "
                               "for actual writes)")
    parser.add_argument("--confirm", action="store_true",
                          help="Required with --apply to actually flip allocations")
    parser.add_argument("--history-only", action="store_true",
                          help="(recommend mode) Don't snapshot, just compute alerts")
    parser.add_argument("--fail-days", type=int, default=5,
                          help="(recommend mode) Consecutive FAIL days before pause rec")
    parser.add_argument("--warning-days", type=int, default=3,
                          help="(recommend mode) Consecutive WARNING days before flag")
    args = parser.parse_args(argv)

    if args.apply:
        try:
            result = apply_recommendations(confirm=args.confirm)
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}")
            return 2
        print("=" * 72)
        print(f"AUTO-PAUSE --apply (confirm={args.confirm})")
        print("=" * 72)
        print(f"Candidates: {result['n_candidates']}, "
              f"paused: {result['n_paused']}, "
              f"dry-run-would-pause: {result['n_dry_run']}, "
              f"already-zero: {result['n_already_zero']}")
        for act in result["actions"]:
            tag = act["action"]
            print(f"  [{tag}] {act['strategy']}: "
                  f"prior={act['prior_factor']} -> new={act['new_factor']}  "
                  f"({act['note']})")
        if not args.confirm and result["n_dry_run"] > 0:
            print()
            print("This was a dry-run. To actually flip allocations, re-run with:")
            print("    python -m ops.auto_pause --apply --confirm")
        return 0

    # Recommend mode: defer to helio.auto_pause.run()
    from helio.auto_pause import run as _run, RECOMMENDATIONS_PATH as _RECS
    rc = _run(
        snapshot=not args.history_only,
        fail_days_for_pause=args.fail_days,
        warning_days_for_flag=args.warning_days,
    )
    # Pretty-print
    if _RECS.exists():
        data = json.loads(_RECS.read_text(encoding="utf-8"))
        counts = data.get("alert_tier_counts", {})
        print("=== Auto-pause recommendations ===")
        print(f"Tier counts: {counts}")
        for a in data.get("alerts", []):
            sym = {"OK": " ", "WARNING": "!", "PAUSE_RECOMMENDED": "X"}.get(
                a["alert_tier"], "?")
            print(f"  [{sym}] {a['strategy']:<28} {a['alert_tier']:<18} "
                  f"verdict={a['current_verdict']:<16} "
                  f"days={a['consecutive_days_at_verdict']}/{a['threshold_days']}")
            if a["alert_tier"] != "OK":
                print(f"      {a['recommendation']}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
