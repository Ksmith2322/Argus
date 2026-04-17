"""Run the full 16-cell × 4-label batch and emit reports.

Pipeline per (instrument, timeframe, label_variant, side):
  1. Load bars (cached)
  2. Compute features (~45)
  3. Compute label
  4. Run per-feature separation analysis (KS + effect size)
  5. Save report

Top-level summary highlights cells where any feature shows strong separation
(KS > 0.10 with Bonferroni-adjusted p < 0.001).
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from research.label_first.analyzer import feature_separation, label_density
from research.label_first.data_fetch import INSTRUMENTS, TIMEFRAMES, fetch_cell
from research.label_first.features import compute_features
from research.label_first.labeler import LABEL_VARIANTS, label_entries

REPORT_DIR = Path(__file__).resolve().parent / "reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

# Bonferroni: 45 features per side per label variant; threshold 0.001/45 ≈ 2.2e-5
P_THRESHOLD_RAW = 0.001
N_FEATURES_EST = 45
P_THRESHOLD_ADJ = P_THRESHOLD_RAW / N_FEATURES_EST
KS_THRESHOLD = 0.10  # minimum interesting effect


def run_cell(inst: str, tf: str) -> dict:
    df = fetch_cell(inst, tf)
    intraday = tf in ("5m", "15m", "1h")
    feats = compute_features(df, intraday=intraday)

    cell_summary = {"instrument": inst, "timeframe": tf, "n_bars": len(df), "results": []}

    for variant in LABEL_VARIANTS:
        labels_df = label_entries(df, variant)
        for side in ("long_label", "short_label"):
            labels = labels_df[side]
            density = label_density(labels)
            if density["n_pos"] < 30:
                continue
            sep = feature_separation(feats, labels)
            if sep.empty:
                continue
            # Strong-separation features
            strong = sep[(sep["ks_stat"] >= KS_THRESHOLD) & (sep["ks_p"] < P_THRESHOLD_ADJ)]
            top_5 = sep.head(5)[["feature", "ks_stat", "ks_p", "effect_size"]].to_dict(orient="records")

            tag = f"{inst}_{tf}_{variant}_{side.replace('_label','')}"
            sep_path = REPORT_DIR / f"{tag}_separation.csv"
            sep.to_csv(sep_path, index=False)

            cell_summary["results"].append({
                "label_variant": variant,
                "side": side.replace("_label", ""),
                "label_density_pct": round(density["rate_pct"], 3),
                "n_pos": density["n_pos"],
                "n_strong_features": len(strong),
                "top_5_features": top_5,
                "report_csv": sep_path.name,
            })
    return cell_summary


def run_all() -> dict:
    full = {"thresholds": {"ks": KS_THRESHOLD, "p_bonferroni": P_THRESHOLD_ADJ}, "cells": []}
    for inst in INSTRUMENTS:
        for tf in TIMEFRAMES:
            try:
                print(f"  Running {inst} {tf}...")
                summary = run_cell(inst, tf)
                full["cells"].append(summary)
            except Exception as e:
                print(f"    FAIL {inst} {tf}: {type(e).__name__}: {e}")
                full["cells"].append({"instrument": inst, "timeframe": tf, "error": f"{type(e).__name__}: {e}"})
    out_path = REPORT_DIR / "batch_summary.json"
    out_path.write_text(json.dumps(full, indent=2, default=str))
    print(f"\nWrote {out_path}")
    return full


if __name__ == "__main__":
    run_all()
