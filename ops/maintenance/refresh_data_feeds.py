"""Scheduled cache refresh for all production data-feed contracts.

Companion to `helio.data_feed_contract` (Codex gap #2). This is the
script that keeps the offline CSV cache fresh enough that
`run_data_feed_contracts` stays GREEN.

USAGE
-----
    # Refresh every contracted universe (default: incremental)
    python -m ops.maintenance.refresh_data_feeds

    # Force a full re-pull (e.g. when investigating cache corruption)
    python -m ops.maintenance.refresh_data_feeds --refresh

    # Refresh just one strategy's universe
    python -m ops.maintenance.refresh_data_feeds --only forge_xs_momentum

WHEN TO RUN
-----------
- Manual: before any monthly rebalance (xs_momentum), before a GLD
  entry-decision evaluation, or after a long market closure.
- Scheduled: 22:00 UTC daily via Windows Task Scheduler (see
  docs/runbooks/data_feed_runbook.md). Runs after the US close so the
  most recent bar is included.

EXIT CODES
----------
  0 — every contract is GREEN after refresh
  1 — at least one contract still YELLOW after refresh
  2 — at least one contract still RED (live data source is broken or
      offline; this is operator-actionable)

Output report: ops/reports/system_audit/data_feed_refresh.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


def _refresh_universe(tickers: list[str], *, refresh: bool) -> list[dict]:
    """Pull every ticker via helio.yfinance_data.fetch_universe and
    return per-ticker outcome rows."""
    from helio.yfinance_data import fetch_universe
    results = fetch_universe(tickers, refresh=refresh)
    return [
        {
            "ticker": r.ticker,
            "rows_fetched": r.rows_fetched,
            "rows_total": r.rows_total,
            "cache_path": str(r.cache_path) if r.cache_path else None,
            "incremental": r.incremental,
            "elapsed_s": round(r.elapsed_s, 2),
            "succeeded": r.cache_path is not None and r.rows_total > 0,
        }
        for r in results
    ]


def _exit_code(verdicts: dict[str, dict]) -> int:
    has_red = any(v["verdict"] == "RED" for v in verdicts.values())
    has_yellow = any(v["verdict"] == "YELLOW" for v in verdicts.values())
    if has_red:
        return 2
    if has_yellow:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh", action="store_true",
        help="Force full re-pull (default is incremental)",
    )
    parser.add_argument(
        "--only", default=None,
        help="Refresh only this contract (default: all)",
    )
    parser.add_argument(
        "--no-verify", action="store_true",
        help="Skip post-refresh contract verification",
    )
    args = parser.parse_args(argv)

    from helio.data_feed_contract import (
        CONTRACTS,
        verify_all_contracts,
    )

    if args.only:
        if args.only not in CONTRACTS:
            print(f"ERROR: unknown contract {args.only!r}; "
                  f"known: {list(CONTRACTS)}")
            return 2
        contracts = {args.only: CONTRACTS[args.only]}
    else:
        contracts = dict(CONTRACTS)

    t0 = time.perf_counter()
    refresh_log: dict[str, list[dict]] = {}
    universe_seen: set[str] = set()
    for name, contract in contracts.items():
        # De-dupe tickers shared across contracts (e.g. GLD)
        new_tickers = [
            t for t in contract.universe if t not in universe_seen
        ]
        universe_seen.update(new_tickers)
        if not new_tickers:
            refresh_log[name] = []
            continue
        refresh_log[name] = _refresh_universe(new_tickers, refresh=args.refresh)

    elapsed = round(time.perf_counter() - t0, 2)

    verdicts = {} if args.no_verify else verify_all_contracts()

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": elapsed,
        "refresh_mode": "full" if args.refresh else "incremental",
        "contracts_refreshed": list(contracts.keys()),
        "refresh_log": refresh_log,
        "post_refresh_verdicts": verdicts,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "data_feed_refresh.json"
    out_path.write_text(
        json.dumps(report, indent=2, default=str),
        encoding="utf-8",
    )

    # Operator summary
    print(f"Refreshed {len(contracts)} contract(s) in {elapsed}s "
          f"(mode={'full' if args.refresh else 'incremental'})")
    for name, rows in refresh_log.items():
        ok = sum(1 for r in rows if r["succeeded"])
        print(f"  {name}: {ok}/{len(rows)} tickers ok")
    if verdicts:
        print("Post-refresh verdicts:")
        for name, v in verdicts.items():
            print(f"  {name}: {v['verdict']}  "
                  f"({'; '.join(v['reasons']) or 'clean'})")
    print(f"Report: {out_path}")

    return _exit_code(verdicts) if verdicts else 0


if __name__ == "__main__":
    sys.exit(main())
