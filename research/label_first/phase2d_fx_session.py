"""Phase 2D: hour-of-day session edge for FX pairs (GBPUSD, USDJPY, CADJPY).

Replicates Phase 2C methodology on FX 1H bars. FX trades 24h with three
distinct sessions (Asia 22-08 UTC, London 08-13, NY 13-22) — natural
variation by hour. Walk-forward 6 folds, robust = >=4 folds positive.

Argus pairs (USDJPY, CADJPY, GBPUSD) are the natural target since we already
trade them — finding intraday session edges adds to the existing infrastructure.
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


def hour_edge_table(inst: str, forward_bars: int = 4, target_atr: float = 1.0, stop_atr: float = 0.5) -> pd.DataFrame:
    df = fetch_cell(inst, "1h")
    a = _atr(df)
    H, L, C = df["High"].values, df["Low"].values, df["Close"].values

    pnl_long = np.full(len(df), np.nan)
    pnl_short = np.full(len(df), np.nan)
    for i in range(len(df) - forward_bars):
        ai = a[i]
        if not np.isfinite(ai) or ai <= 0:
            continue
        entry = C[i]
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

    return pd.DataFrame({
        "hour": df.index.hour,
        "long_pnl": pnl_long,
        "short_pnl": pnl_short,
    }, index=df.index).dropna()


def aggregate(d: pd.DataFrame, side: str) -> pd.DataFrame:
    col = f"{side}_pnl"
    grp = d.groupby("hour")[col].agg(["count", "mean", "std", lambda x: (x > 0).mean()])
    grp.columns = ["n", "exp_atr", "std_atr", "wr"]
    pos = d[d[col] > 0].groupby("hour")[col].sum()
    neg = d[d[col] <= 0].groupby("hour")[col].sum().abs()
    grp["pf"] = pos / neg
    grp["t_stat"] = grp["exp_atr"] / (grp["std_atr"] / np.sqrt(grp["n"]))
    return grp.reset_index()


def walkforward_hour(d: pd.DataFrame, hour: int, side: str, n_folds: int = 6) -> dict:
    col = f"{side}_pnl"
    sub = d[d.hour == hour].sort_index()
    if len(sub) < 30:
        return {"hour": hour, "valid_folds": 0, "pos_folds": 0, "median_exp": None, "median_pf": None}
    fs = len(sub) // n_folds
    fold_exps = []
    fold_pfs = []
    pos = 0
    for f in range(n_folds):
        s = f * fs
        e = (f + 1) * fs if f < n_folds - 1 else len(sub)
        bucket = sub.iloc[s:e][col]
        if len(bucket) < 5:
            continue
        ex = bucket.mean()
        gw = bucket[bucket > 0].sum()
        gl = abs(bucket[bucket <= 0].sum()) or 1e-9
        fold_exps.append(ex)
        fold_pfs.append(gw / gl)
        if ex > 0:
            pos += 1
    return {
        "hour": hour,
        "valid_folds": len(fold_exps),
        "pos_folds": pos,
        "median_exp": float(np.median(fold_exps)) if fold_exps else None,
        "median_pf": float(np.median(fold_pfs)) if fold_pfs else None,
    }


def analyze(inst: str) -> None:
    print(f"\n=== {inst} 1H session-of-day ===")
    d = hour_edge_table(inst)
    print(f"  {len(d):,} forward-outcome rows ({d.index.min()} -> {d.index.max()})")

    for side in ("long", "short"):
        agg = aggregate(d, side)
        agg = agg[agg["n"] >= 100]
        wf_rows = [walkforward_hour(d, int(h), side) for h in agg["hour"]]
        wf = pd.DataFrame(wf_rows)
        full = agg.merge(wf, on="hour", how="left")
        full["robust"] = (full.pos_folds >= 4) & (full.median_pf >= 1.10) & (full.valid_folds >= 5)
        full = full.sort_values("median_pf", ascending=False, na_position="last")
        full.to_csv(REPORT_DIR / f"phase2d_{inst}_{side}_hours.csv", index=False)
        robust = full[full.robust]
        print(f"\n  {side.upper()} side — {len(robust)} robust hours (>=5 valid folds, >=4 positive, median PF>=1.10):")
        if len(robust):
            print(robust.head(8)[["hour", "n", "exp_atr", "wr", "pf", "t_stat", "valid_folds", "pos_folds", "median_pf"]].to_string(index=False))
        else:
            print("    (none)")
            print("    Top 3 by median_pf anyway:")
            print(full.head(3)[["hour", "n", "pf", "median_pf", "pos_folds", "valid_folds"]].to_string(index=False))


def main():
    for inst in ("GBPUSD", "USDJPY", "CADJPY"):
        try:
            analyze(inst)
        except Exception as e:
            print(f"FAIL {inst}: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
