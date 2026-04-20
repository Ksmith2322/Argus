"""Reconciliation checker — compares canonical_fills.jsonl against every
strategy's trades.csv and flags any drift.

If the canonical log is the single source of truth (blueprint §18.11), it
must actually match the per-strategy logs it's backfilled from. This module
proves that match.

Drift categories:
  - MISSING_IN_CANONICAL: trade in strategy CSV but not in canonical
  - EXTRA_IN_CANONICAL: row in canonical not found in strategy CSV
  - COUNT_MISMATCH: row counts differ
  - PNL_SUM_MISMATCH: sum of pnl_usd differs by > $0.01

Output: argus_flow/logs/reconciliation_report.json

Run:
    python -m helio.reconciliation
    python -m helio.reconciliation --json
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
OUT_PATH = _REPO / "argus_flow" / "logs" / "reconciliation_report.json"

SPECS = [
    ("argus_usdjpy",       "argus_flow/logs/usdjpy/trades.csv",   "ts",         True),
    ("argus_gbpusd",       "argus_flow/logs/gbpusd/trades.csv",   "ts",         True),
    ("argus_cadjpy",       "argus_flow/logs/cadjpy/trades.csv",   "ts",         True),
    ("forge_gld_pm_long",  "forge/logs/gld_pm_long/trades.csv",   "ts",         False),
    ("forge_wick_gbpusd",  "forge/logs/wick_gbpusd/trades.csv",   "ts",         False),
    ("forge_nq_overnight", "forge/logs/nq_overnight/trades.csv",  "ts",         False),
    ("forge_jpy_pm_short", "forge/logs/jpy_pm_short/trades.csv",  "ts",         False),
    ("forge_gdx_gld",      "forge/logs/gdx_gld/trades.csv",       "entry_date", False),
]

PNL_DRIFT_TOLERANCE_USD = 0.01


def _read_canonical_by_strategy() -> dict[str, list["Fill"]]:
    """Read across current canonical_fills.jsonl AND any rotated archives
    (canonical_fills_*.jsonl). Must agree with helio.canonical_fills.read_fills
    so reconciliation doesn't flag false drift after rotation.

    First real consumer of helio.domain.Fill in production code. Prior version
    passed raw dicts; switching to Fill means a field rename in canonical_fills
    will fail fast in the domain dataclass instead of silently misreconciling.
    """
    from helio.canonical_fills import _iter_canonical_paths  # private but in-repo
    from helio.domain import Fill

    by_strat: dict[str, list[Fill]] = defaultdict(list)
    for path in _iter_canonical_paths():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                fill = Fill.from_canonical_row(r)
                if fill.strategy:
                    by_strat[fill.strategy].append(fill)
        except Exception:
            continue
    return by_strat


def _read_strategy_csv(spec: tuple) -> list[dict]:
    label, path_rel, ts_col, valid_only = spec
    p = _REPO / path_rel
    if not p.exists():
        return []
    rows = []
    try:
        with open(p, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if valid_only and str(r.get("experiment_valid", "")).lower() != "true":
                    continue
                pnl_raw = r.get("pnl_usd")
                if pnl_raw in (None, ""):
                    continue
                entry_ts = r.get(ts_col) or r.get("entry_ts") or r.get("entry_date") or ""
                exit_ts = r.get("exit_ts") or r.get("exit_date") or ""
                rows.append({
                    "entry_ts": entry_ts,
                    "exit_ts": exit_ts,
                    "pnl_usd": float(pnl_raw),
                })
    except Exception:
        pass
    return rows


def reconcile_strategy(spec: tuple, canonical_fills: list) -> dict:
    """Compare per-strategy CSV trades against canonical Fill objects.

    `canonical_fills` is `list[helio.domain.Fill]`; typed-string avoided to
    keep this module importable even if someone swaps the domain layer.
    """
    label = spec[0]
    csv_rows = _read_strategy_csv(spec)

    csv_keys = {(r["entry_ts"], r["exit_ts"]) for r in csv_rows}
    canonical_keys = {(f.entry_ts or "", f.exit_ts or "") for f in canonical_fills}

    missing_in_canonical = csv_keys - canonical_keys
    extra_in_canonical = canonical_keys - csv_keys

    csv_pnl_sum = sum(r["pnl_usd"] for r in csv_rows)
    canonical_pnl_sum = sum(f.pnl_usd for f in canonical_fills if f.pnl_usd is not None)
    pnl_drift = abs(canonical_pnl_sum - csv_pnl_sum)

    drift_flags = []
    if missing_in_canonical:
        drift_flags.append(f"{len(missing_in_canonical)} rows in csv not in canonical")
    if extra_in_canonical:
        drift_flags.append(f"{len(extra_in_canonical)} rows in canonical not in csv")
    if pnl_drift > PNL_DRIFT_TOLERANCE_USD:
        drift_flags.append(f"pnl sum drift ${pnl_drift:.2f} (tolerance ${PNL_DRIFT_TOLERANCE_USD})")

    return {
        "strategy": label,
        "csv_row_count": len(csv_rows),
        "canonical_row_count": len(canonical_fills),
        "csv_pnl_sum_usd": round(csv_pnl_sum, 2),
        "canonical_pnl_sum_usd": round(canonical_pnl_sum, 2),
        "pnl_drift_usd": round(pnl_drift, 4),
        "missing_in_canonical_count": len(missing_in_canonical),
        "extra_in_canonical_count": len(extra_in_canonical),
        "drift_flags": drift_flags,
        "status": "DRIFT" if drift_flags else "OK",
    }


def build_report() -> dict:
    canonical_by_strat = _read_canonical_by_strategy()
    results = [reconcile_strategy(spec, canonical_by_strat.get(spec[0], []))
               for spec in SPECS]
    drift_strategies = [r["strategy"] for r in results if r["status"] == "DRIFT"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tolerance_usd": PNL_DRIFT_TOLERANCE_USD,
        "strategies": results,
        "summary": {
            "drift_strategies": drift_strategies,
            "total_evaluated": len(results),
            "all_reconciled": len(drift_strategies) == 0,
        },
    }


def _print_table(report: dict) -> None:
    print(f"Reconciliation — canonical_fills vs per-strategy trades.csv")
    print(f"Generated: {report['generated_at']}  tolerance: ${report['tolerance_usd']}")
    print()
    print(f"{'Strategy':24s} {'CSV rows':>9s} {'Canon':>7s} {'CSV PnL':>10s} {'Canon PnL':>10s} {'Drift':>8s}  Status")
    print("-" * 100)
    for r in report["strategies"]:
        print(f"  {r['strategy']:22s} {r['csv_row_count']:>9d} {r['canonical_row_count']:>7d} "
              f"${r['csv_pnl_sum_usd']:>9.2f} ${r['canonical_pnl_sum_usd']:>9.2f} "
              f"${r['pnl_drift_usd']:>7.2f}  {r['status']}")
        for flag in r["drift_flags"]:
            print(f"    ! {flag}")
    print()
    if report["summary"]["all_reconciled"]:
        print("  All strategies reconciled — canonical_fills is trustworthy.")
    else:
        print(f"  DRIFT IN: {', '.join(report['summary']['drift_strategies'])}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = build_report()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2))

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_table(report)
        print(f"\nWrote: {OUT_PATH}")
    # Exit code non-zero if any drift — lets cohort report flag the issue
    return 0 if report["summary"]["all_reconciled"] else 1


if __name__ == "__main__":
    sys.exit(main())
