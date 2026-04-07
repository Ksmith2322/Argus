"""Angle Sweep: test 6 performance improvement angles across all FX pairs.

1. Session/Hour optimization — find the profitable hours
2. ATR-scaled stops — adaptive stop/target sizing
3. Partial profit taking — scale out at halfway
4. Entry confirmation — 2-bar close confirmation
5. Regime-adaptive — different triggers for different regimes
6. Cross-pair confirmation — correlated pair filter

Usage: python ops/angle_sweep.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import numpy as np
from pathlib import Path
from collections import defaultdict

DATA_DIR = Path("argus_flow/data")

PAIRS = {
    "EURUSD": {"pip": 0.0001, "session": (7, 16)},
    "GBPUSD": {"pip": 0.0001, "session": (22, 8)},
    "USDJPY": {"pip": 0.01,   "session": (22, 8)},
    "GBPJPY": {"pip": 0.01,   "session": (7, 16)},
    "AUDUSD": {"pip": 0.0001, "session": (22, 8)},
    "AUDJPY": {"pip": 0.01,   "session": (7, 16)},
    "EURJPY": {"pip": 0.01,   "session": (7, 16)},
    "USDCHF": {"pip": 0.0001, "session": (22, 8)},
    "NZDUSD": {"pip": 0.0001, "session": (22, 8)},
    "AUDCAD": {"pip": 0.0001, "session": (22, 8)},
}

# Cross-pair correlation groups
CORRELATION_GROUPS = {
    "USD_WEAK": ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"],  # all go up when USD weak
    "JPY_WEAK": ["USDJPY", "EURJPY", "GBPJPY", "AUDJPY"],  # all go up when JPY weak
}


def load_pair(pair):
    path = DATA_DIR / f"ibkr_{pair.lower()}_1m.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path, parse_dates=["ts"]).set_index("ts").sort_index()
    col_map = {c: c.capitalize() for c in df.columns if c[0].islower()}
    if col_map:
        df = df.rename(columns=col_map)
    return df


def resample_5m(df):
    return df.resample("5min").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"
    }).dropna()


def compute_atr(df, period=14):
    tr = np.maximum(df["High"] - df["Low"],
        np.maximum(abs(df["High"] - df["Close"].shift(1)), abs(df["Low"] - df["Close"].shift(1))))
    return tr.rolling(period).mean()


def base_trigger(df, i):
    window = df.iloc[max(0, i - 60):i + 1]
    if len(window) < 20:
        return None
    rng = window["High"].max() - window["Low"].min()
    if rng / window["Close"].mean() < 0.001:
        return None
    dist = (float(window["Close"].iloc[-1]) - window["Low"].min()) / (rng + 1e-10)
    if dist < 0.35:
        return "long"
    if dist > 0.65:
        return "short"
    return None


def simulate_trade(df, idx, direction, pip, stop_pips, target_pips, timeout_bars,
                   trailing=0, breakeven=0, partial_at=0):
    entry = float(df["Close"].iloc[idx])
    stop = entry - stop_pips * pip if direction == "long" else entry + stop_pips * pip
    target = entry + target_pips * pip if direction == "long" else entry - target_pips * pip
    peak_fav = 0.0
    be_done = False
    partial_taken = False
    partial_pnl = 0.0

    for j in range(idx + 1, min(idx + timeout_bars + 2, len(df))):
        row = df.iloc[j]
        fav = ((row["High"] - entry) / pip) if direction == "long" else ((entry - row["Low"]) / pip)
        peak_fav = max(peak_fav, fav)

        # Partial profit: close half at partial_at pips
        if partial_at > 0 and not partial_taken and peak_fav >= partial_at:
            partial_pnl = partial_at * 0.5  # half position at partial target
            partial_taken = True
            # Move stop to breakeven for remaining half
            stop = entry + pip if direction == "long" else entry - pip
            be_done = True

        if breakeven > 0 and peak_fav >= breakeven and not be_done:
            stop = entry + pip if direction == "long" else entry - pip
            be_done = True

        if trailing > 0 and peak_fav > trailing:
            if direction == "long":
                stop = max(stop, entry + (peak_fav - trailing) * pip)
            else:
                stop = min(stop, entry - (peak_fav - trailing) * pip)

        if direction == "long" and row["Low"] <= stop:
            pnl = (stop - entry) / pip
            remaining = 0.5 if partial_taken else 1.0
            return round(pnl * remaining + partial_pnl, 1), j
        if direction == "short" and row["High"] >= stop:
            pnl = (entry - stop) / pip
            remaining = 0.5 if partial_taken else 1.0
            return round(pnl * remaining + partial_pnl, 1), j
        if direction == "long" and row["High"] >= target:
            remaining = 0.5 if partial_taken else 1.0
            return round(target_pips * remaining + partial_pnl, 1), j
        if direction == "short" and row["Low"] <= target:
            remaining = 0.5 if partial_taken else 1.0
            return round(target_pips * remaining + partial_pnl, 1), j

        if j >= idx + timeout_bars:
            pnl = (row["Close"] - entry) / pip if direction == "long" else (entry - row["Close"]) / pip
            remaining = 0.5 if partial_taken else 1.0
            return round(pnl * remaining + partial_pnl, 1), j

    return None, idx


def compute_stats(trades):
    if not trades:
        return {"T": 0, "WR": 0, "PF": 0, "Total": 0, "Per": 0}
    wins = [p for p in trades if p > 0]
    losses = [p for p in trades if p <= 0]
    total = sum(trades)
    wr = len(wins) / len(trades) * 100
    pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else 99
    return {"T": len(trades), "WR": round(wr, 1), "PF": round(pf, 2),
            "Total": round(total, 0), "Per": round(total / len(trades), 2)}


def fmt(s):
    if s["T"] == 0:
        return "no trades"
    return f"T={s['T']:4d} WR={s['WR']:4.0f}% PF={s['PF']:5.2f} {s['Total']:+7.0f}p ({s['Per']:+.1f}/t)"


# ═══════════════════════════════════════════════════════════════
# LOAD DATA
# ═══════════════════════════════════════════════════════════════
print("=" * 100)
print("  ANGLE SWEEP — 6 improvement angles across 10 pairs")
print("=" * 100)

pair_data_1m = {}
pair_data_5m = {}
pair_atrs = {}

for pair in PAIRS:
    df = load_pair(pair)
    if df is not None and len(df) > 5000:
        pair_data_1m[pair] = df
        df5 = resample_5m(df)
        pair_data_5m[pair] = df5
        pair_atrs[pair] = compute_atr(df5)
        print(f"  {pair}: {len(df):,} 1m bars -> {len(df5):,} 5m bars")
print()


# ═══════════════════════════════════════════════════════════════
# BASELINE (current best: 5m, 15 stop, 10 trail, 10 BE)
# ═══════════════════════════════════════════════════════════════
print("=" * 100)
print("  BASELINE (current deployed: 5m, tight stop, trailing, breakeven)")
print("=" * 100)

baseline_trades = {}
for pair, df5 in pair_data_5m.items():
    pip = PAIRS[pair]["pip"]
    s_start, s_end = PAIRS[pair]["session"]
    seed = int(len(df5) * 0.2)
    trades = []
    last_exit = 0
    for i in range(seed, len(df5)):
        if i <= last_exit: continue
        hour = df5.index[i].hour
        if s_start < s_end:
            if hour < s_start or hour >= s_end: continue
        else:
            if hour < s_start and hour >= s_end: continue
        d = base_trigger(df5, i)
        if d is None: continue
        pnl, ex = simulate_trade(df5, i, d, pip, 15, 50, 60, trailing=10, breakeven=10)
        if pnl is not None:
            trades.append(pnl)
            last_exit = ex
    baseline_trades[pair] = trades
    print(f"  {pair:8s}: {fmt(compute_stats(trades))}")

all_baseline = [t for trades in baseline_trades.values() for t in trades]
print(f"  {'TOTAL':8s}: {fmt(compute_stats(all_baseline))}")


# ═══════════════════════════════════════════════════════════════
# ANGLE 1: SESSION/HOUR OPTIMIZATION
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 100)
print("  ANGLE 1: SESSION/HOUR PROFITABILITY")
print("=" * 100)

hour_pnl = defaultdict(lambda: defaultdict(list))
for pair, df5 in pair_data_5m.items():
    pip = PAIRS[pair]["pip"]
    seed = int(len(df5) * 0.2)
    last_exit = 0
    for i in range(seed, len(df5)):
        if i <= last_exit: continue
        d = base_trigger(df5, i)
        if d is None: continue
        hour = df5.index[i].hour
        pnl, ex = simulate_trade(df5, i, d, pip, 15, 50, 60, trailing=10, breakeven=10)
        if pnl is not None:
            hour_pnl[pair][hour].append(pnl)
            last_exit = ex

# Aggregate across pairs
agg_hour = defaultdict(list)
for pair in hour_pnl:
    for hour, trades in hour_pnl[pair].items():
        agg_hour[hour].extend(trades)

print(f"\n  {'Hour':>4s}  {'Trades':>6s}  {'WR':>5s}  {'PF':>5s}  {'Total':>8s}  {'Per/T':>7s}  Verdict")
print("  " + "-" * 55)
profitable_hours = set()
for hour in sorted(agg_hour.keys()):
    trades = agg_hour[hour]
    s = compute_stats(trades)
    if s["T"] < 5: continue
    verdict = "KEEP" if s["PF"] >= 1.2 else ("MARGINAL" if s["PF"] >= 0.9 else "KILL")
    if s["PF"] >= 1.0:
        profitable_hours.add(hour)
    marker = " ***" if s["PF"] >= 1.5 else (" *" if s["PF"] >= 1.2 else "")
    print(f"  {hour:4d}  {s['T']:6d}  {s['WR']:4.0f}%  {s['PF']:5.2f}  {s['Total']:+7.0f}p  {s['Per']:+6.1f}p  {verdict}{marker}")

# Test with only profitable hours
print(f"\n  Profitable hours: {sorted(profitable_hours)}")
angle1_trades = []
for pair, df5 in pair_data_5m.items():
    pip = PAIRS[pair]["pip"]
    seed = int(len(df5) * 0.2)
    last_exit = 0
    for i in range(seed, len(df5)):
        if i <= last_exit: continue
        hour = df5.index[i].hour
        if hour not in profitable_hours: continue
        d = base_trigger(df5, i)
        if d is None: continue
        pnl, ex = simulate_trade(df5, i, d, pip, 15, 50, 60, trailing=10, breakeven=10)
        if pnl is not None:
            angle1_trades.append(pnl)
            last_exit = ex
s = compute_stats(angle1_trades)
print(f"  Hour-filtered: {fmt(s)}")
print(f"  vs Baseline:   {fmt(compute_stats(all_baseline))}")


# ═══════════════════════════════════════════════════════════════
# ANGLE 2: ATR-SCALED STOPS
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 100)
print("  ANGLE 2: ATR-SCALED STOPS (adaptive to volatility)")
print("=" * 100)

for atr_stop_mult, atr_target_mult in [(1.0, 2.5), (1.5, 3.0), (2.0, 4.0), (1.5, 2.5)]:
    all_trades = []
    for pair, df5 in pair_data_5m.items():
        pip = PAIRS[pair]["pip"]
        s_start, s_end = PAIRS[pair]["session"]
        atr = pair_atrs[pair]
        seed = int(len(df5) * 0.2)
        last_exit = 0
        for i in range(seed, len(df5)):
            if i <= last_exit: continue
            hour = df5.index[i].hour
            if s_start < s_end:
                if hour < s_start or hour >= s_end: continue
            else:
                if hour < s_start and hour >= s_end: continue
            d = base_trigger(df5, i)
            if d is None: continue
            if i >= len(atr) or pd.isna(atr.iloc[i]):
                continue
            current_atr_pips = float(atr.iloc[i]) / pip
            stop = max(5, round(current_atr_pips * atr_stop_mult))
            target = max(10, round(current_atr_pips * atr_target_mult))
            timeout = 60
            pnl, ex = simulate_trade(df5, i, d, pip, stop, target, timeout, trailing=int(stop*0.7), breakeven=int(stop*0.7))
            if pnl is not None:
                all_trades.append(pnl)
                last_exit = ex
    s = compute_stats(all_trades)
    print(f"  ATR {atr_stop_mult}x/{atr_target_mult}x: {fmt(s)}")
print(f"  Baseline (fixed): {fmt(compute_stats(all_baseline))}")


# ═══════════════════════════════════════════════════════════════
# ANGLE 3: PARTIAL PROFIT TAKING
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 100)
print("  ANGLE 3: PARTIAL PROFIT TAKING (close half, trail rest)")
print("=" * 100)

for partial_at in [10, 15, 20, 25]:
    all_trades = []
    for pair, df5 in pair_data_5m.items():
        pip = PAIRS[pair]["pip"]
        s_start, s_end = PAIRS[pair]["session"]
        seed = int(len(df5) * 0.2)
        last_exit = 0
        for i in range(seed, len(df5)):
            if i <= last_exit: continue
            hour = df5.index[i].hour
            if s_start < s_end:
                if hour < s_start or hour >= s_end: continue
            else:
                if hour < s_start and hour >= s_end: continue
            d = base_trigger(df5, i)
            if d is None: continue
            pnl, ex = simulate_trade(df5, i, d, pip, 15, 50, 60,
                                     trailing=10, breakeven=0, partial_at=partial_at)
            if pnl is not None:
                all_trades.append(pnl)
                last_exit = ex
    s = compute_stats(all_trades)
    print(f"  Partial @ {partial_at:2d} pips: {fmt(s)}")
print(f"  Baseline (no partial): {fmt(compute_stats(all_baseline))}")


# ═══════════════════════════════════════════════════════════════
# ANGLE 4: ENTRY CONFIRMATION (2-bar close)
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 100)
print("  ANGLE 4: ENTRY CONFIRMATION (wait for confirmation)")
print("=" * 100)

for confirm_bars in [1, 2, 3]:
    all_trades = []
    for pair, df5 in pair_data_5m.items():
        pip = PAIRS[pair]["pip"]
        s_start, s_end = PAIRS[pair]["session"]
        seed = int(len(df5) * 0.2)
        last_exit = 0
        pending = None  # (direction, trigger_idx, bars_confirmed)
        for i in range(seed, len(df5)):
            if i <= last_exit:
                pending = None
                continue
            hour = df5.index[i].hour
            if s_start < s_end:
                if hour < s_start or hour >= s_end: pending = None; continue
            else:
                if hour < s_start and hour >= s_end: pending = None; continue

            d = base_trigger(df5, i)

            if pending is not None:
                # Check if current bar confirms the pending direction
                price = float(df5["Close"].iloc[i])
                prev = float(df5["Close"].iloc[i - 1])
                if pending[0] == "long" and price > prev:
                    confirmed = pending[2] + 1
                elif pending[0] == "short" and price < prev:
                    confirmed = pending[2] + 1
                else:
                    pending = None
                    continue

                if confirmed >= confirm_bars:
                    pnl, ex = simulate_trade(df5, i, pending[0], pip, 15, 50, 60, trailing=10, breakeven=10)
                    if pnl is not None:
                        all_trades.append(pnl)
                        last_exit = ex
                    pending = None
                else:
                    pending = (pending[0], pending[1], confirmed)
            elif d is not None:
                pending = (d, i, 0)

    s = compute_stats(all_trades)
    print(f"  {confirm_bars}-bar confirm: {fmt(s)}")
print(f"  Baseline (immediate): {fmt(compute_stats(all_baseline))}")


# ═══════════════════════════════════════════════════════════════
# ANGLE 5: REGIME-ADAPTIVE STRATEGY
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 100)
print("  ANGLE 5: REGIME-ADAPTIVE (different strategy per regime)")
print("=" * 100)

def detect_regime(df, i, lookback=100):
    if i < lookback: return "unknown"
    window = df.iloc[max(0, i - lookback):i + 1]
    close = window["Close"].astype(float)
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    # Trend strength: how far apart EMAs are
    spread = abs(float(ema20.iloc[-1]) - float(ema50.iloc[-1])) / float(close.iloc[-1])
    # Volatility: ATR / price
    atr = float((window["High"] - window["Low"]).rolling(14).mean().iloc[-1])
    vol_norm = atr / float(close.iloc[-1])
    if spread > 0.002 and vol_norm > 0.001:
        return "trending"
    elif vol_norm < 0.0005:
        return "quiet"
    else:
        return "ranging"

# In trending: use momentum (enter WITH trend, not against)
# In ranging: use mean reversion (current strategy)
# In quiet: skip (low opportunity)
all_trades = []
regime_counts = defaultdict(int)
for pair, df5 in pair_data_5m.items():
    pip = PAIRS[pair]["pip"]
    s_start, s_end = PAIRS[pair]["session"]
    seed = int(len(df5) * 0.2)
    last_exit = 0
    for i in range(seed, len(df5)):
        if i <= last_exit: continue
        hour = df5.index[i].hour
        if s_start < s_end:
            if hour < s_start or hour >= s_end: continue
        else:
            if hour < s_start and hour >= s_end: continue

        regime = detect_regime(df5, i)
        regime_counts[regime] += 1

        if regime == "quiet":
            continue  # skip quiet markets

        if regime == "trending":
            # Momentum: enter WITH the trend
            window = df5.iloc[max(0, i - 20):i + 1]
            if len(window) < 10: continue
            close = window["Close"].astype(float)
            ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
            price = float(close.iloc[-1])
            if price > ema20:
                d = "long"
            elif price < ema20:
                d = "short"
            else:
                continue
            pnl, ex = simulate_trade(df5, i, d, pip, 20, 40, 60, trailing=15, breakeven=10)
        else:
            # Ranging: mean reversion (current)
            d = base_trigger(df5, i)
            if d is None: continue
            pnl, ex = simulate_trade(df5, i, d, pip, 15, 50, 60, trailing=10, breakeven=10)

        if pnl is not None:
            all_trades.append(pnl)
            last_exit = ex

s = compute_stats(all_trades)
print(f"  Regime-adaptive:   {fmt(s)}")
print(f"  Baseline (always revert): {fmt(compute_stats(all_baseline))}")
print(f"  Regime distribution: {dict(regime_counts)}")


# ═══════════════════════════════════════════════════════════════
# ANGLE 6: CROSS-PAIR CONFIRMATION
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 100)
print("  ANGLE 6: CROSS-PAIR CONFIRMATION (correlated pair agreement)")
print("=" * 100)

# For each pair, check if at least 1 other pair in its correlation group agrees on direction
def get_cross_signal(pair, direction, df5_all, idx_time):
    for group_name, group_pairs in CORRELATION_GROUPS.items():
        if pair not in group_pairs:
            continue
        confirms = 0
        for other in group_pairs:
            if other == pair or other not in df5_all:
                continue
            odf = df5_all[other]
            # Find the bar closest to idx_time
            mask = odf.index <= idx_time
            if mask.sum() < 30:
                continue
            oi = mask.sum() - 1
            od = base_trigger(odf, oi)
            if od == direction:
                confirms += 1
        return confirms
    return 0  # pair not in any group

all_trades_cross = []
all_trades_no_cross = []
for pair, df5 in pair_data_5m.items():
    pip = PAIRS[pair]["pip"]
    s_start, s_end = PAIRS[pair]["session"]
    seed = int(len(df5) * 0.2)
    last_exit_c = 0
    last_exit_n = 0
    for i in range(seed, len(df5)):
        hour = df5.index[i].hour
        if s_start < s_end:
            if hour < s_start or hour >= s_end: continue
        else:
            if hour < s_start and hour >= s_end: continue
        d = base_trigger(df5, i)
        if d is None: continue

        # Without cross-pair
        if i > last_exit_n:
            pnl, ex = simulate_trade(df5, i, d, pip, 15, 50, 60, trailing=10, breakeven=10)
            if pnl is not None:
                all_trades_no_cross.append(pnl)
                last_exit_n = ex

        # With cross-pair confirmation
        if i > last_exit_c:
            confirms = get_cross_signal(pair, d, pair_data_5m, df5.index[i])
            if confirms >= 1:  # at least 1 other pair agrees
                pnl, ex = simulate_trade(df5, i, d, pip, 15, 50, 60, trailing=10, breakeven=10)
                if pnl is not None:
                    all_trades_cross.append(pnl)
                    last_exit_c = ex

print(f"  With cross-pair:    {fmt(compute_stats(all_trades_cross))}")
print(f"  Without cross-pair: {fmt(compute_stats(all_trades_no_cross))}")


# ═══════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 100)
print("  SUMMARY: ALL ANGLES vs BASELINE")
print("=" * 100)
print(f"  {'Angle':<30s}  {'Result':>50s}")
print("  " + "-" * 82)
print(f"  {'Baseline (current)':<30s}  {fmt(compute_stats(all_baseline)):>50s}")
print(f"  {'1. Hour filter':<30s}  {fmt(compute_stats(angle1_trades)):>50s}")
# ATR best will be printed inline
print(f"  {'4. Entry confirmation':<30s}  (see above)")
print(f"  {'5. Regime-adaptive':<30s}  {fmt(s):>50s}")
print(f"  {'6. Cross-pair confirm':<30s}  {fmt(compute_stats(all_trades_cross)):>50s}")
