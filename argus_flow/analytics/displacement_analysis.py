"""Phase 1-2: Find large displacement events and analyze precursors.

Payoff-first approach: start from where big moves happen,
reverse-engineer what precedes them.

Usage:
    python -m argus_flow.analytics.displacement_analysis \
        --bars argus_flow/replay_out/bars_1s.csv
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def find_displacement_events(
    bars: pd.DataFrame,
    horizon_bars: int = 600,
    threshold_pct: float = 0.0075,
    min_gap_bars: int = 600,
) -> pd.DataFrame:
    """Find non-overlapping large displacement events."""
    fwd_high = bars["high"].shift(-1).rolling(horizon_bars, min_periods=1).max()
    fwd_high = fwd_high.shift(-horizon_bars + 1)
    fwd_low = bars["low"].shift(-1).rolling(horizon_bars, min_periods=1).min()
    fwd_low = fwd_low.shift(-horizon_bars + 1)

    up = (fwd_high - bars["close"]) / bars["close"]
    down = (bars["close"] - fwd_low) / bars["close"]

    events = []
    next_allowed = 0
    for i in range(len(bars)):
        if i < next_allowed:
            continue
        u = up.iloc[i] if not pd.isna(up.iloc[i]) else 0
        d = down.iloc[i] if not pd.isna(down.iloc[i]) else 0
        if u >= threshold_pct or d >= threshold_pct:
            events.append({
                "idx": i,
                "ts": bars.at[i, "ts"],
                "close": float(bars.at[i, "close"]),
                "direction": "up" if u >= d else "down",
                "magnitude": float(max(u, d)),
            })
            next_allowed = i + min_gap_bars
    return pd.DataFrame(events)


def compute_precursors(bars: pd.DataFrame, events: pd.DataFrame, lookback: int = 300):
    """Compute observable features from the window BEFORE each event."""
    records = []
    for _, ev in events.iterrows():
        idx = int(ev["idx"])
        if idx < lookback + 60:
            continue

        pre = bars.iloc[idx - lookback : idx]
        context = bars.iloc[max(0, idx - 3600) : idx]

        # 1. Volatility z-score
        pre_range = (pre["high"] - pre["low"]).mean()
        ctx_range = (context["high"] - context["low"]).mean()
        vol_z = (pre_range - ctx_range) / ctx_range if ctx_range > 0 else 0

        # 2. Range compression
        total_range = pre["high"].max() - pre["low"].min()
        range_pct = total_range / pre["close"].iloc[-1] if pre["close"].iloc[-1] > 0 else 0

        # 3. Activity z-score
        pre_vol = pre["volume"].mean()
        ctx_vol = context["volume"].mean()
        activity_z = (pre_vol - ctx_vol) / ctx_vol if ctx_vol > 0 else 0

        # 4. Volume acceleration
        v1 = pre.iloc[: len(pre) // 2]["volume"].mean()
        v2 = pre.iloc[len(pre) // 2 :]["volume"].mean()
        vol_accel = (v2 - v1) / v1 if v1 > 0 else 0

        # 5. Delta imbalance
        total_delta = pre["delta"].sum()
        total_vol = pre["volume"].sum()
        delta_ratio = total_delta / total_vol if total_vol > 0 else 0

        # 6. Trade intensity z-score
        pre_intensity = pre["trade_count"].mean()
        ctx_intensity = context["trade_count"].mean()
        intensity_z = (pre_intensity - ctx_intensity) / ctx_intensity if ctx_intensity > 0 else 0

        # 7. Pre-window momentum
        momentum = (pre["close"].iloc[-1] - pre["close"].iloc[0]) / pre["close"].iloc[0]

        # 8. Bar range acceleration
        r1 = pre.iloc[: len(pre) // 2]["bar_range"].mean()
        r2 = pre.iloc[len(pre) // 2 :]["bar_range"].mean()
        range_accel = (r2 - r1) / r1 if r1 > 0 else 0

        records.append({
            "idx": idx,
            "ts": ev["ts"],
            "direction": ev["direction"],
            "magnitude": ev["magnitude"],
            "vol_z": vol_z,
            "range_pct": range_pct,
            "activity_z": activity_z,
            "vol_accel": vol_accel,
            "delta_ratio": delta_ratio,
            "intensity_z": intensity_z,
            "momentum_pct": momentum,
            "range_accel": range_accel,
        })

    return pd.DataFrame(records)


def generate_background(bars: pd.DataFrame, event_indices: set, n: int, lookback: int = 300, seed: int = 42):
    """Generate random background samples for contrast analysis."""
    rng = np.random.default_rng(seed)
    candidates = [i for i in range(lookback + 60, len(bars) - 600) if i not in event_indices]
    chosen = rng.choice(candidates, size=min(n, len(candidates)), replace=False)

    records = []
    for idx in chosen:
        pre = bars.iloc[idx - lookback : idx]
        context = bars.iloc[max(0, idx - 3600) : idx]

        pre_range = (pre["high"] - pre["low"]).mean()
        ctx_range = (context["high"] - context["low"]).mean()
        vol_z = (pre_range - ctx_range) / ctx_range if ctx_range > 0 else 0

        total_range = pre["high"].max() - pre["low"].min()
        range_pct = total_range / pre["close"].iloc[-1] if pre["close"].iloc[-1] > 0 else 0

        pre_vol = pre["volume"].mean()
        ctx_vol = context["volume"].mean()
        activity_z = (pre_vol - ctx_vol) / ctx_vol if ctx_vol > 0 else 0

        v1 = pre.iloc[: len(pre) // 2]["volume"].mean()
        v2 = pre.iloc[len(pre) // 2 :]["volume"].mean()
        vol_accel = (v2 - v1) / v1 if v1 > 0 else 0

        total_delta = pre["delta"].sum()
        total_vol = pre["volume"].sum()
        delta_ratio = total_delta / total_vol if total_vol > 0 else 0

        pre_intensity = pre["trade_count"].mean()
        ctx_intensity = context["trade_count"].mean()
        intensity_z = (pre_intensity - ctx_intensity) / ctx_intensity if ctx_intensity > 0 else 0

        momentum = (pre["close"].iloc[-1] - pre["close"].iloc[0]) / pre["close"].iloc[0]

        r1 = pre.iloc[: len(pre) // 2]["bar_range"].mean()
        r2 = pre.iloc[len(pre) // 2 :]["bar_range"].mean()
        range_accel = (r2 - r1) / r1 if r1 > 0 else 0

        records.append({
            "vol_z": vol_z,
            "range_pct": range_pct,
            "activity_z": activity_z,
            "vol_accel": vol_accel,
            "delta_ratio": delta_ratio,
            "intensity_z": intensity_z,
            "momentum_pct": momentum,
            "range_accel": range_accel,
        })

    return pd.DataFrame(records)


def contrast_analysis(events_df: pd.DataFrame, background_df: pd.DataFrame):
    """Compare event precursors vs random background."""
    features = [
        "vol_z", "range_pct", "activity_z", "vol_accel",
        "delta_ratio", "intensity_z", "momentum_pct", "range_accel",
    ]

    print(f"\n{'Feature':>20s} {'event_med':>10} {'bg_med':>10} {'ratio':>8} {'cohens_d':>9} {'strength':>10}")
    print("-" * 72)

    results = {}
    for feat in features:
        ev_med = events_df[feat].median()
        bg_med = background_df[feat].median()
        ev_mean = events_df[feat].mean()
        bg_mean = background_df[feat].mean()

        pooled_std = np.sqrt((events_df[feat].std() ** 2 + background_df[feat].std() ** 2) / 2)
        d = abs(ev_mean - bg_mean) / pooled_std if pooled_std > 0 else 0

        ratio = ev_med / bg_med if bg_med != 0 else float("inf")
        strength = "STRONG" if d > 0.5 else ("MEDIUM" if d > 0.3 else "weak")

        print(f"{feat:>20s} {ev_med:>10.4f} {bg_med:>10.4f} {ratio:>8.2f}x {d:>9.3f} {strength:>10}")
        results[feat] = {"cohens_d": d, "strength": strength, "event_median": ev_med, "bg_median": bg_med}

    return results


def monotonic_check(events_df: pd.DataFrame):
    """Check if stronger precursors -> larger displacement."""
    features = ["vol_z", "range_pct", "activity_z", "vol_accel", "intensity_z", "range_accel"]

    print(f"\n{'Feature':>20s} {'Q1_mag':>8} {'Q2_mag':>8} {'Q3_mag':>8} {'Q4_mag':>8} {'monotonic':>10}")
    print("-" * 72)

    for feat in features:
        vals = events_df[feat].dropna()
        if len(vals) < 20:
            continue

        try:
            events_df["_q"] = pd.qcut(vals, 4, labels=False, duplicates="drop")
        except ValueError:
            continue

        mags = []
        for q in sorted(events_df["_q"].dropna().unique()):
            group = events_df[events_df["_q"] == q]
            mags.append(group["magnitude"].mean() * 100)

        is_mono = all(mags[i] <= mags[i + 1] for i in range(len(mags) - 1))

        mag_strs = " ".join(f"{m:>7.3f}%" for m in mags)
        print(f"{feat:>20s} {mag_strs} {'YES' if is_mono else 'no':>10}")
        events_df.drop(columns=["_q"], inplace=True)


def main():
    parser = argparse.ArgumentParser(description="Displacement event analysis")
    parser.add_argument("--bars", required=True, help="Path to 1s bars CSV")
    parser.add_argument("--horizon", type=int, default=600, help="Forward horizon in bars/seconds")
    parser.add_argument("--threshold", type=float, default=0.0075, help="Min displacement (default: 0.0075 = 75bps)")
    parser.add_argument("--outdir", default="argus_flow/replay_out", help="Output directory")
    args = parser.parse_args()

    bars = pd.read_csv(args.bars)
    bars["ts"] = pd.to_datetime(bars["ts"], utc=True)
    n_days = (bars["ts"].max() - bars["ts"].min()).total_seconds() / 86400

    print(f"Loaded {len(bars):,} bars ({n_days:.1f} days)")

    # Phase 1: Find events
    print("\n" + "=" * 70)
    print(f"PHASE 1: DISPLACEMENT EVENTS (horizon={args.horizon}s, threshold={args.threshold*100:.2f}%)")
    print("=" * 70)

    events = find_displacement_events(bars, args.horizon, args.threshold)
    print(f"Events found: {len(events)} ({len(events)/n_days:.1f}/day)")
    print(f"Up: {(events['direction']=='up').sum()}  Down: {(events['direction']=='down').sum()}")
    print(f"Magnitude: avg={events['magnitude'].mean()*100:.3f}%  med={events['magnitude'].median()*100:.3f}%")

    # Phase 2: Precursors
    print("\n" + "=" * 70)
    print("PHASE 2: PRECURSOR ANALYSIS")
    print("=" * 70)

    precursors = compute_precursors(bars, events)
    print(f"Analyzable events: {len(precursors)}")

    event_idx_set = set(precursors["idx"].values)
    background = generate_background(bars, event_idx_set, n=len(precursors) * 3)
    print(f"Background samples: {len(background)}")

    # Contrast
    print("\n--- Event vs Background Contrast ---")
    contrast = contrast_analysis(precursors, background)

    # Monotonic
    print("\n--- Monotonic Check: stronger precursor -> larger move? ---")
    monotonic_check(precursors)

    # Session analysis
    precursors["hour"] = precursors["ts"].dt.hour
    precursors["session"] = np.select(
        [precursors["hour"].between(0, 7), precursors["hour"].between(8, 15), precursors["hour"].between(16, 23)],
        ["asia", "europe", "us"],
        default="unknown",
    )

    print("\n--- Session Distribution ---")
    for sess, g in precursors.groupby("session"):
        print(f"  {sess}: {len(g)} events ({len(g)/len(precursors)*100:.1f}%)  avg_mag={g['magnitude'].mean()*100:.3f}%")

    print("\n--- Top Hours ---")
    for h, c in precursors["hour"].value_counts().head(5).items():
        print(f"  {h:02d}:00 UTC: {c} events")

    # Save
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    precursors.to_csv(outdir / "displacement_events.csv", index=False)
    background.to_csv(outdir / "background_samples.csv", index=False)

    summary = {
        "horizon_bars": args.horizon,
        "threshold_pct": args.threshold,
        "n_events": len(precursors),
        "events_per_day": len(precursors) / n_days,
        "avg_magnitude_pct": float(precursors["magnitude"].mean()),
        "contrast": {k: {"cohens_d": v["cohens_d"], "strength": v["strength"]} for k, v in contrast.items()},
    }
    (outdir / "displacement_summary.json").write_text(json.dumps(summary, indent=2))

    print(f"\nSaved: {outdir / 'displacement_events.csv'}")
    print(f"Saved: {outdir / 'displacement_summary.json'}")


if __name__ == "__main__":
    main()