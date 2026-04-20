"""Tori 1-hour sibling backtest.

Runs the same Tori strategy (post-v2 exit logic, bounces disabled) on
1-hour bars instead of the canonical 4-hour. Produces an independent
artifact at strategy_confidence/tori_1h.json so dashboard consumers can
distinguish the 4H validated result from the 1H sibling validation.

Design note: Tori's rules are claimed to be TF-invariant per her teaching
(same rules, different bar size), but that's her marketing — each TF
needs independent backtest proof. This script provides that for 1H.

yfinance caps 1h data at 730 days, same as the 4H canonical window.

Run:
    python -m forge.tori.backtest_1h
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

OUT_CSV = _REPO / "forge" / "logs" / "tori" / "backtest_trades_1h.csv"
OUT_ARTIFACT = _REPO / "strategy_confidence" / "tori_1h.json"


def run_1h_backtest() -> dict:
    """Same runner pipeline, but stays on 1H bars (no 4H resample).

    Returns: {n_trades, pf, wr, total_pnl, csv_path}
    """
    from forge.tori.runner import (
        download_data, compute_atr, run_backtest, TICKERS, STARTING_EQUITY,
    )

    datasets = {}
    for ticker in TICKERS:
        df = download_data(ticker, period="2y", interval="1h")
        # Skip the 4H resample — keep 1H bars
        df = compute_atr(df)
        datasets[ticker] = df
        print(f"  {ticker}: {len(df)} 1H bars")

    print(f"Running backtest on 1H bars (equity=${STARTING_EQUITY:,.0f})...")
    trades, equity_curve, final_equity = run_backtest(datasets, equity=STARTING_EQUITY)

    # Save CSV
    if trades:
        OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
        keys = trades[0].keys()
        with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(trades)
        print(f"Wrote CSV: {OUT_CSV}")

    # Stats
    pnls = [float(t["pnl_usd"]) for t in trades if float(t["pnl_usd"]) != 0]
    wins = sum(1 for p in pnls if p > 0)
    gross_w = sum(p for p in pnls if p > 0)
    gross_l = abs(sum(p for p in pnls if p < 0))
    pf = gross_w / gross_l if gross_l else float("inf")

    summary = {
        "n_trades_with_pnl": len(pnls),
        "win_rate": round(wins / len(pnls), 4) if pnls else 0.0,
        "profit_factor": round(pf, 4) if pf != float("inf") else None,
        "total_pnl_usd": round(sum(pnls), 2),
        "final_equity_usd": round(final_equity, 2),
    }
    print(f"\nSummary: {summary}")
    return summary


def build_artifact() -> dict:
    """Build a Tori_1H artifact from the just-written 1H CSV."""
    from helio.fleet_state import (
        _bootstrap_p_positive, _evidence_bar, _monte_carlo_shuffle,
        _walk_forward_stability, _cost_stress, _top_n_sensitivity,
        _per_group_profitability,
    )

    if not OUT_CSV.exists():
        raise FileNotFoundError(f"missing {OUT_CSV} — run run_1h_backtest first")

    with open(OUT_CSV, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if float(r["pnl_usd"]) != 0]

    n = len(rows)
    pnls = [float(r["pnl_usd"]) for r in rows]
    r_mults = [float(r["r_multiple"]) for r in rows if r.get("r_multiple") not in (None, "")]
    wins = sum(1 for p in pnls if p > 0)
    gross_w = sum(p for p in pnls if p > 0)
    gross_l = abs(sum(p for p in pnls if p < 0))
    pf = gross_w / gross_l if gross_l else float("inf")

    warn = [
        f"{n} backfill, 0 live",
        "1H sibling of canonical 4H Tori -- independent backtest, not inheriting 4H's proof",
    ]
    if sum(pnls) < 0:
        warn.append("union negative -- 1H does NOT generalize as Tori claims")
    else:
        warn.append(f"union positive: PF={pf:.2f}")
    # 1H at 3000+ trades over 2 years produces extreme compounding on the
    # uncapped equity-based sizing model — absolute PnL in the CSV is not
    # realistically achievable live. PF/WR/distribution metrics remain valid
    # as relative-edge indicators.
    warn.append("ABSOLUTE PNL INFLATED: uncapped equity compounding over 3000+ trades -- read PF/WR only")

    artifact = {
        "schema_version": 1,
        "strategy": "tori_1h",
        "source": "tori_backtest_1h_2y_no_bounce",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bt_pf": round(pf, 2) if pf != float("inf") else None,
        "bt_wr": round(wins / n, 4) if n else 0.0,
        "bt_trades": n,
        "n_total": n,
        "n_live": 0,
        "n_paper": n,
        "expectancy_usd": round(sum(pnls) / n, 4) if n else None,
        "expectancy_r": round(sum(r_mults) / len(r_mults), 4) if r_mults else None,
        "p_expectancy_positive": _bootstrap_p_positive(pnls) if n >= 10 else None,
        "evidence_bar": _evidence_bar(n),
        "sample_warning": " | ".join(warn),
    }
    mc = _monte_carlo_shuffle(pnls)
    if mc is not None:
        artifact["mc_stress"] = mc
    wf = _walk_forward_stability(pnls, n_folds=4)
    if wf is not None:
        pfs = [f["profit_factor"] for f in wf["fold_details"]
               if f.get("profit_factor") is not None]
        artifact["walk_forward"] = {
            "folds": wf["n_folds"], "stable_folds": wf["positive_folds"],
            "pf_per_fold": pfs, **wf,
        }
    cs = _cost_stress(pnls, cost_per_trade_usd=5.0)
    if cs is not None:
        artifact["cost_stress"] = cs
    tns = _top_n_sensitivity(pnls, max_n=5)
    if tns is not None:
        artifact["top_n_sensitivity"] = tns
    per_inst = _per_group_profitability(rows, group_key="name")
    if per_inst is not None:
        artifact["per_instrument"] = per_inst

    OUT_ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    OUT_ARTIFACT.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(f"Wrote artifact: {OUT_ARTIFACT}")
    return artifact


def main() -> int:
    summary = run_1h_backtest()
    if summary["n_trades_with_pnl"] == 0:
        print("No trades produced; skipping artifact.")
        return 1
    artifact = build_artifact()
    print(json.dumps({
        "bt_pf": artifact["bt_pf"],
        "bt_trades": artifact["bt_trades"],
        "p_expectancy_positive": artifact["p_expectancy_positive"],
        "walk_forward_stability": (artifact.get("walk_forward") or {}).get("stability_score"),
        "per_instrument_all_positive": (artifact.get("per_instrument") or {}).get("all_positive"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
