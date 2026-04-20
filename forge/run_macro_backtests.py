"""Run-once helper: persist VIX Mean-Reversion + Sector Rotation backtests
to CSV so the confidence writers can read a cached tradelist instead of
re-running slow yfinance calls each time.

Outputs:
  - forge/data/vix_revert/backtest_trades.csv
  - forge/data/sector_rot/backtest_trades.csv

The underlying backtests hit yfinance and take 20+ seconds each, so the
default behavior is to skip if the CSV already exists. Pass `--refresh`
to force a re-run.

Invoke:
    python -m forge.run_macro_backtests
    python -m forge.run_macro_backtests --refresh
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

VIX_CSV = _REPO / "forge" / "data" / "vix_revert" / "backtest_trades.csv"
SECTOR_CSV = _REPO / "forge" / "data" / "sector_rot" / "backtest_trades.csv"

# Default backtest window — chosen to include 2020 COVID crash (biggest
# VIX-revert test) through current date. Keep aligned with the defaults
# on VixMeanReversion.backtest / SectorRotation.backtest staticmethods.
_START = "2020-01-01"
_END = "2026-04-01"


def _write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def run_vix(refresh: bool) -> Path:
    if VIX_CSV.exists() and not refresh:
        print(f"  [skip] {VIX_CSV} exists — use --refresh to re-run")
        return VIX_CSV
    from forge.macro_strategies import VixMeanReversion
    result = VixMeanReversion.backtest(_START, _END)
    detail = result.get("detail") or []
    if not detail:
        raise RuntimeError(
            "VixMeanReversion.backtest returned no detail — yfinance may be "
            "down or the strategy found no trades in the backtest window."
        )
    columns = [
        "entry_date", "exit_date", "vix_at_entry", "entry_price",
        "exit_price", "return_pct", "days_held", "exit_reason",
    ]
    _write_csv(VIX_CSV, detail, columns)
    print(f"  [ok]   wrote {len(detail)} trades -> {VIX_CSV}")
    return VIX_CSV


def run_sector(refresh: bool) -> Path:
    if SECTOR_CSV.exists() and not refresh:
        print(f"  [skip] {SECTOR_CSV} exists — use --refresh to re-run")
        return SECTOR_CSV
    from forge.macro_strategies import SectorRotation
    result = SectorRotation.backtest(_START, _END)
    detail = result.get("detail") or []
    if not detail:
        raise RuntimeError(
            "SectorRotation.backtest returned no detail — yfinance may be "
            "down or the strategy produced no monthly records."
        )
    # "holdings" is a list — flatten to a pipe-joined string for CSV storage.
    normalized = []
    for r in detail:
        row = dict(r)
        holdings = row.get("holdings")
        if isinstance(holdings, list):
            row["holdings"] = "|".join(holdings)
        normalized.append(row)
    columns = ["date", "regime", "holdings", "month_return", "cumulative"]
    _write_csv(SECTOR_CSV, normalized, columns)
    print(f"  [ok]   wrote {len(normalized)} months -> {SECTOR_CSV}")
    return SECTOR_CSV


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true",
                    help="Re-run backtests even if cached CSVs exist")
    args = ap.parse_args()

    print("Running macro backtests (VIX Revert + Sector Rotation)...")
    run_vix(args.refresh)
    run_sector(args.refresh)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
