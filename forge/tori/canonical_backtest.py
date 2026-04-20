"""Tori canonical backtest — reproducible reference run.

Freezes the exact configuration that produced the 2026-04-19 strong
artifact (PF 2.21, 665 trades, P(exp>0)=1.000):

  - Period: 2 years (yfinance 1-hour data, resampled to 4H)
  - Instruments: Platinum, Crude Oil, Gold, Dow (the 4 default tickers)
  - Exit logic: v2 trend-line trail (not the flat-swing trail)
  - Setups: break + break_retest (bounce is disabled per TORI_ENABLE_BOUNCE)
  - Starting equity: $10,000

Run:
    python -m forge.tori.canonical_backtest

This writes a timestamped CSV + JSON summary so you can re-verify the
reference result later without it drifting when runner defaults change.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

CANONICAL_CSV = _REPO / "forge" / "logs" / "tori" / "backtest_trades_canonical_2y.csv"
CANONICAL_SUMMARY = _REPO / "forge" / "logs" / "tori" / "canonical_2y_summary.json"


def _git_sha_short() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_REPO), stderr=subprocess.DEVNULL,
        )
        return out.decode("ascii", errors="replace").strip() or "unknown"
    except Exception:
        return "unknown"


def run_canonical() -> dict:
    """Run the frozen canonical backtest, preserving the config that produced
    the reference artifact. Returns a summary dict and writes the CSV +
    summary JSON to disk."""
    # Import runner fresh so any TORI_ENABLE_BOUNCE override from the caller's
    # environment is reflected
    from forge.tori import runner as tori_runner
    from forge.tori.runner import (
        download_data, resample_to_4h, compute_atr, run_backtest,
        TICKERS, STARTING_EQUITY, LOG_DIR,
    )

    if tori_runner.TORI_ENABLE_BOUNCE:
        print("WARNING: TORI_ENABLE_BOUNCE=True. Canonical run expects False.")
        print("         The reference artifact was produced with bounces disabled.")

    datasets = {}
    for ticker in TICKERS:
        df = download_data(ticker, period="2y", interval="1h")
        df = resample_to_4h(df)
        df = compute_atr(df)
        datasets[ticker] = df

    trades, equity_curve, final_equity = run_backtest(datasets, equity=STARTING_EQUITY)

    # Stats
    pnls = [float(t["pnl_usd"]) for t in trades if float(t["pnl_usd"]) != 0]
    wins = sum(1 for p in pnls if p > 0)
    gross_w = sum(p for p in pnls if p > 0)
    gross_l = abs(sum(p for p in pnls if p < 0))
    pf = gross_w / gross_l if gross_l else float("inf")

    summary = {
        "config": {
            "period": "2y",
            "interval_download": "1h",
            "interval_resample": "4h",
            "instruments": list(TICKERS),
            "starting_equity": STARTING_EQUITY,
            "bounce_enabled": bool(tori_runner.TORI_ENABLE_BOUNCE),
        },
        "git_sha": _git_sha_short(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_trades_with_pnl": len(pnls),
        "win_rate": round(wins / len(pnls), 4) if pnls else 0.0,
        "profit_factor": round(pf, 4) if pf != float("inf") else None,
        "total_pnl_usd": round(sum(pnls), 2),
        "final_equity_usd": round(final_equity, 2),
    }

    # Save canonical CSV + summary
    import csv as _csv
    if trades:
        CANONICAL_CSV.parent.mkdir(parents=True, exist_ok=True)
        keys = trades[0].keys()
        with open(CANONICAL_CSV, "w", newline="", encoding="utf-8") as f:
            writer = _csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(trades)

    CANONICAL_SUMMARY.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    summary = run_canonical()
    print(json.dumps(summary, indent=2))
    print(f"\nCanonical CSV:     {CANONICAL_CSV}")
    print(f"Canonical summary: {CANONICAL_SUMMARY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
