"""Per-feature separation analysis.

For each feature, ask: does its distribution differ between bars labeled
LONG_ENTRY (or SHORT_ENTRY) and bars not labeled? Strong separation = the
feature carries predictive information about future moves.

Two metrics per feature:
- KS statistic (distribution distance, 0=identical, 1=fully separated)
- Mean difference normalized by std (effect size)

Bonferroni-style adjustment is applied at the report level — only features
with KS p-value surviving 45-feature correction count as "real" separation.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import stats


def feature_separation(features: pd.DataFrame, labels: pd.Series) -> pd.DataFrame:
    """For each feature column, compute KS-stat + effect size between
    labels==True and labels==False populations.

    Returns DataFrame sorted by ks_stat descending, with columns:
      feature, ks_stat, ks_p, effect_size, mean_pos, mean_neg, n_pos, n_neg
    """
    pos_mask = labels.fillna(False).astype(bool).values
    neg_mask = ~pos_mask
    n_pos = int(pos_mask.sum())
    n_neg = int(neg_mask.sum())

    rows = []
    for col in features.columns:
        x = features[col].values
        finite = np.isfinite(x)
        x_pos = x[pos_mask & finite]
        x_neg = x[neg_mask & finite]
        if len(x_pos) < 30 or len(x_neg) < 30:
            continue
        try:
            ks = stats.ks_2samp(x_pos, x_neg, alternative="two-sided", method="asymp")
        except Exception:
            continue
        std_pool = np.sqrt((np.var(x_pos) + np.var(x_neg)) / 2) or 1e-12
        effect = (np.mean(x_pos) - np.mean(x_neg)) / std_pool
        rows.append({
            "feature": col,
            "ks_stat": ks.statistic,
            "ks_p": ks.pvalue,
            "effect_size": effect,
            "mean_pos": float(np.mean(x_pos)),
            "mean_neg": float(np.mean(x_neg)),
            "n_pos": int(len(x_pos)),
            "n_neg": int(len(x_neg)),
        })
    df = pd.DataFrame(rows).sort_values("ks_stat", ascending=False).reset_index(drop=True)
    return df


def label_density(labels: pd.Series) -> dict:
    n = len(labels)
    pos = int(labels.fillna(False).astype(bool).sum())
    return {"n_bars": n, "n_pos": pos, "rate_pct": 100 * pos / n if n else 0.0}
