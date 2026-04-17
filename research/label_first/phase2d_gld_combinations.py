"""Phase 2D: combination filter search on GLD afternoon edge.

Already established (Phase 2C): GLD long at hours 18/19/20 UTC has PF 1.63-2.28.
Question: do filter features make it even stronger? Or is the time-of-day
edge already saturated and additional filters just shrink the sample?

Tests each Phase 1 feature as a quartile-based filter on top of the
hour-restricted base signal. Reports per-hour and combined.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from research.label_first.backtest_engine import backtest_long, stats
from research.label_first.data_fetch import fetch_cell
from research.label_first.features import compute_features

REPORT_DIR = Path(__file__).resolve().parent / "reports"

SIGNAL_HOURS = [18, 19, 20]


def base_signal(df: pd.DataFrame) -> np.ndarray:
    return np.isin(df.index.hour, SIGNAL_HOURS)


def search(target_atr=1.0, stop_atr=0.5, hold_bars=4, min_trades=50):
    df = fetch_cell("GLD", "1h")
    feats = compute_features(df, intraday=True)
    sig_base = base_signal(df)

    base_trades = backtest_long(df, sig_base, target_atr, stop_atr, hold_bars, exit_mode="oco")
    base_stats = stats(base_trades)
    print(f"BASE (all 3 hours): n={base_stats['n']} WR={base_stats['wr']:.1%} PF={base_stats['pf']:.2f} exp={base_stats['exp']:+.4f}")

    rows = [{"filter": "BASE", "threshold": "-", **base_stats, "lift_pf": 1.0}]
    for col in feats.columns:
        if col in ("hour", "minute", "dow", "session_us"):  # skip time features (already filtering)
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
            trades = backtest_long(df, sig, target_atr, stop_atr, hold_bars, exit_mode="oco")
            s = stats(trades)
            if s["n"] < min_trades:
                continue
            rows.append({
                "filter": col, "threshold": tag, **s,
                "lift_pf": s["pf"] / base_stats["pf"] if base_stats["pf"] > 0 else float("inf"),
            })

    out = pd.DataFrame(rows).sort_values("pf", ascending=False).reset_index(drop=True)
    return out, base_stats


def per_hour_breakdown(filter_spec=None):
    """Show PF per hour for base or filtered signal."""
    df = fetch_cell("GLD", "1h")
    feats = compute_features(df, intraday=True)
    sig = base_signal(df)
    if filter_spec:
        col, mode, q = filter_spec
        x = feats[col].values
        finite = np.isfinite(x)
        if mode == "above":
            sig = sig & finite & (x > q)
        elif mode == "below":
            sig = sig & finite & (x < q)
        elif mode == "middle":
            q33, q66 = q
            sig = sig & finite & (x >= q33) & (x <= q66)

    print(f"  Per-hour stats (filter={filter_spec}):")
    for h in SIGNAL_HOURS:
        h_mask = sig & (df.index.hour == h)
        if h_mask.sum() < 5:
            print(f"    Hour {h}: n={h_mask.sum()} insufficient"); continue
        trades = backtest_long(df, h_mask, 1.0, 0.5, 4, exit_mode="oco")
        s = stats(trades)
        print(f"    Hour {h}: n={s['n']} WR={s['wr']:.1%} PF={s['pf']:.2f} exp={s['exp']:+.4f}")


def main():
    print("=== Phase 2D: GLD afternoon + combination filters ===")
    print("Base = long entry at top of hour 18/19/20 UTC, target +1.0 ATR / stop -0.5 ATR / 4-bar hold\n")
    out, base_stats = search()
    out.to_csv(REPORT_DIR / "phase2d_gld_combinations.csv", index=False)
    print(f"\nTop 20 filtered signals by PF:")
    print(out.head(20)[["filter", "threshold", "n", "wr", "pf", "exp", "lift_pf"]].to_string(index=False))
    print(f"\nBottom 5 (filters that HURT):")
    print(out.tail(5)[["filter", "threshold", "n", "wr", "pf", "exp", "lift_pf"]].to_string(index=False))

    # Per-hour breakdown of base + top filter
    print("\n--- BASELINE per-hour ---")
    per_hour_breakdown()
    if len(out) > 1:
        top = out.iloc[1]  # row 0 is BASE itself
        col, threshold = top["filter"], top["threshold"]
        print(f"\n--- TOP filter ({col} {threshold}) per-hour ---")
        df = fetch_cell("GLD", "1h")
        feats = compute_features(df, intraday=True)
        x = feats[col].values
        finite = np.isfinite(x[np.isfinite(x)])
        q33, q66 = np.nanpercentile(x[np.isfinite(x)], [33, 67])
        if threshold.startswith(">"):
            per_hour_breakdown((col, "above", q66))
        elif threshold.startswith("<"):
            per_hour_breakdown((col, "below", q33))
        else:
            per_hour_breakdown((col, "middle", (q33, q66)))


if __name__ == "__main__":
    main()
