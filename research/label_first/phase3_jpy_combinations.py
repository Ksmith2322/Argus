"""Phase 3A: combination filter search for JPY PM Short edge.

Both USDJPY and CADJPY show 6/6-fold-positive short edge at 19 UTC. Question:
do additional filter features make it stronger? Or saturate the signal?

Tests each Phase 1 feature as a quartile filter on top of the hour-19 base
signal for both pairs. Reports per-pair and combined.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from research.label_first.backtest_engine import atr, stats
from research.label_first.data_fetch import fetch_cell
from research.label_first.features import compute_features

REPORT_DIR = Path(__file__).resolve().parent / "reports"
SIGNAL_HOUR = 19


def backtest_short(
    df: pd.DataFrame,
    signal: np.ndarray,
    target_atr: float = 1.0,
    stop_atr: float = 0.5,
    hold_bars: int = 4,
) -> pd.DataFrame:
    """Mirror of backtest_long for SHORT."""
    a_arr = atr(df)
    H, L, C, O = df["High"].values, df["Low"].values, df["Close"].values, df["Open"].values
    trades = []
    for i in np.where(signal)[0]:
        if i + 1 >= len(df):
            continue
        a = a_arr[i]
        if not np.isfinite(a) or a <= 0:
            continue
        entry = O[i + 1]
        target = entry - target_atr * a  # SHORT: target below
        stop = entry + stop_atr * a       # SHORT: stop above
        exit_price = None
        for j in range(i + 1, min(i + 1 + hold_bars, len(df))):
            if H[j] >= stop:
                exit_price = stop; break
            if L[j] <= target:
                exit_price = target; break
        if exit_price is None:
            exit_price = C[min(i + hold_bars, len(df) - 1)]
        trades.append({"entry_idx": i + 1, "pnl_atr": (entry - exit_price) / a})
    return pd.DataFrame(trades)


def search_combinations(inst: str, min_trades: int = 50):
    df = fetch_cell(inst, "1h")
    feats = compute_features(df, intraday=True)
    sig_base = np.asarray(df.index.hour == SIGNAL_HOUR)

    base_trades = backtest_short(df, sig_base)
    base_stats = stats(base_trades)
    print(f"\n  BASE {inst}: n={base_stats['n']} WR={base_stats['wr']:.1%} PF={base_stats['pf']:.2f} exp={base_stats['exp']:+.4f}")

    rows = [{"inst": inst, "filter": "BASE", "threshold": "-", **base_stats, "lift_pf": 1.0}]
    for col in feats.columns:
        if col in ("hour", "minute", "dow", "session_us"):
            continue
        x = feats[col].values
        finite = np.isfinite(x)
        if finite.sum() < 200:
            continue
        try:
            q33, q66 = np.nanpercentile(x[finite], [33, 67])
        except Exception:
            continue
        for tag, mask in [
            (f">{q66:.4f}", x > q66),
            (f"<{q33:.4f}", x < q33),
            (f"{q33:.4f}-{q66:.4f}", (x >= q33) & (x <= q66)),
        ]:
            sig = sig_base & mask & finite
            if sig.sum() < min_trades:
                continue
            t = backtest_short(df, sig)
            s = stats(t)
            if s["n"] < min_trades:
                continue
            rows.append({"inst": inst, "filter": col, "threshold": tag, **s,
                         "lift_pf": s["pf"] / base_stats["pf"] if base_stats["pf"] > 0 else float("inf")})
    return pd.DataFrame(rows).sort_values("pf", ascending=False).reset_index(drop=True), base_stats


def main():
    print("=== Phase 3A: JPY PM Short + combination filters ===")
    print("Base = SHORT entry at hour 19 UTC, target -1.0 ATR / stop +0.5 ATR / 4-bar hold")

    all_rows = []
    for inst in ("USDJPY", "CADJPY"):
        out, _ = search_combinations(inst)
        all_rows.append(out)
        print(f"\n  {inst} top 10 filters:")
        print(out.head(10)[["filter", "threshold", "n", "wr", "pf", "exp", "lift_pf"]].to_string(index=False))

    combined = pd.concat(all_rows, ignore_index=True)
    combined.to_csv(REPORT_DIR / "phase3_jpy_combinations.csv", index=False)
    print(f"\nSaved combined report: phase3_jpy_combinations.csv ({len(combined)} rows)")

    # Find filters that work across BOTH pairs (robust signal)
    print("\n  --- Cross-pair robust filters (PF>=1.40 on both USDJPY AND CADJPY) ---")
    pivot = combined.pivot_table(index=["filter", "threshold"], columns="inst", values="pf", aggfunc="mean")
    pivot.columns = [f"pf_{c}" for c in pivot.columns]
    pivot = pivot.dropna()
    robust = pivot[(pivot["pf_USDJPY"] >= 1.40) & (pivot["pf_CADJPY"] >= 1.40)]
    if len(robust):
        robust = robust.sort_values("pf_USDJPY", ascending=False)
        print(robust.head(10).to_string())
    else:
        print("    None found at >=1.40 on both. Top combined-PF filters:")
        pivot["min_pf"] = pivot.min(axis=1)
        print(pivot.sort_values("min_pf", ascending=False).head(10).to_string())


if __name__ == "__main__":
    main()
