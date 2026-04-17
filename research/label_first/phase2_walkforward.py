"""Phase 2B: Walk-forward validation + 2-filter combinations + exit-mode sweep.

Tests the GBPUSD wick rule with the best single filters from Phase 2A,
combined pairwise, with 3 exit modes. 6-fold walk-forward (chronological,
non-overlapping). Only strategies that produce ≥30 trades per fold AND
positive expectancy in ≥4 of 6 folds count as out-of-sample valid.
"""
from __future__ import annotations

import json
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

from research.label_first.backtest_engine import backtest_long, stats
from research.label_first.data_fetch import fetch_cell
from research.label_first.features import compute_features
from research.label_first.phase2_combination import base_signal

REPORT_DIR = Path(__file__).resolve().parent / "reports"

# Top filter candidates from Phase 2A — features whose middle third lifted PF most
FILTER_CANDIDATES = [
    ("ema21_slope_atr", "middle"),  # PF 1.17 alone
    ("bb_width_pct", "below"),       # PF 1.13 alone
    ("rsi_14", "middle"),            # PF 1.08
    ("ema8_slope_atr", "middle"),    # PF 1.06
    ("ema8_dist_atr", "middle"),     # PF 1.02
    ("realized_vol_20", "above"),    # PF 1.00 — vol regime
    ("choppiness_14", "above"),      # PF 0.99 — high chop
]


def make_filter_mask(feats: pd.DataFrame, col: str, mode: str) -> np.ndarray:
    x = feats[col].values
    finite = np.isfinite(x)
    q33, q66 = np.nanpercentile(x[finite], [33, 67])
    if mode == "middle":
        return finite & (x >= q33) & (x <= q66)
    if mode == "below":
        return finite & (x < q33)
    if mode == "above":
        return finite & (x > q66)
    raise ValueError(mode)


def run_walkforward(
    df: pd.DataFrame,
    sig: np.ndarray,
    target_atr: float,
    stop_atr: float,
    hold_bars: int,
    exit_mode: str,
    n_folds: int = 6,
) -> dict:
    """Walk-forward by splitting data into n_folds chronological buckets;
    report stats per fold. NOTE: this is purely 'per-fold backtest' — no
    train/test optimization within folds since we're testing pre-specified rules."""
    fold_size = len(df) // n_folds
    fold_results = []
    for f in range(n_folds):
        s = f * fold_size
        e = (f + 1) * fold_size if f < n_folds - 1 else len(df)
        sub_df = df.iloc[s:e]
        sub_sig = sig[s:e]
        if sub_sig.sum() < 5:
            fold_results.append({"fold": f + 1, "n": int(sub_sig.sum()), "wr": None, "pf": None, "exp": None})
            continue
        trades = backtest_long(sub_df, sub_sig, target_atr, stop_atr, hold_bars, exit_mode=exit_mode)
        st = stats(trades)
        fold_results.append({"fold": f + 1, "start": str(sub_df.index[0].date()), "end": str(sub_df.index[-1].date()), **st})
    # Aggregate stats — only count folds with ≥10 trades
    valid = [f for f in fold_results if f.get("n", 0) >= 10 and f.get("exp") is not None]
    pos_folds = sum(1 for f in valid if f["exp"] > 0)
    return {
        "n_folds_total": n_folds,
        "n_folds_valid": len(valid),
        "n_folds_positive": pos_folds,
        "fold_details": fold_results,
        "median_pf": float(np.median([f["pf"] for f in valid])) if valid else None,
        "median_exp": float(np.median([f["exp"] for f in valid])) if valid else None,
    }


def main():
    df = fetch_cell("GBPUSD", "1d")
    feats = compute_features(df, intraday=False)
    sig_base = base_signal(df, uw_min=0.6, cp_max=0.3)

    # Test single filter + 2-filter combinations
    test_specs = [("BASE_only", [])]
    for f in FILTER_CANDIDATES:
        test_specs.append((f"{f[0]}({f[1]})", [f]))
    for f1, f2 in combinations(FILTER_CANDIDATES, 2):
        test_specs.append((f"{f1[0]}({f1[1]}) + {f2[0]}({f2[1]})", [f1, f2]))

    # Exit modes to try
    exit_configs = [
        ("OCO_1.0_0.5_h8", 1.0, 0.5, 8, "oco"),
        ("BE_1.0_0.5_h8", 1.0, 0.5, 8, "breakeven"),
        ("Trail_1.0_0.5_h8", 1.0, 0.5, 8, "trail"),
        ("OCO_2.0_1.0_h20", 2.0, 1.0, 20, "oco"),
        ("Trail_2.0_1.0_h20", 2.0, 1.0, 20, "trail"),
    ]

    results = []
    print(f"Testing {len(test_specs)} filter specs × {len(exit_configs)} exit configs = {len(test_specs)*len(exit_configs)} runs\n")
    for spec_name, filters in test_specs:
        sig = sig_base.copy()
        for col, mode in filters:
            sig = sig & make_filter_mask(feats, col, mode)
        for exit_name, tgt, stp, hold, mode in exit_configs:
            wf = run_walkforward(df, sig, tgt, stp, hold, mode)
            n_total = sum(f.get("n", 0) for f in wf["fold_details"])
            results.append({
                "spec": spec_name,
                "exit": exit_name,
                "n_total": n_total,
                "n_folds_valid": wf["n_folds_valid"],
                "n_folds_positive": wf["n_folds_positive"],
                "median_pf": wf["median_pf"],
                "median_exp": wf["median_exp"],
                "fold_pfs": [f.get("pf") for f in wf["fold_details"]],
            })

    out = pd.DataFrame(results)
    # Filter to specs with median PF defined
    valid = out[out.median_pf.notnull() & (out.n_total >= 60)].copy()
    valid["robust"] = (valid.n_folds_positive >= 4) & (valid.median_pf >= 1.10)
    valid = valid.sort_values(["robust", "median_pf"], ascending=[False, False]).reset_index(drop=True)

    valid.to_csv(REPORT_DIR / "phase2_walkforward.csv", index=False)
    print("Top 25 by median PF (filtered to >=60 total trades):\n")
    print(valid.head(25)[["spec", "exit", "n_total", "n_folds_valid", "n_folds_positive", "median_pf", "median_exp", "robust"]].to_string(index=False))
    print(f"\nFull report: phase2_walkforward.csv ({len(valid)} rows)")
    print(f"\nROBUST candidates (>=4 of 6 folds positive AND median PF >= 1.10):")
    robust = valid[valid.robust]
    print(f"  {len(robust)} pass")
    if len(robust):
        print(robust[["spec", "exit", "n_total", "n_folds_positive", "median_pf"]].head(10).to_string(index=False))


if __name__ == "__main__":
    main()
