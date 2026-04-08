"""Data-First Edge Discovery — Let the data tell us what works.

Instead of testing pre-built strategies, this script:
1. Computes features at every bar
2. Labels each bar with actual forward returns (what WOULD have happened)
3. Finds feature combinations that predict profitable entries
4. Outputs the "natural" strategy the data supports

This is NOT ML/overfitting — it's conditional probability analysis.
"""
import json, sys, math
from pathlib import Path
from collections import defaultdict
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

print("=" * 70)
print("  EDGE DISCOVERY ENGINE")
print("=" * 70)

# Load all FX data
data_dir = Path("argus_flow/data")
datasets = {}
for f in sorted(data_dir.glob("ibkr_*_1m.csv")):
    sym = f.stem.replace("ibkr_", "").replace("_1m", "").upper()
    if len(sym) == 6 and not any(c.isdigit() for c in sym):
        datasets[sym] = pd.read_csv(f)
        datasets[sym]["ts"] = pd.to_datetime(datasets[sym]["ts"], utc=True)

print(f"Loaded {len(datasets)} FX pairs\n")

def compute_all_features(df):
    """Compute 30+ features for every bar."""
    df = df.copy()
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    o = df["open"].astype(float)
    v = df["volume"].astype(float).fillna(0)

    # Basic
    df["range"] = h - l
    df["body"] = abs(c - o)
    df["upper_wick"] = h - df[["open","close"]].max(axis=1)
    df["lower_wick"] = df[["open","close"]].min(axis=1) - l
    df["bullish"] = (c > o).astype(int)

    # ATR
    df["atr_14"] = df["range"].rolling(14).mean()
    df["atr_50"] = df["range"].rolling(50).mean()

    # Range as % of price
    df["range_pct"] = df["range"] / c
    df["range_pct_z"] = (df["range_pct"] - df["range_pct"].rolling(60).mean()) / df["range_pct"].rolling(60).std().clip(lower=1e-10)

    # Range acceleration (last 15 vs prior 15)
    r1 = df["range"].rolling(15).mean().shift(15)
    r2 = df["range"].rolling(15).mean()
    df["range_accel"] = ((r2 - r1) / r1.clip(lower=1e-10))

    # Volatility
    df["vol_z"] = (v - v.rolling(60).mean()) / v.rolling(60).std().clip(lower=1e-10)

    # Trend
    df["sma_20"] = c.rolling(20).mean()
    df["sma_60"] = c.rolling(60).mean()
    df["sma_200"] = c.rolling(200).mean()
    df["above_sma20"] = (c > df["sma_20"]).astype(int)
    df["above_sma60"] = (c > df["sma_60"]).astype(int)
    df["trend_20_60"] = (df["sma_20"] > df["sma_60"]).astype(int)

    # Distance from recent high/low
    df["high_20"] = h.rolling(20).max()
    df["low_20"] = l.rolling(20).min()
    r20 = df["high_20"] - df["low_20"]
    df["dist_from_low"] = ((c - df["low_20"]) / r20.clip(lower=1e-10))
    df["dist_from_high"] = ((df["high_20"] - c) / r20.clip(lower=1e-10))

    # RSI
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.clip(lower=1e-10)
    df["rsi_14"] = 100 - (100 / (1 + rs))

    # Bollinger
    bb_mid = c.rolling(20).mean()
    bb_std = c.rolling(20).std()
    df["bb_upper"] = bb_mid + 2 * bb_std
    df["bb_lower"] = bb_mid - 2 * bb_std
    df["bb_pct"] = (c - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"]).clip(lower=1e-10)

    # Time features
    df["hour"] = df["ts"].dt.hour
    df["day_of_week"] = df["ts"].dt.dayofweek  # 0=Mon
    df["is_london"] = ((df["hour"] >= 7) & (df["hour"] <= 16)).astype(int)
    df["is_ny"] = ((df["hour"] >= 13) & (df["hour"] <= 21)).astype(int)
    df["is_overlap"] = ((df["hour"] >= 13) & (df["hour"] <= 16)).astype(int)

    # Efficiency ratio (trend strength)
    df["eff_ratio"] = abs(c - c.shift(20)) / df["range"].rolling(20).sum().clip(lower=1e-10)

    # Candle pattern
    df["doji"] = (df["body"] < df["range"] * 0.1).astype(int)
    df["hammer"] = ((df["lower_wick"] > df["body"] * 2) & (df["upper_wick"] < df["body"])).astype(int)
    df["engulfing_bull"] = ((c > o) & (c.shift(1) < o.shift(1)) & (c > o.shift(1)) & (o < c.shift(1))).astype(int)

    return df

def compute_forward_returns(df, horizons=[15, 30, 60, 90, 120]):
    """Label each bar with actual forward returns."""
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)

    for n in horizons:
        # Forward return in pips (approx)
        df[f"fwd_{n}m_ret"] = (c.shift(-n) - c)  # raw price change
        # Max favorable/adverse excursion
        df[f"fwd_{n}m_max_up"] = h.rolling(n).max().shift(-n) - c  # best long outcome
        df[f"fwd_{n}m_max_dn"] = c - l.rolling(n).min().shift(-n)  # best short outcome
        # Would a long with X stop / Y target have worked?
    return df

def analyze_conditional_edge(df, feature, bins, forward_col, pip_size=0.0001):
    """Analyze edge conditioned on a feature."""
    results = []
    for i in range(len(bins) - 1):
        mask = (df[feature] >= bins[i]) & (df[feature] < bins[i+1])
        subset = df.loc[mask, forward_col].dropna()
        if len(subset) < 20:
            continue
        ret_pips = subset / pip_size
        wr = (ret_pips > 0).mean()
        results.append({
            "range": f"{bins[i]:.2f}-{bins[i+1]:.2f}",
            "n": len(subset), "wr": wr,
            "avg_pips": ret_pips.mean(), "total_pips": ret_pips.sum(),
            "std": ret_pips.std(),
        })
    return results

# Process each pair
all_discoveries = {}

for symbol, df in datasets.items():
    pip_size = 0.01 if "JPY" in symbol else 0.0001
    print(f"\n{'='*70}")
    print(f"  {symbol}: {len(df)} bars")
    print(f"{'='*70}")

    df = compute_all_features(df)
    df = compute_forward_returns(df)
    df = df.dropna(subset=["atr_14", "fwd_60m_ret"])

    print(f"  Bars with complete features: {len(df)}")

    # ── DISCOVERY 1: Best hour to trade ──
    print(f"\n  HOUR ANALYSIS (60min forward return):")
    print(f"  {'Hour':>4s} {'N':>6s} {'WR_long':>8s} {'Avg_long':>9s} {'WR_short':>9s} {'Avg_short':>10s} {'Best':>6s}")
    hour_results = []
    for h in range(24):
        mask = df["hour"] == h
        subset = df[mask]
        if len(subset) < 50:
            continue
        long_ret = (subset["fwd_60m_ret"] / pip_size)
        short_ret = -(subset["fwd_60m_ret"] / pip_size)
        wr_long = (long_ret > 0).mean()
        wr_short = (short_ret > 0).mean()
        avg_long = long_ret.mean()
        avg_short = short_ret.mean()
        best = "LONG" if avg_long > avg_short and avg_long > 0.5 else ("SHORT" if avg_short > avg_long and avg_short > 0.5 else "-")
        hour_results.append({"hour": h, "n": len(subset), "wr_long": wr_long, "avg_long": avg_long,
                            "wr_short": wr_short, "avg_short": avg_short, "best": best})
        print(f"  {h:4d} {len(subset):6d} {wr_long:8.1%} {avg_long:+9.2f} {wr_short:9.1%} {avg_short:+10.2f} {best:>6s}")

    # ── DISCOVERY 2: Best conditions for entry ──
    print(f"\n  RANGE_PCT CONDITIONING (when range expands, what happens next 60min?):")
    range_bins = [0, 0.0005, 0.001, 0.0015, 0.002, 0.003, 0.005, 1.0]
    for r in analyze_conditional_edge(df, "range_pct", range_bins, "fwd_60m_ret", pip_size):
        direction = "LONG" if r["avg_pips"] > 0 else "SHORT"
        print(f"    range_pct {r['range']:15s}: n={r['n']:5d} WR={r['wr']:.1%} "
              f"avg={r['avg_pips']:+.2f} total={r['total_pips']:+.0f} [{direction}]")

    # ── DISCOVERY 3: RSI conditioning ──
    print(f"\n  RSI CONDITIONING:")
    rsi_bins = [0, 20, 30, 40, 50, 60, 70, 80, 100]
    for r in analyze_conditional_edge(df, "rsi_14", rsi_bins, "fwd_60m_ret", pip_size):
        direction = "LONG" if r["avg_pips"] > 0 else "SHORT"
        print(f"    RSI {r['range']:15s}: n={r['n']:5d} WR={r['wr']:.1%} "
              f"avg={r['avg_pips']:+.2f} [{direction}]")

    # ── DISCOVERY 4: Dist from low conditioning ──
    print(f"\n  DIST_FROM_LOW CONDITIONING (position in 20-bar range):")
    dist_bins = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    for r in analyze_conditional_edge(df, "dist_from_low", dist_bins, "fwd_60m_ret", pip_size):
        direction = "LONG" if r["avg_pips"] > 0 else "SHORT"
        print(f"    dist {r['range']:15s}: n={r['n']:5d} WR={r['wr']:.1%} "
              f"avg={r['avg_pips']:+.2f} [{direction}]")

    # ── DISCOVERY 5: Combined filters — find the sweet spot ──
    print(f"\n  COMBINED FILTER SEARCH (top 10 by avg pips):")
    combos = []
    for h in range(24):
        for rsi_lo, rsi_hi in [(0,30), (30,50), (50,70), (70,100)]:
            for dist_lo, dist_hi in [(0,0.3), (0.3,0.7), (0.7,1.0)]:
                mask = ((df["hour"] == h) &
                       (df["rsi_14"] >= rsi_lo) & (df["rsi_14"] < rsi_hi) &
                       (df["dist_from_low"] >= dist_lo) & (df["dist_from_low"] < dist_hi))
                subset = df.loc[mask, "fwd_60m_ret"].dropna()
                if len(subset) < 15:
                    continue
                ret_pips = subset / pip_size
                avg = ret_pips.mean()
                wr = (ret_pips > 0).mean()
                combos.append({
                    "filter": f"h={h} RSI=[{rsi_lo},{rsi_hi}) dist=[{dist_lo},{dist_hi})",
                    "n": len(subset), "wr": wr, "avg": avg, "total": ret_pips.sum(),
                    "direction": "LONG" if avg > 0 else "SHORT",
                })
    combos.sort(key=lambda x: abs(x["avg"]), reverse=True)
    for c in combos[:10]:
        print(f"    {c['filter']:45s} n={c['n']:4d} WR={c['wr']:.0%} "
              f"avg={c['avg']:+.2f} total={c['total']:+.0f} [{c['direction']}]")

    # ── DISCOVERY 6: Optimal hold time ──
    print(f"\n  OPTIMAL HOLD TIME (avg forward return by horizon):")
    for n in [15, 30, 60, 90, 120]:
        col = f"fwd_{n}m_ret"
        if col in df.columns:
            ret = (df[col] / pip_size).dropna()
            print(f"    {n:3d}min: avg={ret.mean():+.2f} WR_long={((ret>0).mean()):.1%} std={ret.std():.1f}")

    # ── DISCOVERY 7: MFE analysis — what's the actual payoff? ──
    print(f"\n  MFE ANALYSIS (max favorable excursion in 60min):")
    mfe_long = (df["fwd_60m_max_up"] / pip_size).dropna()
    mfe_short = (df["fwd_60m_max_dn"] / pip_size).dropna()
    for pct in [25, 50, 75, 90, 95]:
        print(f"    p{pct}: MFE_long={np.percentile(mfe_long, pct):+.1f} MFE_short={np.percentile(mfe_short, pct):+.1f}")

    all_discoveries[symbol] = {
        "hours": hour_results,
        "combos_top10": combos[:10],
    }

# Save discoveries
output = Path("ops/validation_tests/results/edge_discovery.json")
output.parent.mkdir(parents=True, exist_ok=True)
with open(output, "w") as f:
    json.dump(all_discoveries, f, indent=2, default=str)

print(f"\n{'='*70}")
print(f"  DISCOVERY COMPLETE — saved to {output}")
print(f"  Pairs analyzed: {len(all_discoveries)}")
print(f"{'='*70}")
