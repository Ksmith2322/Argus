"""Run parameter sweeps on all instruments with available historical data.

Usage:
    python -m argus_flow.backtest.run_all_sweeps
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from argus_flow.backtest.engine import run_sweep

REPO = Path(__file__).resolve().parents[2]
RESULTS_DIR = REPO / "argus_flow" / "backtest" / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# Map: config -> data file -> sweep grid
JOBS = [
    # FX pairs
    {"config": "argus_flow/configs/gbpusd_range_paper_v1.json", "data": "argus_flow/data/ibkr_gbpusd_1m.csv", "sweep": "argus_flow/backtest/sweep_fx.json"},
    {"config": "argus_flow/configs/eurjpy_t4_paper_v1.json", "data": "argus_flow/data/ibkr_eurjpy_1m.csv", "sweep": "argus_flow/backtest/sweep_fx.json"},
    {"config": "argus_flow/configs/gbpjpy_t4_paper_v1.json", "data": "argus_flow/data/ibkr_gbpjpy_1m.csv", "sweep": "argus_flow/backtest/sweep_fx.json"},
    {"config": "argus_flow/configs/cadjpy_t4_paper_v1.json", "data": "argus_flow/data/ibkr_cadjpy_1m.csv", "sweep": "argus_flow/backtest/sweep_fx.json"},
    {"config": "argus_flow/configs/audjpy_t4_paper_v1.json", "data": "argus_flow/data/ibkr_audjpy_1m.csv", "sweep": "argus_flow/backtest/sweep_fx.json"},
    {"config": "argus_flow/configs/usdjpy_ny_paper_v1.json", "data": "argus_flow/data/ibkr_usdjpy_1m.csv", "sweep": "argus_flow/backtest/sweep_fx.json"},
    {"config": "argus_flow/configs/audusd_ny_paper_v1.json", "data": "argus_flow/data/ibkr_audusd_1m.csv", "sweep": "argus_flow/backtest/sweep_fx.json"},
    # Futures (use existing data files)
    {"config": "argus_flow/configs/mes_range_paper_v1.json", "data": "argus_flow/data/ibkr_MES_sp500_micro_1m.csv", "sweep": "argus_flow/backtest/sweep_futures.json"},
    {"config": "argus_flow/configs/mnq_range_paper_v1.json", "data": "argus_flow/data/ibkr_MNQ_nasdaq_micro_1m.csv", "sweep": "argus_flow/backtest/sweep_futures.json"},
    {"config": "argus_flow/configs/mym_range_paper_v1.json", "data": "argus_flow/data/ibkr_MYM_dow_micro_1m.csv", "sweep": "argus_flow/backtest/sweep_futures.json"},
]


def main():
    summary = []
    total_start = time.time()

    for job in JOBS:
        cfg_path = REPO / job["config"]
        data_path = REPO / job["data"]
        sweep_path = REPO / job["sweep"]

        if not cfg_path.exists():
            print(f"SKIP: config not found: {cfg_path.name}")
            continue
        if not data_path.exists():
            print(f"SKIP: data not found: {data_path.name}")
            continue

        config = json.loads(cfg_path.read_text())
        bars = pd.read_csv(data_path)
        param_grid = json.loads(sweep_path.read_text())
        symbol = config.get("symbol", "???")

        print(f"\n{'='*70}")
        print(f"SWEEP: {symbol} / {config.get('strategy', '?')} ({len(bars)} bars, {len(list(__import__('itertools').product(*param_grid.values())))} combos)")
        print(f"{'='*70}")

        t0 = time.time()
        try:
            results = run_sweep(config, bars, param_grid)
        except Exception as e:
            print(f"  ERROR: {symbol} sweep failed: {e}")
            summary.append({"symbol": symbol, "strategy": config.get("strategy", "?"),
                            "bars": len(bars), "best_pf": 0, "best_wr": 0, "best_exp": 0,
                            "best_params": {}, "best_count": 0, "combos": 0, "elapsed_s": 0, "error": str(e)})
            continue
        elapsed = time.time() - t0

        # Save results with provenance
        out_file = RESULTS_DIR / f"sweep_{symbol.lower()}.json"
        provenance = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol, "strategy": config.get("strategy", "?"),
            "config_path": str(job["config"]), "data_path": str(job["data"]),
            "bars": len(bars), "combos": len(results),
            "elapsed_s": round(elapsed),
        }
        out_file.write_text(json.dumps({"provenance": provenance, "results": results}, indent=2) + "\n")

        # Print top 5
        print(f"\nTop 5 for {symbol} ({elapsed:.0f}s):")
        print(f"{'PF':>8} {'WR':>6} {'Exp':>8} {'Trades':>6} {'DD':>8}  Params")
        print("-" * 70)
        for r in results[:5]:
            p = " ".join(f"{k.split('.')[-1]}={v}" for k, v in r["params"].items())
            print(f"{r['profit_factor']:>8.3f} {r['win_rate']:>5.1%} {r['expectancy']:>+8.3f} "
                  f"{r['count']:>6} {r['max_drawdown']:>8.2f}  {p}")

        # Filter: only consider results with >= 10 trades for "best"
        viable = [r for r in results if r["count"] >= 10]
        best = viable[0] if viable else (results[0] if results else None)
        summary.append({
            "symbol": symbol,
            "strategy": config.get("strategy", "?"),
            "bars": len(bars),
            "best_pf": best["profit_factor"] if best else 0,
            "best_wr": best["win_rate"] if best else 0,
            "best_exp": best["expectancy"] if best else 0,
            "best_count": best["count"] if best else 0,
            "best_params": best["params"] if best else {},
            "combos": len(results),
            "elapsed_s": round(elapsed),
        })

    # Final summary
    total_elapsed = time.time() - total_start
    print(f"\n{'='*70}")
    print(f"SWEEP SUMMARY — {len(summary)} instruments, {total_elapsed/60:.0f} min total")
    print(f"{'='*70}")
    print(f"{'Symbol':<10} {'Strategy':<18} {'Best PF':>8} {'WR':>6} {'Exp':>8}  Optimal Params")
    print("-" * 90)
    for s in sorted(summary, key=lambda x: x["best_pf"], reverse=True):
        p = " ".join(f"{k.split('.')[-1]}={v}" for k, v in s["best_params"].items())
        print(f"{s['symbol']:<10} {s['strategy']:<18} {s['best_pf']:>8.3f} {s['best_wr']:>5.1%} {s['best_exp']:>+8.3f}  {p}")

    # Save summary
    summary_file = RESULTS_DIR / "sweep_summary.json"
    summary_file.write_text(json.dumps(summary, indent=2) + "\n")
    print(f"\nSummary saved to {summary_file}")


if __name__ == "__main__":
    main()
