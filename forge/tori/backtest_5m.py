"""Tori 5-minute sibling backtest.

Runs Tori's post-v2 rules on 5-minute bars. Differs from 4H and 1H in two
hard ways:

1. **yfinance caps 5m data at 60 days** (not 730 like 1h). Sample window
   is a 1/12 of the 1H sibling's window. Expect a smaller trade count.
2. **Intrabar noise is much higher at 5m.** Tori herself says 5m is for
   Apex prop-firm compliance (forced day-trading) and requires more skill
   — the same rules produce more decisions per day, each with shorter
   hold times. If the edge survives at 5m, it's strong evidence of TF
   invariance; if it doesn't, the 4H edge is regime-scoped to slower bars.

Output: strategy_confidence/tori_5m.json, independent of 4H and 1H.

Run:
    python -m forge.tori.backtest_5m
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

OUT_CSV = _REPO / "forge" / "logs" / "tori" / "backtest_trades_5m.csv"
OUT_ARTIFACT = _REPO / "strategy_confidence" / "tori_5m.json"


def run_5m_backtest() -> dict:
    from forge.tori.runner import (
        download_data, compute_atr, run_backtest, TICKERS, STARTING_EQUITY,
    )

    datasets = {}
    for ticker in TICKERS:
        try:
            df = download_data(ticker, period="60d", interval="5m")
        except Exception as e:
            print(f"  {ticker}: download failed ({e})")
            continue
        df = compute_atr(df)
        datasets[ticker] = df
        print(f"  {ticker}: {len(df)} 5m bars")

    if not datasets:
        print("No datasets loaded.")
        return {"n_trades_with_pnl": 0}

    print(f"Running backtest on 5m bars (equity=${STARTING_EQUITY:,.0f})...")
    trades, equity_curve, final_equity = run_backtest(datasets, equity=STARTING_EQUITY)

    if trades:
        OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
        keys = trades[0].keys()
        with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(trades)
        print(f"Wrote CSV: {OUT_CSV}")

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
    }
    print(f"\nSummary: {summary}")
    return summary


def build_artifact() -> dict:
    from helio.fleet_state import (
        _bootstrap_p_positive, _evidence_bar, _monte_carlo_shuffle,
        _walk_forward_stability, _cost_stress, _top_n_sensitivity,
        _per_group_profitability,
    )

    if not OUT_CSV.exists():
        raise FileNotFoundError(f"missing {OUT_CSV}")

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
        "5m sibling of canonical 4H Tori — 60-day yfinance cap means smaller sample",
    ]
    if sum(pnls) < 0:
        warn.append("union negative -- 5m does NOT hold Tori's edge at her lowest stated TF")
    else:
        warn.append(f"union positive: PF={pf:.2f}")
    warn.append("ABSOLUTE PNL likely inflated by uncapped equity compounding -- read PF/WR only")

    artifact = {
        "schema_version": 1,
        "strategy": "tori_5m",
        "source": "tori_backtest_5m_60d_no_bounce",
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
    summary = run_5m_backtest()
    if summary["n_trades_with_pnl"] == 0:
        print("No trades produced; skipping artifact.")
        return 1
    artifact = build_artifact()
    print(json.dumps({
        "bt_pf": artifact["bt_pf"],
        "bt_wr": artifact["bt_wr"],
        "bt_trades": artifact["bt_trades"],
        "p_expectancy_positive": artifact["p_expectancy_positive"],
        "walk_forward_stability": (artifact.get("walk_forward") or {}).get("stability_score"),
        "per_instrument_all_positive": (artifact.get("per_instrument") or {}).get("all_positive"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
