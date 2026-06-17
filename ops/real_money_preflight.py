"""ops/real_money_preflight.py — operator CLI for the 12-point preflight.

Diagnoses each strategy's readiness for real-money allocation by running
all 12 checks defined in helio.real_money_preflight. READ-ONLY: does
not write files or mutate allocation.

USAGE:
    python -m ops.real_money_preflight                    # all active strategies
    python -m ops.real_money_preflight --strategy=forge_xs_momentum
    python -m ops.real_money_preflight --json             # JSON output
    python -m ops.real_money_preflight --n-min=30         # tighter live threshold

EXIT CODES:
    0  all evaluated strategies are READY_FOR_REAL
    1  at least one strategy is BLOCKED_PENDING_REVIEW (YELLOWs only)
    2  at least one strategy is BLOCKED (any REDs)

INTENT

Run this on a manual cadence (e.g. before each weekly review) and
when considering a real-money allocation flip. The output is the
authoritative "are we ready?" verdict — supersedes the eyeball check
of allocation_factors.json + scattered audit reports.

The recommend-then-apply pattern from ops/auto_pause.py is intentionally
NOT replicated here. real-money decisions stay 100% manual; this CLI
just compiles the evidence.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


def _all_active_strategies() -> list[str]:
    """The list to evaluate by default: every strategy with allocation > 0
    PLUS any strategy in the ACTIVE_ROSTER test pin (so flagged-but-not-yet-
    flipped strategies also get evaluated)."""
    out: set[str] = set()
    try:
        from argus_flow.tests.test_sunset_roster import ACTIVE_ROSTER
        out.update(ACTIVE_ROSTER)
    except Exception:
        pass
    try:
        path = REPO / "argus_flow" / "configs" / "allocation_factors.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        for s, f in (cfg.get("factors") or {}).items():
            try:
                if float(f) > 0.0:
                    out.add(s)
            except (TypeError, ValueError):
                continue
    except Exception:
        pass
    # Stable order: sort
    return sorted(out)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", action="append", default=None,
                          help="Strategy label to evaluate (repeatable). "
                               "Default: every active + ACTIVE_ROSTER strategy.")
    parser.add_argument("--n-min", type=int, default=20,
                          help="Minimum live trade count threshold (default 20)")
    parser.add_argument("--capacity-multiplier", type=float, default=2.0,
                          help="Capacity headroom target (default 2.0×)")
    parser.add_argument("--heartbeat-max-hours", type=float, default=24.0,
                          help="Max heartbeat age in hours (default 24)")
    parser.add_argument("--json", action="store_true",
                          help="Emit JSON instead of human-readable text")
    parser.add_argument("--save", action="store_true",
                          help="Also write report to ops/reports/system_audit/")
    args = parser.parse_args(argv)

    from helio.real_money_preflight import (
        evaluate_strategy, render_strategy_report,
    )

    strategies = args.strategy if args.strategy else _all_active_strategies()
    if not strategies:
        print("ERROR: no strategies to evaluate.", file=sys.stderr)
        return 2

    results: list[dict] = []
    rendered: list[str] = []
    any_red = False
    any_yellow = False
    for s in strategies:
        try:
            p = evaluate_strategy(
                s, n_min=args.n_min,
                capacity_multiplier=args.capacity_multiplier,
                heartbeat_max_age_hours=args.heartbeat_max_hours,
            )
        except Exception as exc:
            results.append({"strategy": s, "error": str(exc)})
            continue
        results.append(p.to_dict())
        rendered.append(render_strategy_report(p))
        if p.n_red > 0:
            any_red = True
        elif p.n_yellow > 0:
            any_yellow = True

    if args.json:
        print(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "n_strategies": len(results),
            "results": results,
        }, indent=2, default=str))
    else:
        print("=" * 80)
        print("REAL-MONEY PREFLIGHT  (12-point promotion-grade audit card)")
        print("=" * 80)
        for r in rendered:
            print()
            print(r)
        print()
        print("=" * 80)
        # Cohort summary
        readys = [r for r in results
                    if r.get("verdict") == "READY_FOR_REAL"]
        pending = [r for r in results
                     if r.get("verdict") == "BLOCKED_PENDING_REVIEW"]
        blocked = [r for r in results if r.get("verdict") == "BLOCKED"]
        print(f"READY_FOR_REAL ({len(readys)}): "
              f"{[r['strategy'] for r in readys]}")
        print(f"BLOCKED_PENDING_REVIEW ({len(pending)}): "
              f"{[r['strategy'] for r in pending]}")
        print(f"BLOCKED ({len(blocked)}): {[r['strategy'] for r in blocked]}")

    if args.save:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        json_path = OUT_DIR / f"real_money_preflight_{ts}.json"
        json_path.write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "n_strategies": len(results),
            "results": results,
        }, indent=2, default=str), encoding="utf-8")

        md_path = OUT_DIR / "real_money_preflight.md"
        md_lines = []
        md_lines.append("# Real-money preflight — 12-point audit card")
        md_lines.append("")
        md_lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
        md_lines.append("")
        for r in results:
            if "error" in r:
                md_lines.append(f"## {r['strategy']} — ERROR")
                md_lines.append("")
                md_lines.append(f"`{r['error']}`")
                continue
            md_lines.append(f"## {r['strategy']} — {r['verdict']}")
            md_lines.append("")
            md_lines.append(f"GREEN={r['n_green']}, YELLOW={r['n_yellow']}, "
                             f"RED={r['n_red']}")
            md_lines.append("")
            md_lines.append("| Check | Verdict | Reason |")
            md_lines.append("|---|---|---|")
            for c in r.get("checks", []):
                md_lines.append(
                    f"| {c['name']} | {c['verdict']} | {c['reason']} |"
                )
            md_lines.append("")
        md_path.write_text("\n".join(md_lines), encoding="utf-8")
        if not args.json:
            print(f"\nJSON: {json_path}")
            print(f"Markdown: {md_path}")

    if any_red:
        return 2
    if any_yellow:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
