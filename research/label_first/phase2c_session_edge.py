"""Phase 2C: hour-of-day / session edge for ES, NQ, GLD on 1H bars.

For each (instrument, hour-of-day) cell, compute the empirical forward
return distribution. Hours with consistently positive expected return on
forward N-bar moves are tradeable as "trade only at hour X" strategies.

Walk-forward across 6 chronological folds; only hours that produce
positive expectancy in >=4 folds count as robust.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from research.label_first.data_fetch import fetch_cell

REPORT_DIR = Path(__file__).resolve().parent / "reports"


def _atr(df: pd.DataFrame, n: int = 14) -> np.ndarray:
    h, l, c = df["High"].values, df["Low"].values, df["Close"].values
    pc = np.concatenate([[np.nan], c[:-1]])
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    out = np.full_like(tr, np.nan, dtype=float)
    for i in range(n - 1, len(tr)):
        out[i] = np.nanmean(tr[i - n + 1 : i + 1])
    return out


def hour_edge_table(inst: str, tf: str = "1h", forward_bars: int = 4, target_atr: float = 1.0, stop_atr: float = 0.5) -> pd.DataFrame:
    """For each hour, simulate 'enter long at this bar's close, exit OCO ±ATR'."""
    df = fetch_cell(inst, tf)
    a = _atr(df)
    H, L, C = df["High"].values, df["Low"].values, df["Close"].values

    # Forward OCO outcome per bar
    pnl_long = np.full(len(df), np.nan)
    pnl_short = np.full(len(df), np.nan)
    for i in range(len(df) - forward_bars):
        ai = a[i]
        if not np.isfinite(ai) or ai <= 0:
            continue
        entry = C[i]
        # LONG
        target_l = entry + target_atr * ai
        stop_l = entry - stop_atr * ai
        out_l = None
        for j in range(i + 1, i + 1 + forward_bars):
            if L[j] <= stop_l:
                out_l = (stop_l - entry) / ai; break
            if H[j] >= target_l:
                out_l = (target_l - entry) / ai; break
        if out_l is None:
            out_l = (C[i + forward_bars] - entry) / ai
        pnl_long[i] = out_l
        # SHORT (mirror)
        target_s = entry - target_atr * ai
        stop_s = entry + stop_atr * ai
        out_s = None
        for j in range(i + 1, i + 1 + forward_bars):
            if H[j] >= stop_s:
                out_s = (entry - stop_s) / ai; break
            if L[j] <= target_s:
                out_s = (entry - target_s) / ai; break
        if out_s is None:
            out_s = (entry - C[i + forward_bars]) / ai
        pnl_short[i] = out_s

    out = pd.DataFrame({
        "hour": df.index.hour,
        "dow": df.index.dayofweek,
        "long_pnl": pnl_long,
        "short_pnl": pnl_short,
    }, index=df.index).dropna()
    return out


def aggregate_by_hour(d: pd.DataFrame, side: str) -> pd.DataFrame:
    col = f"{side}_pnl"
    grp = d.groupby("hour")[col].agg(["count", "mean", "std", lambda x: (x > 0).mean()])
    grp.columns = ["n", "exp_atr", "std_atr", "wr"]
    grp["pf"] = d[d[col] > 0].groupby("hour")[col].sum() / d[d[col] <= 0].groupby("hour")[col].sum().abs()
    grp["t_stat"] = grp["exp_atr"] / (grp["std_atr"] / np.sqrt(grp["n"]))
    return grp.reset_index()


def walkforward_hour(d: pd.DataFrame, hour: int, side: str, n_folds: int = 6) -> dict:
    col = f"{side}_pnl"
    sub = d[d.hour == hour]
    if len(sub) < 30:
        return {"hour": hour, "n": len(sub), "valid_folds": 0, "pos_folds": 0, "median_exp": None}
    sub = sub.sort_index()
    fs = len(sub) // n_folds
    fold_exps = []
    pos = 0
    for f in range(n_folds):
        s = f * fs
        e = (f + 1) * fs if f < n_folds - 1 else len(sub)
        bucket = sub.iloc[s:e][col]
        if len(bucket) < 5:
            continue
        ex = bucket.mean()
        fold_exps.append(ex)
        if ex > 0:
            pos += 1
    return {
        "hour": hour, "n": len(sub),
        "valid_folds": len(fold_exps), "pos_folds": pos,
        "median_exp": float(np.median(fold_exps)) if fold_exps else None,
        "robust": pos >= 4 and len(fold_exps) >= 5,
    }


def analyze_instrument(inst: str) -> dict:
    print(f"\n=== {inst} 1H session-of-day edge ===")
    d = hour_edge_table(inst, "1h", forward_bars=4, target_atr=1.0, stop_atr=0.5)
    print(f"  {len(d):,} forward-outcome rows ({d.index.min()} -> {d.index.max()})")

    results = {"instrument": inst, "long": [], "short": []}
    for side in ("long", "short"):
        agg = aggregate_by_hour(d, side)
        agg = agg[agg["n"] >= 50].sort_values("exp_atr", ascending=False).reset_index(drop=True)
        wf_rows = []
        for h in agg["hour"]:
            wf = walkforward_hour(d, int(h), side)
            wf_rows.append(wf)
        wf_df = pd.DataFrame(wf_rows)
        full = agg.merge(wf_df, on=["hour", "n"], how="left")
        full = full.sort_values("median_exp", ascending=False, na_position="last")
        results[side] = full
        print(f"\n  {side.upper()} side — top hours by walk-forward median expectancy:")
        print(full.head(8)[["hour", "n", "exp_atr", "wr", "pf", "t_stat", "valid_folds", "pos_folds", "median_exp", "robust"]].to_string(index=False))
    return results


def main():
    out_dir = REPORT_DIR
    for inst in ("ES", "NQ", "GLD"):
        results = analyze_instrument(inst)
        for side in ("long", "short"):
            results[side].to_csv(out_dir / f"phase2c_{inst}_{side}_hours.csv", index=False)
    print(f"\nReports saved to {out_dir}")


if __name__ == "__main__":
    main()
