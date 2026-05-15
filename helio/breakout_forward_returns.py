#!/usr/bin/env python3
"""helio/breakout_forward_returns.py — Forward-return edge gate for sub-$10 breakouts.

The detection step in helio/breakout_research.py tells us a 10%+ move happened.
This module answers the question that actually matters: AFTER the move, what
came next? Is there a tradable edge to FADE or FOLLOW these breakouts, or is
this noise that survives only because of methodology bias?

Methodology (per 2026-05-14 audit Agent 2):
  - Entry: NEXT-DAY OPEN (no lookahead from the breakout-day close).
  - Forward windows: k ∈ {1, 3, 5, 10} trading days.
  - Excess return: stock_return - SPY_return over the same window.
  - Statistical test: two-sided t-test on excess returns, bootstrap 95% CI.
  - Negative control: sector-shuffle test (mismatched ticker+date) detects
    methodology leakage. If shuffled distribution has the same mean as the
    real distribution, our "edge" is noise.
  - Survivorship discount: headline IR is multiplied by 0.6 to account for
    the 27% delisting rate in our universe.

Decision gate:
  - forward_ir >= 0.5 (post-discount) → FOLLOW signal, build long-side strategy
  - forward_ir <= -0.5 (post-discount) → FADE signal, build short-side strategy
  - |forward_ir| < 0.3 → NOISE, pivot to higher-EV adjacent path (S-3 dilution
    short, PEAD on small-caps)

Usage:
    python -m helio.breakout_forward_returns --suffix _sub10
    python -m helio.breakout_forward_returns --suffix _sub10 --shuffle
    python -m helio.breakout_forward_returns --suffix _sub10 --no-discount
"""
from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
DATA_DIR = REPO / "helio" / "data" / "breakout"
RESULTS_DIR = REPO / "helio" / "data" / "breakout_results"

# Per Agent 2: discount headline IR by 0.6× to account for survivorship bias
# from our 27% delist rate. Set to 1.0 to disable.
DEFAULT_SURVIVORSHIP_DISCOUNT = 0.6

# Forward-return windows (trading days after breakout)
FORWARD_WINDOWS = (1, 3, 5, 10)


def _load_daily(symbol: str) -> pd.DataFrame | None:
    """Load a single ticker's daily OHLCV CSV. Returns None if missing."""
    path = DATA_DIR / f"{symbol}_daily.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        return df
    except Exception:
        return None


def _next_day_open_return(df: pd.DataFrame, breakout_date: pd.Timestamp,
                          k_days: int, post_window_offset: int = 5) -> float | None:
    """Compute (close[entry+k] / open[entry] - 1).

    CRITICAL no-lookahead correction (2026-05-14): the breakout-detection
    engine in breakout_research.py records `breakout_date` as the START of
    the 5-day window where a >=10% move was detected — NOT the day the
    threshold was crossed. Entering the day after breakout_date overlaps
    the move we're trying to predict, producing fictional IRs of 2-5.

    Fix: wait for the entire detection window to complete before entering.
    With post_window_offset=5 (matches the detect lookback), entry is at
    the OPEN of breakout_date + 5 + 1 = day 6. This represents the realistic
    case "I saw the 5-day breakout complete, then I traded the day after."

    Returns None if data missing."""
    try:
        idx_dates = df.index.tz_localize(None) if df.index.tz is not None else df.index
        bd = pd.Timestamp(breakout_date).tz_localize(None) if pd.Timestamp(breakout_date).tz is not None else pd.Timestamp(breakout_date)
        mask = idx_dates >= bd
        if not mask.any():
            return None
        # Skip past the detection window so we never overlap the move
        entry_idx = mask.argmax() + post_window_offset + 1
        exit_idx = entry_idx + k_days
        if exit_idx >= len(df) or entry_idx >= len(df):
            return None
        entry_open = float(df["Open"].iloc[entry_idx])
        exit_close = float(df["Close"].iloc[exit_idx])
        if entry_open <= 0:
            return None
        return exit_close / entry_open - 1.0
    except Exception:
        return None


def _bootstrap_ci(values: np.ndarray, n_resamples: int = 5000,
                  ci: float = 0.95) -> tuple[float, float]:
    """Bootstrap 95% CI for the mean of an array. Returns (low, high)."""
    if len(values) == 0:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(42)
    means = []
    n = len(values)
    for _ in range(n_resamples):
        sample = rng.choice(values, size=n, replace=True)
        means.append(np.mean(sample))
    alpha = (1.0 - ci) / 2.0
    return (float(np.quantile(means, alpha)), float(np.quantile(means, 1.0 - alpha)))


def _t_statistic(values: np.ndarray) -> tuple[float, float]:
    """Return (t_stat, two-sided p-value) for null = mean is zero."""
    if len(values) < 2:
        return (float("nan"), float("nan"))
    mean = float(np.mean(values))
    std = float(np.std(values, ddof=1))
    n = len(values)
    if std == 0:
        return (float("inf") if mean != 0 else 0.0, 0.0 if mean != 0 else 1.0)
    t = mean / (std / np.sqrt(n))
    # Approximate two-sided p-value via normal (large n) — for n>1000 this is accurate
    from math import erfc, sqrt
    p = erfc(abs(t) / sqrt(2.0))
    return (t, p)


def _information_ratio(excess_returns: np.ndarray) -> float:
    """Annualized IR assuming returns are k-day-overlapping non-iid; we use
    daily-equivalent IR for comparability: mean/std × sqrt(252/k_avg)."""
    if len(excess_returns) < 2:
        return float("nan")
    mean = float(np.mean(excess_returns))
    std = float(np.std(excess_returns, ddof=1))
    if std == 0:
        return float("inf") if mean > 0 else (float("-inf") if mean < 0 else 0.0)
    # Use the median window (5d) as the annualization basis for headline IR
    return (mean / std) * np.sqrt(252.0 / 5.0)


def compute_forward_returns(breakouts_csv: Path, spy_df: pd.DataFrame,
                             shuffle: bool = False,
                             survivorship_discount: float = 1.0,
                             catalyst_filter: str | None = None) -> dict:
    """Compute forward returns and excess-vs-SPY for every breakout in the CSV.

    shuffle=True activates the sector-shuffle negative control: each breakout's
    ticker is replaced with a random different ticker from the same direction
    bucket, holding the date constant. Any "edge" that survives shuffling is
    methodology leakage, not real signal.

    survivorship_discount: multiplier applied to the headline IR in the output
    summary (default 1.0 = none; 0.6 = audit-recommended discount for 27%
    delist rate).
    """
    df = pd.read_csv(breakouts_csv)
    df["date"] = pd.to_datetime(df["date"])
    print(f"\nLoaded {len(df)} breakouts from {breakouts_csv.name}")

    # Optionally filter to one catalyst type (post-EDGAR-enrichment analysis)
    if catalyst_filter is not None and "catalyst_label" in df.columns:
        before = len(df)
        df = df[df["catalyst_label"] == catalyst_filter].copy()
        print(f"  Filtered to catalyst='{catalyst_filter}': {len(df)} of {before} breakouts")
        if len(df) == 0:
            print("  No breakouts match this catalyst. Aborting.")
            return ({"n_breakouts": 0, "catalyst_filter": catalyst_filter}, df)

    # Build per-ticker daily-data cache to avoid re-reading per row
    print("Loading per-ticker daily data...")
    cache: dict[str, pd.DataFrame | None] = {}
    for sym in df["symbol"].unique():
        cache[sym] = _load_daily(sym)
    n_loaded = sum(1 for v in cache.values() if v is not None)
    print(f"  {n_loaded}/{len(cache)} tickers loaded")

    # Optionally shuffle ticker assignments (negative control)
    if shuffle:
        print("SHUFFLE mode: reassigning each breakout's ticker to a random "
              "other ticker (date held constant)...")
        all_tickers = [s for s, d in cache.items() if d is not None]
        rng = random.Random(42)
        df = df.copy()
        # For each row, pick a random different ticker from the same direction
        new_symbols = []
        for _, row in df.iterrows():
            candidates = [t for t in all_tickers if t != row["symbol"]]
            new_symbols.append(rng.choice(candidates) if candidates else row["symbol"])
        df["symbol"] = new_symbols

    # Compute forward returns per row
    results = []
    skipped = 0
    for _, row in df.iterrows():
        sym = row["symbol"]
        ticker_df = cache.get(sym)
        if ticker_df is None:
            skipped += 1
            continue
        row_out = {
            "symbol": sym,
            "date": row["date"].strftime("%Y-%m-%d"),
            "direction": row["direction"],
            "move_pct": row.get("move_pct", float("nan")),
        }
        any_ok = False
        for k in FORWARD_WINDOWS:
            stock_ret = _next_day_open_return(ticker_df, row["date"], k)
            spy_ret = _next_day_open_return(spy_df, row["date"], k)
            row_out[f"ret_{k}d"] = stock_ret if stock_ret is not None else float("nan")
            row_out[f"spy_ret_{k}d"] = spy_ret if spy_ret is not None else float("nan")
            if stock_ret is not None and spy_ret is not None:
                row_out[f"excess_{k}d"] = stock_ret - spy_ret
                any_ok = True
            else:
                row_out[f"excess_{k}d"] = float("nan")
        if any_ok:
            results.append(row_out)
        else:
            skipped += 1

    out_df = pd.DataFrame(results)
    print(f"  Computed forward returns for {len(out_df)} breakouts ({skipped} skipped)")

    # Summary stats per direction and window
    summary: dict = {
        "n_breakouts": int(len(out_df)),
        "n_skipped": int(skipped),
        "shuffle_control": bool(shuffle),
        "survivorship_discount": float(survivorship_discount),
        "windows": {},
    }

    print(f"\n{'='*70}")
    print(f"FORWARD-RETURN SUMMARY  (shuffle={shuffle}, discount={survivorship_discount}×)")
    print(f"{'='*70}")
    print(f"  {'Direction':6s}  {'k':>3s}  {'n':>6s}  {'mean%':>7s}  {'t':>6s}  {'p':>7s}  "
          f"{'CI95_low%':>10s}  {'CI95_hi%':>9s}  {'IR':>6s}")

    for direction in ("UP", "DOWN"):
        dir_df = out_df[out_df["direction"] == direction]
        summary["windows"][direction] = {}
        for k in FORWARD_WINDOWS:
            col = f"excess_{k}d"
            vals = dir_df[col].dropna().to_numpy()
            n = len(vals)
            if n == 0:
                continue
            mean = float(np.mean(vals))
            t_stat, p_val = _t_statistic(vals)
            ci_lo, ci_hi = _bootstrap_ci(vals, n_resamples=2000)
            ir = _information_ratio(vals) * survivorship_discount
            summary["windows"][direction][f"{k}d"] = {
                "n": n,
                "mean_pct": round(mean * 100, 4),
                "t_stat": round(t_stat, 3),
                "p_value": round(p_val, 5),
                "ci95_low_pct": round(ci_lo * 100, 4),
                "ci95_high_pct": round(ci_hi * 100, 4),
                "information_ratio": round(ir, 3),
            }
            print(f"  {direction:6s}  {k:>3d}  {n:>6d}  {mean*100:>+6.3f}%  "
                  f"{t_stat:>+6.2f}  {p_val:>7.4f}  "
                  f"{ci_lo*100:>+9.3f}%  {ci_hi*100:>+8.3f}%  {ir:>+6.2f}")

    # Decision gate
    print(f"\n{'='*70}")
    print("DECISION GATE")
    print(f"{'='*70}")
    for direction in ("UP", "DOWN"):
        max_ir = 0.0
        max_k = None
        for k in FORWARD_WINDOWS:
            ir = summary["windows"].get(direction, {}).get(f"{k}d", {}).get("information_ratio", 0.0)
            if abs(ir) > abs(max_ir):
                max_ir = ir
                max_k = k
        if max_k is None:
            continue
        verdict = ("FOLLOW" if max_ir >= 0.5 else
                   "FADE" if max_ir <= -0.5 else
                   "NOISE")
        print(f"  {direction:6s} breakouts: best window k={max_k}d, IR={max_ir:+.2f} -> {verdict}")

    return summary, out_df


def main():
    parser = argparse.ArgumentParser(description="Forward-Return Edge Gate")
    parser.add_argument("--suffix", default="_sub10",
                        help="Suffix of the breakout CSV (default: _sub10)")
    parser.add_argument("--shuffle", action="store_true",
                        help="Run sector-shuffle negative control instead of "
                             "real analysis")
    parser.add_argument("--no-discount", action="store_true",
                        help="Disable the 0.6× survivorship discount")
    parser.add_argument("--catalyst", default=None,
                        help="Filter to one catalyst label (e.g. dilution_prospectus, "
                             "current_event_8k, none). Requires CSV enriched by "
                             "helio.edgar_catalysts --enrich.")
    parser.add_argument("--all-catalysts", action="store_true",
                        help="Loop over all catalyst labels and print summary table.")
    parser.add_argument("--walk-forward", action="store_true",
                        help="Split breakouts by date into first-half / second-half "
                             "and run separately. Signals that don't survive walk-forward "
                             "are overfitting. Combines with --catalyst or --all-catalysts.")
    args = parser.parse_args()

    breakouts_csv = RESULTS_DIR / f"all_breakouts{args.suffix}.csv"
    if not breakouts_csv.exists():
        print(f"ERROR: {breakouts_csv} not found. Run helio.breakout_research --scan first.")
        return

    spy_df = _load_daily("SPY")
    if spy_df is None:
        print("ERROR: SPY data not found. Run download_universe(['SPY']) first.")
        return

    discount = 1.0 if args.no_discount else DEFAULT_SURVIVORSHIP_DISCOUNT

    if args.walk_forward:
        # Split breakouts into first-half / second-half by date.
        # Run analysis on each. Survival across halves = robust signal.
        df_full = pd.read_csv(breakouts_csv)
        df_full["date"] = pd.to_datetime(df_full["date"])
        df_full = df_full.sort_values("date").reset_index(drop=True)
        midpoint = len(df_full) // 2
        median_date = df_full["date"].iloc[midpoint]
        print(f"\n{'='*70}")
        print(f"WALK-FORWARD VALIDATION  (split at {median_date.strftime('%Y-%m-%d')})")
        print(f"{'='*70}")

        # Write half-CSVs
        first_csv = RESULTS_DIR / f"_wf_first{args.suffix}.csv"
        second_csv = RESULTS_DIR / f"_wf_second{args.suffix}.csv"
        df_full.iloc[:midpoint].to_csv(first_csv, index=False)
        df_full.iloc[midpoint:].to_csv(second_csv, index=False)

        # Loop over catalysts and compare halves
        if "catalyst_label" in df_full.columns:
            catalysts = df_full["catalyst_label"].value_counts().to_dict()
        else:
            catalysts = {"_ALL_": len(df_full)}

        print(f"\n{'CATALYST':24s} {'DIR':4s} {'k':>3s} "
              f"{'h1_n':>6s} {'h1_IR':>7s}  {'h2_n':>6s} {'h2_IR':>7s}  STABLE?")
        print("-" * 80)

        comparison: dict = {}
        for cat, count in sorted(catalysts.items(), key=lambda x: -x[1]):
            if count < 60:
                continue  # need >=30 per half
            cat_filter = cat if cat != "_ALL_" else None
            s1, _ = compute_forward_returns(
                first_csv, spy_df, shuffle=False,
                survivorship_discount=discount, catalyst_filter=cat_filter,
            )
            s2, _ = compute_forward_returns(
                second_csv, spy_df, shuffle=False,
                survivorship_discount=discount, catalyst_filter=cat_filter,
            )
            comparison[cat] = {"first": s1, "second": s2}
            for direction in ("UP", "DOWN"):
                w1 = s1.get("windows", {}).get(direction, {})
                w2 = s2.get("windows", {}).get(direction, {})
                for k_label in ("1d", "3d", "5d", "10d"):
                    if k_label not in w1 or k_label not in w2:
                        continue
                    ir1 = w1[k_label]["information_ratio"]
                    ir2 = w2[k_label]["information_ratio"]
                    n1 = w1[k_label]["n"]
                    n2 = w2[k_label]["n"]
                    # "STABLE" = same sign AND both |IR| >= 0.3
                    stable = (ir1 * ir2 > 0) and (abs(ir1) >= 0.3 and abs(ir2) >= 0.3)
                    flag = "STABLE" if stable else ""
                    # Only print rows with at least one |IR| >= 0.5 to keep output usable
                    if max(abs(ir1), abs(ir2)) < 0.5:
                        continue
                    print(f"{cat:24s} {direction:4s} {k_label:>3s} "
                          f"{n1:>6d} {ir1:>+6.2f}   {n2:>6d} {ir2:>+6.2f}   {flag}")

        # Save the walk-forward comparison
        out_json = RESULTS_DIR / f"walk_forward{args.suffix}.json"
        out_json.write_text(json.dumps({
            "split_date": median_date.strftime("%Y-%m-%d"),
            "by_catalyst": comparison,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2, default=str))
        print(f"\n  Saved: {out_json}")
        # Clean up halves
        try:
            first_csv.unlink()
            second_csv.unlink()
        except OSError:
            pass
        return

    if args.all_catalysts:
        # Loop over every catalyst label in the CSV and produce a comparison table
        df_full = pd.read_csv(breakouts_csv)
        if "catalyst_label" not in df_full.columns:
            print("ERROR: CSV has no catalyst_label column. Run helio.edgar_catalysts --enrich first.")
            return
        catalysts = df_full["catalyst_label"].value_counts().to_dict()
        print(f"\n{'='*70}")
        print(f"PER-CATALYST FORWARD-RETURN COMPARISON  (discount={discount}x)")
        print(f"{'='*70}")
        all_summaries = {}
        for cat, count in sorted(catalysts.items(), key=lambda x: -x[1]):
            if count < 30:
                continue  # skip tiny samples
            print(f"\n--- catalyst = {cat} (n={count}) ---")
            summary, _ = compute_forward_returns(
                breakouts_csv, spy_df,
                shuffle=args.shuffle,
                survivorship_discount=discount,
                catalyst_filter=cat,
            )
            all_summaries[cat] = summary
        # Save combined summary
        out_json = RESULTS_DIR / f"forward_returns{args.suffix}_per_catalyst.json"
        out_json.write_text(json.dumps({
            "by_catalyst": all_summaries,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2, default=str))
        print(f"\n  Per-catalyst summary saved: {out_json}")
        return

    summary, out_df = compute_forward_returns(
        breakouts_csv, spy_df,
        shuffle=args.shuffle,
        survivorship_discount=discount,
        catalyst_filter=args.catalyst,
    )

    # Save outputs
    parts = [args.suffix]
    if args.shuffle:
        parts.append("_shuffled")
    if args.catalyst:
        parts.append(f"_cat-{args.catalyst}")
    out_suffix = "".join(parts)
    out_csv = RESULTS_DIR / f"forward_returns{out_suffix}.csv"
    out_json = RESULTS_DIR / f"forward_returns{out_suffix}.json"
    out_df.to_csv(out_csv, index=False)
    summary["generated_at"] = datetime.now(timezone.utc).isoformat()
    out_json.write_text(json.dumps(summary, indent=2, default=str))
    print(f"\n  Per-row CSV saved: {out_csv}")
    print(f"  Summary JSON saved: {out_json}")


if __name__ == "__main__":
    main()
