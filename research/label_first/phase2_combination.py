"""Phase 2A: combination filter search for the GBPUSD daily wick rule.

Take the base wick signal (uw>0.6 AND cp<0.3 from Phase 1) and test which
secondary features, used as filters, materially lift the PF/expectancy.

For each candidate filter feature, test 3 quantile windows (top, middle,
bottom thirds) AND 'exclude top'/'exclude bottom' filters. Report which
filter+threshold combinations give the largest lift over the base signal.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from research.label_first.backtest_engine import backtest_long, stats
from research.label_first.data_fetch import fetch_cell
from research.label_first.features import compute_features

REPORT_DIR = Path(__file__).resolve().parent / "reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)


def base_signal(df: pd.DataFrame, uw_min: float = 0.6, cp_max: float = 0.3) -> np.ndarray:
    H, L, C, O = df["High"].values, df["Low"].values, df["Close"].values, df["Open"].values
    rng = H - L
    upper_wick = (H - np.maximum(O, C)) / np.where(rng == 0, np.nan, rng)
    close_pos = (C - L) / np.where(rng == 0, np.nan, rng)
    return (upper_wick > uw_min) & (close_pos < cp_max) & np.isfinite(rng)


def search_combinations(
    inst: str = "GBPUSD",
    tf: str = "1d",
    target_atr: float = 1.0,
    stop_atr: float = 0.5,
    hold_bars: int = 8,
    exit_mode: str = "oco",
    base_uw: float = 0.6,
    base_cp: float = 0.3,
    min_trades: int = 50,
) -> pd.DataFrame:
    df = fetch_cell(inst, tf)
    intraday = tf in ("5m", "15m", "1h")
    feats = compute_features(df, intraday=intraday)

    sig_base = base_signal(df, base_uw, base_cp)
    base_trades = backtest_long(df, sig_base, target_atr, stop_atr, hold_bars, exit_mode=exit_mode)
    base_stats = stats(base_trades)
    print(f"BASE: n={base_stats['n']} WR={base_stats['wr']:.1%} PF={base_stats['pf']:.2f} exp={base_stats['exp']:+.4f}")

    rows = [{"filter": "BASE", "threshold": "-", **base_stats, "lift_pf": 1.0}]

    for col in feats.columns:
        x = feats[col].values
        finite = np.isfinite(x)
        if finite.sum() < 200:
            continue
        # Compute quantiles on the WHOLE bar set (not signal bars only) — represents
        # the threshold a live system would actually observe.
        q33, q66 = np.nanpercentile(x[finite], [33, 67])
        for tag, mask in [
            (f">{q66:.4f}", x > q66),
            (f"<{q33:.4f}", x < q33),
            (f"{q33:.4f}-{q66:.4f}", (x >= q33) & (x <= q66)),
        ]:
            sig = sig_base & mask & finite
            if sig.sum() < min_trades:
                continue
            trades = backtest_long(df, sig, target_atr, stop_atr, hold_bars, exit_mode=exit_mode)
            s = stats(trades)
            if s["n"] < min_trades:
                continue
            lift = s["pf"] / base_stats["pf"] if base_stats["pf"] > 0 else float("inf")
            rows.append({"filter": col, "threshold": tag, **s, "lift_pf": lift})

    out = pd.DataFrame(rows).sort_values("pf", ascending=False).reset_index(drop=True)
    return out


if __name__ == "__main__":
    print("=== Phase 2A: GBPUSD daily wick + combination filters ===\n")
    print("Exit: target +1.0 ATR / stop -0.5 ATR / hold 8 bars / OCO\n")
    res = search_combinations()
    res.to_csv(REPORT_DIR / "phase2_gbpusd_combinations.csv", index=False)
    print(f"\nTop 20 by PF:")
    print(res.head(20)[["filter", "threshold", "n", "wr", "pf", "exp", "lift_pf"]].to_string(index=False))
    print(f"\nFull report: phase2_gbpusd_combinations.csv ({len(res)} rows)")
