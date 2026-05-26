"""Run the nightly replay-vs-ledger health check for the active fleet.

Writes argus_flow/logs/replay_health.json that helio.ibkr_execution.submit_bracket
reads as a pre-trade fail-closed guard.

Usage:
    python -m ops.audit.run_replay_health_check
    python -m ops.audit.run_replay_health_check --strategies forge_xs_momentum,forge_gld_pm_long
    python -m ops.audit.run_replay_health_check --json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from helio.replay_health import (
    DEFAULT_HEALTH_PATH,
    SUPPORTED_STRATEGIES,
    compute_strategy_health,
    write_health_file,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategies", default="",
                        help="Comma-separated; defaults to all SUPPORTED_STRATEGIES")
    parser.add_argument("--period", default="1y",
                        help="Replay period (default 1y — enough for monthly strategies)")
    parser.add_argument("--out", default=str(DEFAULT_HEALTH_PATH),
                        help="Path to write replay_health.json")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    targets = (
        [s.strip() for s in args.strategies.split(",") if s.strip()]
        if args.strategies else list(SUPPORTED_STRATEGIES)
    )

    if not args.json:
        print(f"Running replay health check on {len(targets)} strategies "
              f"(period={args.period})...")
        print()

    results = []
    for strat in targets:
        if not args.json:
            print(f"  {strat:42s} ", end="", flush=True)
        h = compute_strategy_health(strat, period=args.period)
        results.append(h)
        if not args.json:
            badge = {
                "ok": "[OK]", "drift": "[DRIFT]",
                "warming": "[WARMING]", "error": "[ERR]",
            }.get(h.status, "[?]")
            print(f"{badge}  replay={h.n_replay_events:4d}  "
                  f"ledger={h.n_ledger_fills:4d}  "
                  f"matched={h.n_matched:4d}  "
                  f"mismatches={h.n_mismatches:3d}  "
                  f"blocking={h.n_blocking_mismatches:2d}"
                  + (f"  err={h.error}" if h.error else ""))

    write_health_file(results, path=Path(args.out))
    if not args.json:
        total_blocking = sum(h.n_blocking_mismatches for h in results)
        n_drift = sum(1 for h in results if h.status == "drift")
        n_warming = sum(1 for h in results if h.status == "warming")
        n_err = sum(1 for h in results if h.status == "error")
        n_ok = sum(1 for h in results if h.status == "ok")
        print()
        print(f"Summary: {n_ok} ok / {n_warming} warming / {n_drift} drift / "
              f"{n_err} error  (total blocking mismatches: {total_blocking})")
        print(f"Wrote: {args.out}")
    else:
        out = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "n_targets": len(targets),
            "results": [
                {"strategy": h.strategy, "status": h.status,
                 "n_blocking_mismatches": h.n_blocking_mismatches,
                 "error": h.error}
                for h in results
            ],
        }
        print(json.dumps(out, indent=2))

    # Exit code: 1 if any strategy is in drift, 2 if any errored, 0 if all ok
    if any(h.status == "drift" for h in results):
        return 1
    if any(h.status == "error" for h in results):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
