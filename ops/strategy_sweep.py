"""Strategy Sweep: test 13 strategies × 10 pairs × 4 timeframes.

Tests every combination and ranks by total PnL and profit factor.
Timeframes: 1m (current), 5m, 15m, 1H — each with scaled stop/target.

Usage: python ops/strategy_sweep.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import numpy as np
from pathlib import Path

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

TIMEFRAMES = {
    "1m":  {"resample": None,    "stop_mult": 1.0, "timeout_mult": 1.0, "check_every": 5},
    "5m":  {"resample": "5min",  "stop_mult": 1.5, "timeout_mult": 1.0, "check_every": 1},
    "15m": {"resample": "15min", "stop_mult": 2.5, "timeout_mult": 1.5, "check_every": 1},
    "30m": {"resample": "30min", "stop_mult": 3.0, "timeout_mult": 1.5, "check_every": 1},
    "1h":  {"resample": "1h",    "stop_mult": 4.0, "timeout_mult": 2.0, "check_every": 1},
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


def resample_df(df, rule):
    if rule is None:
        return df
    return df.resample(rule).agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"
    }).dropna()


def simulate_trade(df, entry_idx, direction, pip, stop_pips, target_pips, timeout_bars,
                   trailing=0, breakeven=0):
    entry_price = float(df["Close"].iloc[entry_idx])
    stop = entry_price - stop_pips * pip if direction == "long" else entry_price + stop_pips * pip
    target = entry_price + target_pips * pip if direction == "long" else entry_price - target_pips * pip
    peak_favorable = 0.0
    be_active = False

    for j in range(entry_idx + 1, min(entry_idx + timeout_bars + 2, len(df))):
        row = df.iloc[j]

        if direction == "long":
            fav = (row["High"] - entry_price) / pip
        else:
            fav = (entry_price - row["Low"]) / pip
        peak_favorable = max(peak_favorable, fav)

        if breakeven > 0 and peak_favorable >= breakeven and not be_active:
            stop = entry_price + 1 * pip if direction == "long" else entry_price - 1 * pip
            be_active = True

        if trailing > 0 and peak_favorable > trailing:
            if direction == "long":
                stop = max(stop, entry_price + (peak_favorable - trailing) * pip)
            else:
                stop = min(stop, entry_price - (peak_favorable - trailing) * pip)

        if direction == "long" and row["Low"] <= stop:
            return round((stop - entry_price) / pip, 1), j
        if direction == "short" and row["High"] >= stop:
            return round((entry_price - stop) / pip, 1), j
        if direction == "long" and row["High"] >= target:
            return target_pips, j
        if direction == "short" and row["Low"] <= target:
            return target_pips, j

        if j >= entry_idx + timeout_bars:
            pnl = (row["Close"] - entry_price) / pip if direction == "long" else (entry_price - row["Close"]) / pip
            return round(pnl, 1), j

    return None, entry_idx


# ═══════════════════════════════════════════════════════════════
# TRIGGER FUNCTIONS (work on any timeframe DataFrame)
# ═══════════════════════════════════════════════════════════════

def trigger_range(df, i, lookback=60):
    """Mean reversion: long near range low, short near range high."""
    window = df.iloc[max(0, i - lookback):i + 1]
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


def trigger_bb(df, i, **_):
    """Bollinger Band bounce: long at lower, short at upper."""
    window = df.iloc[max(0, i - 100):i + 1]
    if len(window) < 25:
        return None
    close = window["Close"].astype(float)
    sma = close.rolling(20).mean()
    std = close.rolling(20).std(ddof=0)
    price = float(close.iloc[-1])
    upper = float((sma + 2 * std).iloc[-1])
    lower = float((sma - 2 * std).iloc[-1])
    if price <= lower:
        return "long"
    if price >= upper:
        return "short"
    return None


def trigger_vwap(df, i, **_):
    """VWAP reversion: long when > 1 ATR below VWAP, short when > 1 ATR above."""
    window = df.iloc[max(0, i - 300):i + 1]
    if len(window) < 60:
        return None
    tp = (window["High"] + window["Low"] + window["Close"]) / 3
    vol = window["Volume"].clip(lower=1)
    vwap = float((tp * vol).cumsum().iloc[-1] / vol.cumsum().iloc[-1])
    atr = float((window["High"] - window["Low"]).rolling(14).mean().iloc[-1])
    if atr <= 0:
        return None
    deviation = (float(window["Close"].iloc[-1]) - vwap) / atr
    if deviation < -1.0:
        return "long"
    if deviation > 1.0:
        return "short"
    return None


def trigger_rsi(df, i, **_):
    """RSI extreme: long < 25, short > 75."""
    window = df.iloc[max(0, i - 100):i + 1]
    if len(window) < 20:
        return None
    close = window["Close"].astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss
    rsi = float((100 - (100 / (1 + rs))).iloc[-1])
    if rsi < 25:
        return "long"
    if rsi > 75:
        return "short"
    return None


def trigger_momentum(df, i, **_):
    """Momentum breakout: long on 60-bar high, short on 60-bar low."""
    window = df.iloc[max(0, i - 60):i + 1]
    if len(window) < 60:
        return None
    price = float(window["Close"].iloc[-1])
    pip = 0.01 if price > 50 else 0.0001
    high_60 = float(window["High"].max())
    low_60 = float(window["Low"].min())
    if abs(price - high_60) < 3 * pip:
        return "long"
    if abs(price - low_60) < 3 * pip:
        return "short"
    return None


def trigger_ema_cross(df, i, **_):
    """EMA crossover: long when EMA20 > EMA50, short when below."""
    window = df.iloc[max(0, i - 60):i + 1]
    if len(window) < 55:
        return None
    close = window["Close"].astype(float)
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    curr_20, curr_50 = float(ema20.iloc[-1]), float(ema50.iloc[-1])
    prev_20, prev_50 = float(ema20.iloc[-2]), float(ema50.iloc[-2])
    # Fresh cross only
    if prev_20 <= prev_50 and curr_20 > curr_50:
        return "long"
    if prev_20 >= prev_50 and curr_20 < curr_50:
        return "short"
    return None


def trigger_engulfing(df, i, **_):
    """Engulfing candle pattern: bullish or bearish engulfing."""
    if i < 2:
        return None
    prev = df.iloc[i - 1]
    curr = df.iloc[i]
    # Bullish engulfing: prev red, curr green, curr body engulfs prev body
    if prev["Close"] < prev["Open"] and curr["Close"] > curr["Open"]:
        if curr["Open"] <= prev["Close"] and curr["Close"] >= prev["Open"]:
            return "long"
    # Bearish engulfing
    if prev["Close"] > prev["Open"] and curr["Close"] < curr["Open"]:
        if curr["Open"] >= prev["Close"] and curr["Close"] <= prev["Open"]:
            return "short"
    return None


def trigger_sr_bounce(df, i, **_):
    """Support/resistance bounce: long at support cluster, short at resistance."""
    if i < 120:
        return None
    lookback = df.iloc[max(0, i - 120):i]
    if len(lookback) < 60:
        return None
    # Find S/R via price clusters
    highs = lookback["High"].values
    lows = lookback["Low"].values
    atr = float((lookback["High"] - lookback["Low"]).rolling(14).mean().iloc[-1])
    if atr <= 0:
        return None
    bucket = atr / 5
    all_prices = np.concatenate([highs, lows])
    bins = ((all_prices - all_prices.min()) / bucket).astype(int)
    counts = np.bincount(bins)
    top_bins = np.argsort(counts)[-3:]  # top 3 levels
    levels = [(all_prices.min() + b * bucket + bucket / 2, counts[b]) for b in top_bins if counts[b] >= 4]
    if not levels:
        return None
    price = float(df["Close"].iloc[i])
    for lvl, strength in levels:
        if abs(price - lvl) < atr * 0.3:
            if price > lvl:
                return "long"  # bouncing off support
            else:
                return "short"  # rejected at resistance
    return None


# ═══════════════════════════════════════════════════════════════
# STRATEGY REGISTRY
# ═══════════════════════════════════════════════════════════════

STRATEGIES = {
    # (name, trigger, base_stop, base_target, base_timeout_bars, trailing, breakeven, description)
    "range_revert":    (trigger_range,     20, 50, 60, 0, 0,  "Mean reversion: range low->long, high->short"),
    "tight_stop":      (trigger_range,     10, 50, 60, 0, 0,  "Range + tight 10-pip stop"),
    "rr_1_3":          (trigger_range,     15, 45, 60, 0, 0,  "Range + 1:3 R:R ratio"),
    "scalp":           (trigger_range,     10, 20, 30, 0, 0,  "Scalp: 10/20 stop/target, 30-bar timeout"),
    "trailing_10":     (trigger_range,     20, 50, 60, 10, 0, "Range + 10-pip trailing stop"),
    "breakeven":       (trigger_range,     20, 50, 60, 0, 10, "Range + move to BE after +10 pips"),
    "timeout_short":   (trigger_range,     20, 50, 30, 0, 0,  "Range + 30-bar timeout (exit fast)"),
    "bb_bounce":       (trigger_bb,        15, 30, 45, 0, 0,  "Bollinger Band bounce"),
    "vwap_revert":     (trigger_vwap,      15, 30, 60, 0, 0,  "VWAP mean reversion (>1 ATR)"),
    "rsi_extreme":     (trigger_rsi,       20, 40, 60, 0, 0,  "RSI extreme (<25 long, >75 short)"),
    "momentum":        (trigger_momentum,  20, 40, 60, 0, 0,  "Momentum breakout (new 60-bar high/low)"),
    "ema_cross":       (trigger_ema_cross, 20, 40, 60, 0, 0,  "EMA 20/50 crossover"),
    "engulfing":       (trigger_engulfing, 15, 30, 45, 0, 0,  "Engulfing candle pattern"),
    "sr_bounce":       (trigger_sr_bounce, 15, 40, 60, 0, 0,  "Support/resistance cluster bounce"),
}


def backtest(pair, df_1m, cfg, strat_name, strat, tf_name, tf_cfg):
    trigger_fn, base_stop, base_target, base_timeout, trailing, breakeven, desc = strat
    stop_mult = tf_cfg["stop_mult"]
    stop = int(base_stop * stop_mult)
    target = int(base_target * stop_mult)
    timeout = int(base_timeout * tf_cfg["timeout_mult"])
    check_every = tf_cfg["check_every"]
    pip = cfg["pip"]
    s_start, s_end = cfg["session"]

    df = resample_df(df_1m, tf_cfg["resample"])
    if len(df) < 200:
        return []

    seed_bars = min(int(len(df) * 0.2), 500)
    trades = []
    last_exit = 0

    for i in range(seed_bars, len(df), check_every):
        if i <= last_exit:
            continue
        hour = df.index[i].hour
        if s_start < s_end:
            if hour < s_start or hour >= s_end:
                continue
        else:
            if hour < s_start and hour >= s_end:
                continue

        direction = trigger_fn(df, i)
        if direction is None:
            continue

        trail_scaled = int(trailing * stop_mult) if trailing else 0
        be_scaled = int(breakeven * stop_mult) if breakeven else 0

        pnl, exit_idx = simulate_trade(
            df, i, direction, pip,
            stop_pips=stop, target_pips=target, timeout_bars=timeout,
            trailing=trail_scaled, breakeven=be_scaled,
        )
        if pnl is not None:
            trades.append(pnl)
            last_exit = exit_idx

    return trades


def compute_stats(trades):
    if not trades:
        return {"T": 0, "WR": 0, "PF": 0, "Total": 0, "PerTrade": 0, "DD": 0}
    wins = [p for p in trades if p > 0]
    losses = [p for p in trades if p <= 0]
    total = sum(trades)
    wr = len(wins) / len(trades) * 100
    pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else 99
    cum = np.cumsum(trades)
    dd = float(np.max(np.maximum.accumulate(cum) - cum)) if len(cum) > 0 else 0
    return {
        "T": len(trades), "WR": round(wr, 1), "PF": round(pf, 2),
        "Total": round(total, 0), "PerTrade": round(total / len(trades), 2), "DD": round(dd, 0),
    }


if __name__ == "__main__":
    print("=" * 100)
    print("  STRATEGY SWEEP — 14 strategies × 10 pairs × 4 timeframes (560 combinations)")
    print("=" * 100)

    pair_data = {}
    for pair in PAIRS:
        df = load_pair(pair)
        if df is not None and len(df) > 5000:
            pair_data[pair] = df
            print(f"  {pair}: {len(df):,} bars ({df.index[-1].date() - df.index[0].date()} days)")
    print()

    # Aggregate results: (strategy, timeframe) -> combined stats
    combo_results = {}

    for strat_name, strat in STRATEGIES.items():
        for tf_name, tf_cfg in TIMEFRAMES.items():
            key = (strat_name, tf_name)
            all_trades = []
            for pair, df_1m in pair_data.items():
                trades = backtest(pair, df_1m, PAIRS[pair], strat_name, strat, tf_name, tf_cfg)
                all_trades.extend(trades)
            combo_results[key] = compute_stats(all_trades)

    # Print ranked results
    ranked = sorted(combo_results.items(), key=lambda x: x[1]["Total"], reverse=True)

    print(f"{'Strategy':<18s} {'TF':>4s} {'Trades':>7s} {'WR':>5s} {'PF':>6s} {'Total':>9s} {'Per/T':>8s} {'MaxDD':>7s}  Description")
    print("-" * 115)
    for (strat_name, tf_name), s in ranked:
        if s["T"] < 20:
            continue
        desc = STRATEGIES[strat_name][-1]
        marker = " ***" if s["PF"] >= 1.5 and s["T"] >= 50 else (" **" if s["PF"] >= 1.3 and s["T"] >= 50 else (" *" if s["PF"] >= 1.1 and s["T"] >= 30 else ""))
        print(f"{strat_name:<18s} {tf_name:>4s} {s['T']:>7d} {s['WR']:>4.0f}% {s['PF']:>5.2f} {s['Total']:>+8.0f}p {s['PerTrade']:>+7.2f}p {s['DD']:>6.0f}p  {desc[:50]}{marker}")

    # Print top 10 with per-pair breakdown
    print("\n" + "=" * 100)
    print("  TOP 10 STRATEGIES — Per-Pair Breakdown")
    print("=" * 100)

    top10 = [(k, v) for k, v in ranked if v["T"] >= 20][:10]
    for (strat_name, tf_name), s in top10:
        strat = STRATEGIES[strat_name]
        tf_cfg = TIMEFRAMES[tf_name]
        desc = strat[-1]
        print(f"\n  [{strat_name} @ {tf_name}] PF={s['PF']:.2f} Total={s['Total']:+.0f}p — {desc}")
        for pair, df_1m in sorted(pair_data.items()):
            trades = backtest(pair, df_1m, PAIRS[pair], strat_name, strat, tf_name, tf_cfg)
            ps = compute_stats(trades)
            if ps["T"] > 0:
                print(f"    {pair:8s} T={ps['T']:3d} WR={ps['WR']:4.0f}% PF={ps['PF']:5.2f} {ps['Total']:+7.0f}p")

    # Print timeframe comparison
    print("\n" + "=" * 100)
    print("  TIMEFRAME COMPARISON (best strategy per TF)")
    print("=" * 100)
    for tf_name in TIMEFRAMES:
        tf_combos = [(k, v) for k, v in ranked if k[1] == tf_name and v["T"] >= 20]
        if tf_combos:
            best = tf_combos[0]
            print(f"  {tf_name:>4s}: {best[0][0]:<18s} PF={best[1]['PF']:.2f} T={best[1]['T']:4d} Total={best[1]['Total']:+.0f}p")
