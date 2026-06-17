"""Walk-forward fold harness.

In-sample / out-of-sample sliding split for a single (spec, ticker) pair.
v1 is "anchored expanding train, fixed-size test" — the simplest
honest split. Permutation/Monte-Carlo deferred to v2.

Why anchored expanding rather than rolling: most retail-scale daily
backtest universes (~5-10 years) don't have enough rolling-window
samples to give rolling its supposed advantage, and the anchored split
shows whether the strategy's edge degrades on each new chunk of data.

Usage:
    folds = walk_forward(spec, df, n_folds=4, test_frac=0.25)
    # → list of FoldResult, plus aggregate dict

A spec PASSES walk-forward if all OOS folds have PF >= 1.2 (matching the
project_2026_05_20 bootstrap promotion floor). Caller decides — this
module returns metrics, doesn't filter.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

import pandas as pd

from helio.backtest_factory.engine import BacktestResult, run_spec


@dataclass
class FoldResult:
    fold_idx: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    train_metrics: Dict[str, Any]
    test_metrics: Dict[str, Any]
    n_train_trades: int
    n_test_trades: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fold_idx": self.fold_idx,
            "train_window": [str(self.train_start), str(self.train_end)],
            "test_window": [str(self.test_start), str(self.test_end)],
            "train": self.train_metrics,
            "test": self.test_metrics,
            "n_train_trades": self.n_train_trades,
            "n_test_trades": self.n_test_trades,
        }


def walk_forward(
    spec: Any,
    df: pd.DataFrame,
    *,
    n_folds: int = 4,
    test_frac: float = 0.25,
    min_test_bars: int = 60,
) -> Tuple[List[FoldResult], Dict[str, Any]]:
    """Run anchored-expanding walk-forward.

    Splits df into n_folds contiguous test windows. For fold k:
        train = df[:test_start_k]   (everything before this fold's test)
        test  = df[test_start_k : test_end_k]

    Returns (list of FoldResult, aggregate dict). Aggregate carries:
        oos_pf_min, oos_pf_mean, oos_wr_mean, oos_n_total,
        train_pf_mean, degradation_pf (= train_pf_mean - oos_pf_mean)
    """
    if n_folds < 1:
        raise ValueError(f"n_folds must be >= 1, got {n_folds}")
    if not 0.0 < test_frac < 1.0:
        raise ValueError(f"test_frac must be in (0, 1), got {test_frac}")

    n = len(df)
    test_bars = max(int(n * test_frac / n_folds), min_test_bars)
    total_test = test_bars * n_folds
    if total_test >= n:
        raise ValueError(
            f"insufficient bars: n={n}, n_folds={n_folds}, test_bars={test_bars}, "
            f"total_test={total_test} >= n. Reduce n_folds or test_frac."
        )

    # Test windows are at the END of df, contiguous, each size test_bars
    test_start_idx = n - total_test
    folds: List[FoldResult] = []
    for k in range(n_folds):
        t_start = test_start_idx + k * test_bars
        t_end = t_start + test_bars
        train_df = df.iloc[:t_start]
        test_df = df.iloc[t_start:t_end]
        if len(train_df) < 30 or len(test_df) < min_test_bars:
            continue
        train_result: BacktestResult = run_spec(spec, train_df, ticker="WF_TRAIN")
        test_result: BacktestResult = run_spec(spec, test_df, ticker="WF_TEST")
        folds.append(FoldResult(
            fold_idx=k,
            train_start=train_df.index[0],
            train_end=train_df.index[-1],
            test_start=test_df.index[0],
            test_end=test_df.index[-1],
            train_metrics=train_result.metrics,
            test_metrics=test_result.metrics,
            n_train_trades=len(train_result.trades),
            n_test_trades=len(test_result.trades),
        ))

    agg = _aggregate(folds)
    return folds, agg


def _aggregate(folds: List[FoldResult]) -> Dict[str, Any]:
    if not folds:
        return {
            "n_folds": 0,
            "oos_pf_min": None,
            "oos_pf_mean": None,
            "oos_wr_mean": None,
            "oos_n_total": 0,
            "train_pf_mean": None,
            "degradation_pf": None,
            "passes_promotion_floor": False,
        }

    def _pf(m: Dict[str, Any]) -> float:
        v = m.get("pf")
        if v is None:
            return 0.0
        return float(v)

    oos_pfs = [_pf(f.test_metrics) for f in folds]
    train_pfs = [_pf(f.train_metrics) for f in folds]
    oos_wrs = [float(f.test_metrics.get("wr", 0.0)) for f in folds]
    oos_n = sum(f.n_test_trades for f in folds)

    oos_pf_min = min(oos_pfs)
    oos_pf_mean = sum(oos_pfs) / len(oos_pfs)
    train_pf_mean = sum(train_pfs) / len(train_pfs)
    oos_wr_mean = sum(oos_wrs) / len(oos_wrs)

    # Promotion floor matches project_2026_05_20 bootstrap memo: PF lower
    # bound 1.20. Here we use OOS PF min as a per-fold robustness gate.
    passes = oos_pf_min >= 1.20 and oos_n >= 20

    return {
        "n_folds": len(folds),
        "oos_pf_min": round(oos_pf_min, 4),
        "oos_pf_mean": round(oos_pf_mean, 4),
        "oos_wr_mean": round(oos_wr_mean, 4),
        "oos_n_total": int(oos_n),
        "train_pf_mean": round(train_pf_mean, 4),
        "degradation_pf": round(train_pf_mean - oos_pf_mean, 4),
        "passes_promotion_floor": bool(passes),
    }
