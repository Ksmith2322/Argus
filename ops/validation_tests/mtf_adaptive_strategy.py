"""Multi-Timeframe Adaptive Strategy — 4H trend / 1H confirmation / 5M execution.

Architecture:
  4H: Determines trend direction + key S/R levels (we only trade WITH the 4H trend)
  1H: Confirms setup — pullback to level, break of structure, or momentum alignment
  5M: Execution trigger — precise entry when 4H+1H agree

Target: Middle of the move. Not the reversal, not the top. Enter after confirmation,
exit before exhaustion. Capture 40-60% of the move.

All tests use honest costs: 1 pip slippage + 0.2 pip commission per trade.
"""
import json, sys, math
from pathlib import Path
from datetime import datetime, timezone
import numpy as np
import pandas as pd
from scipy import stats as sp_stats

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

RESULTS_DIR = Path("ops/validation_tests/results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

print("=" * 70)
print("  MTF ADAPTIVE STRATEGY — 4H / 1H / 5M")
print("  'The middle of the move is where we make money'")
print("=" * 70)

# ── Load and resample data ──
data_dir = Path("argus_flow/data")
datasets = {}
for f in sorted(data_dir.glob("ibkr_*_1m.csv")):
    sym = f.stem.replace("ibkr_", "").replace("_1m", "").replace("_extended", "").upper()
    if len(sym) == 6 and not any(c.isdigit() for c in sym) and sym not in datasets:
        df = pd.read_csv(f)
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = df.set_index("ts").sort_index()
        datasets[sym] = df

def resample_ohlcv(df, freq):
    """Resample 1m bars to higher timeframe."""
    return df.resample(freq).agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()

def compute_sr_levels(df_4h, lookback=20):
    """Find key S/R levels from 4H swing highs/lows."""
    highs = df_4h["high"].rolling(lookback).max()
    lows = df_4h["low"].rolling(lookback).min()
    levels = []
    for i in range(5, len(df_4h)):
        h = df_4h["high"].iloc[i]
        l = df_4h["low"].iloc[i]
        # Swing high: higher than 2 bars each side
        if i >= 2 and i < len(df_4h) - 2:
            if (h >= df_4h["high"].iloc[i-2:i].max() and
                h >= df_4h["high"].iloc[i+1:i+3].max() if i+3 <= len(df_4h) else True):
                levels.append({"price": h, "type": "resistance", "idx": i})
            if (l <= df_4h["low"].iloc[i-2:i].min() and
                l <= df_4h["low"].iloc[i+1:i+3].min() if i+3 <= len(df_4h) else True):
                levels.append({"price": l, "type": "support", "idx": i})
    return levels

def analyze_4h(df_4h, current_idx):
    """4H analysis: trend direction + nearest S/R levels."""
    if current_idx < 20:
        return {"trend": "NONE", "support": 0, "resistance": 0, "strength": 0}

    window = df_4h.iloc[max(0, current_idx-20):current_idx+1]
    closes = window["close"].values

    # Trend: EMA 8 vs EMA 21
    ema8 = pd.Series(closes).ewm(span=8).mean().iloc[-1]
    ema21 = pd.Series(closes).ewm(span=21).mean().iloc[-1]
    current = closes[-1]

    if ema8 > ema21 and current > ema21:
        trend = "UP"
        strength = (ema8 - ema21) / ema21 * 10000  # in pips-equivalent
    elif ema8 < ema21 and current < ema21:
        trend = "DOWN"
        strength = (ema21 - ema8) / ema21 * 10000
    else:
        trend = "RANGE"
        strength = 0

    # Nearest S/R
    recent_highs = window["high"].nlargest(3).values
    recent_lows = window["low"].nsmallest(3).values
    resistance = float(np.mean(recent_highs)) if len(recent_highs) > 0 else current * 1.01
    support = float(np.mean(recent_lows)) if len(recent_lows) > 0 else current * 0.99

    return {"trend": trend, "support": support, "resistance": resistance,
            "strength": round(strength, 1), "ema8": ema8, "ema21": ema21}

def analyze_1h(df_1h, current_idx, trend_4h):
    """1H analysis: confirmation of 4H setup."""
    if current_idx < 20:
        return {"confirmed": False, "reason": "insufficient_data", "setup": "NONE"}

    window = df_1h.iloc[max(0, current_idx-20):current_idx+1]
    closes = window["close"].values
    highs = window["high"].values
    lows = window["low"].values
    current = closes[-1]

    # EMA alignment
    ema8 = pd.Series(closes).ewm(span=8).mean().iloc[-1]
    ema21 = pd.Series(closes).ewm(span=21).mean().iloc[-1]

    # RSI
    delta = pd.Series(closes).diff()
    gain = delta.clip(lower=0).rolling(14).mean().iloc[-1]
    loss = (-delta.clip(upper=0)).rolling(14).mean().iloc[-1]
    rsi = 100 - (100 / (1 + gain / max(loss, 1e-10)))

    confirmed = False
    setup = "NONE"
    reason = ""

    if trend_4h == "UP":
        # Look for: pullback to support (RSI 40-60) OR break of 1H high
        pullback = current < ema21 * 1.001  # price near or below 1H EMA21
        rsi_ok = 35 < rsi < 65  # not overbought
        new_high = current > max(highs[-5:-1]) if len(highs) > 5 else False

        if pullback and rsi_ok:
            confirmed = True
            setup = "PULLBACK_TO_EMA"
            reason = f"1H pullback to EMA21, RSI={rsi:.0f}"
        elif new_high and rsi < 75:
            confirmed = True
            setup = "BREAK_OF_HIGH"
            reason = f"1H new high, RSI={rsi:.0f}"
        elif ema8 > ema21 and rsi > 50:
            confirmed = True
            setup = "TREND_CONTINUATION"
            reason = f"1H EMAs aligned UP, RSI={rsi:.0f}"

    elif trend_4h == "DOWN":
        pullback = current > ema21 * 0.999
        rsi_ok = 35 < rsi < 65
        new_low = current < min(lows[-5:-1]) if len(lows) > 5 else False

        if pullback and rsi_ok:
            confirmed = True
            setup = "PULLBACK_TO_EMA"
            reason = f"1H pullback to EMA21, RSI={rsi:.0f}"
        elif new_low and rsi > 25:
            confirmed = True
            setup = "BREAK_OF_LOW"
            reason = f"1H new low, RSI={rsi:.0f}"
        elif ema8 < ema21 and rsi < 50:
            confirmed = True
            setup = "TREND_CONTINUATION"
            reason = f"1H EMAs aligned DOWN, RSI={rsi:.0f}"

    return {"confirmed": confirmed, "setup": setup, "reason": reason,
            "rsi": round(rsi, 1), "ema8": ema8, "ema21": ema21}

def find_5m_entry(df_5m, start_idx, direction, max_wait=12):
    """5M execution: find precise entry within max_wait bars (1 hour).

    For LONG: wait for bullish engulfing, hammer, or break of 5M high
    For SHORT: wait for bearish engulfing, shooting star, or break of 5M low
    """
    if start_idx + max_wait >= len(df_5m):
        return None

    for i in range(start_idx, min(start_idx + max_wait, len(df_5m) - 1)):
        bar = df_5m.iloc[i]
        prev = df_5m.iloc[i - 1] if i > 0 else bar
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
        po, pc = prev["open"], prev["close"]
        body = abs(c - o)
        bar_range = h - l
        if bar_range <= 0:
            continue

        if direction == "long":
            # Bullish signals: close > open, engulfing prev, or hammer
            bullish_candle = c > o and body > bar_range * 0.4
            engulfing = c > o and c > max(po, pc) and o < min(po, pc)
            hammer = (min(o, c) - l) > body * 2 and (h - max(o, c)) < body * 0.5
            if bullish_candle or engulfing or hammer:
                return {"bar_idx": i + 1, "entry_price": df_5m.iloc[i + 1]["open"] if i + 1 < len(df_5m) else c,
                        "trigger": "engulfing" if engulfing else ("hammer" if hammer else "bullish_bar")}

        elif direction == "short":
            bearish_candle = c < o and body > bar_range * 0.4
            engulfing = c < o and c < min(po, pc) and o > max(po, pc)
            shooting_star = (h - max(o, c)) > body * 2 and (min(o, c) - l) < body * 0.5
            if bearish_candle or engulfing or shooting_star:
                return {"bar_idx": i + 1, "entry_price": df_5m.iloc[i + 1]["open"] if i + 1 < len(df_5m) else c,
                        "trigger": "engulfing" if engulfing else ("shooting_star" if shooting_star else "bearish_bar")}

    return None  # No entry trigger within window

def simulate_mtf(df_1m, symbol, stop_pips, target_pips, timeout_bars_5m=36,
                 slippage_pips=1.0, commission_pips=0.2):
    """Run the full MTF strategy simulation."""
    pip_size = 0.01 if "JPY" in symbol else 0.0001

    # Build timeframes
    df_5m = resample_ohlcv(df_1m, "5min")
    df_1h = resample_ohlcv(df_1m, "1h")
    df_4h = resample_ohlcv(df_1m, "4h")

    if len(df_4h) < 25 or len(df_1h) < 25 or len(df_5m) < 100:
        return []

    trades = []
    last_entry_5m_idx = -36  # min gap between trades

    # Map 5m bars to their corresponding 1H and 4H bar indices
    h1_times = df_1h.index
    h4_times = df_4h.index

    for i in range(60, len(df_5m) - timeout_bars_5m - 2):
        if i - last_entry_5m_idx < 36:  # min 3 hour gap between trades
            continue

        current_5m_time = df_5m.index[i]

        # Find corresponding 4H and 1H bars
        h4_idx = h4_times.searchsorted(current_5m_time, side="right") - 1
        h1_idx = h1_times.searchsorted(current_5m_time, side="right") - 1
        if h4_idx < 20 or h1_idx < 20:
            continue

        # STEP 1: 4H trend analysis
        analysis_4h = analyze_4h(df_4h, h4_idx)
        trend = analysis_4h["trend"]
        if trend == "RANGE" or trend == "NONE":
            continue  # Only trade with clear trend

        # STEP 2: 1H confirmation
        direction = "long" if trend == "UP" else "short"
        analysis_1h = analyze_1h(df_1h, h1_idx, trend)
        if not analysis_1h["confirmed"]:
            continue

        # STEP 3: 5M execution trigger
        entry_signal = find_5m_entry(df_5m, i, direction, max_wait=12)
        if entry_signal is None:
            continue

        entry_idx = entry_signal["bar_idx"]
        entry_px = entry_signal["entry_price"]

        # Apply slippage
        if direction == "long":
            entry_px += slippage_pips * pip_size
            stop_px = entry_px - stop_pips * pip_size
            target_px = entry_px + target_pips * pip_size
        else:
            entry_px -= slippage_pips * pip_size
            stop_px = entry_px + stop_pips * pip_size
            target_px = entry_px - target_pips * pip_size

        # Simulate trade on 5m bars
        exit_reason = "timeout"
        exit_px = None
        exit_idx = min(entry_idx + timeout_bars_5m, len(df_5m) - 1)

        for j in range(entry_idx, exit_idx + 1):
            bar = df_5m.iloc[j]
            if direction == "long":
                if bar["low"] <= stop_px:
                    exit_reason = "stop"
                    exit_px = stop_px
                    exit_idx = j
                    break
                if bar["high"] >= target_px:
                    exit_reason = "target"
                    exit_px = target_px
                    exit_idx = j
                    break
            else:
                if bar["high"] >= stop_px:
                    exit_reason = "stop"
                    exit_px = stop_px
                    exit_idx = j
                    break
                if bar["low"] <= target_px:
                    exit_reason = "target"
                    exit_px = target_px
                    exit_idx = j
                    break

        if exit_px is None:
            exit_px = df_5m.iloc[exit_idx]["close"]
            if direction == "long":
                exit_px -= slippage_pips * pip_size
            else:
                exit_px += slippage_pips * pip_size

        # PnL
        if direction == "long":
            pnl = (exit_px - entry_px) / pip_size
        else:
            pnl = (entry_px - exit_px) / pip_size
        pnl -= commission_pips

        trades.append({
            "entry_time": str(df_5m.index[entry_idx]),
            "exit_time": str(df_5m.index[exit_idx]),
            "direction": direction,
            "entry_px": round(entry_px, 5),
            "exit_px": round(exit_px, 5),
            "pnl": round(pnl, 2),
            "exit_reason": exit_reason,
            "setup_4h": trend,
            "setup_1h": analysis_1h["setup"],
            "trigger_5m": entry_signal["trigger"],
            "duration_bars": exit_idx - entry_idx,
            "hour": df_5m.index[entry_idx].hour,
        })
        last_entry_5m_idx = entry_idx

    return trades

def evaluate(trades):
    if not trades:
        return {"count": 0}
    pnls = [t["pnl"] for t in trades]
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    net = sum(pnls)
    wr = len(wins) / n
    pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else 999
    exp = net / n
    p_val = sp_stats.ttest_1samp(pnls, 0)[1] if n >= 10 else 1.0
    exits = {}
    for t in trades:
        exits[t["exit_reason"]] = exits.get(t["exit_reason"], 0) + 1
    setups = {}
    for t in trades:
        k = f"{t['setup_1h']}_{t['trigger_5m']}"
        setups[k] = setups.get(k, 0) + 1
    return {"count": n, "wr": round(wr, 4), "pf": round(pf, 4) if pf != 999 else 999,
            "exp": round(exp, 2), "net": round(net, 1), "p": round(p_val, 4),
            "exits": exits, "setups": setups}

# ══════════════════════════════════════════════════════════════
# RUN ON ALL PAIRS
# ══════════════════════════════════════════════════════════════

all_results = []

# Test multiple stop/target combos
CONFIGS = [
    {"stop": 15, "target": 20, "timeout": 36, "label": "15/20 (3hr)"},
    {"stop": 15, "target": 30, "timeout": 36, "label": "15/30 (3hr)"},
    {"stop": 20, "target": 25, "timeout": 48, "label": "20/25 (4hr)"},
    {"stop": 20, "target": 40, "timeout": 48, "label": "20/40 (4hr)"},
    {"stop": 25, "target": 35, "timeout": 36, "label": "25/35 (3hr)"},
    {"stop": 10, "target": 15, "timeout": 24, "label": "10/15 (2hr)"},
]

for cfg in CONFIGS:
    print(f"\n{'='*70}")
    print(f"  CONFIG: stop={cfg['stop']} target={cfg['target']} timeout={cfg['timeout']}x5m={cfg['timeout']*5}min")
    print(f"{'='*70}")
    print(f"{'Pair':8s} {'#':>4s} {'WR':>6s} {'PF':>7s} {'Exp':>7s} {'Net':>8s} {'p':>6s} {'Exits':>25s} {'Top Setup':>30s}", flush=True)
    print("-" * 110, flush=True)

    for symbol, df_1m in sorted(datasets.items()):
        trades = simulate_mtf(df_1m, symbol,
                              stop_pips=cfg["stop"], target_pips=cfg["target"],
                              timeout_bars_5m=cfg["timeout"])
        m = evaluate(trades)
        if m["count"] == 0:
            continue

        top_setup = max(m.get("setups", {}).items(), key=lambda x: x[1])[0] if m.get("setups") else ""
        exits_str = str(m.get("exits", {}))[:25]
        print(f"{symbol:8s} {m['count']:4d} {m['wr']:6.1%} {m.get('pf',0):7.2f} "
              f"{m['exp']:+7.2f} {m['net']:+8.1f} {m['p']:6.3f} {exits_str:>25s} {top_setup:>30s}", flush=True)

        all_results.append({"symbol": symbol, "config": cfg["label"], **m})

# ══════════════════════════════════════════════════════════════
# WALK-FORWARD on winners
# ══════════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print("  WALK-FORWARD on profitable configs (PF > 1.0, n >= 15)")
print(f"{'='*70}\n")

profitable = [r for r in all_results if r.get("pf", 0) > 1.0 and r.get("count", 0) >= 15]
profitable.sort(key=lambda x: x.get("exp", 0) * x.get("count", 0), reverse=True)
print(f"Profitable: {len(profitable)} configs", flush=True)

for r in profitable[:10]:
    print(f"  {r.get('config',''):12s} {r['symbol']:8s} n={r['count']:3d} PF={r['pf']:.2f} "
          f"exp={r['exp']:+.2f} p={r['p']:.3f}", flush=True)

for r in profitable[:5]:
    sym = r["symbol"]
    if sym not in datasets:
        continue
    df_1m = datasets[sym]
    n_bars = len(df_1m)
    fold_size = n_bars // 5
    # Parse config
    parts = r["config"].split("/")
    stop = int(parts[0])
    target = int(parts[1].split(" ")[0])
    timeout = {"2hr": 24, "3hr": 36, "4hr": 48}.get(parts[1].split("(")[1].replace(")", ""), 36)

    print(f"\n  WF: {r['config']} {sym}", flush=True)
    fold_pfs = []
    for fold in range(4):
        test_start = fold_size * (fold + 1)
        test_end = min(test_start + fold_size, n_bars)
        test_df = df_1m.iloc[test_start:test_end].copy()
        if len(test_df) < 1000:
            continue
        trades = simulate_mtf(test_df, sym, stop_pips=stop, target_pips=target, timeout_bars_5m=timeout)
        m = evaluate(trades)
        fold_pfs.append(m.get("pf", 0))
        print(f"    Fold {fold+1}: n={m['count']:3d} PF={m.get('pf',0):5.2f} exp={m.get('exp',0):+.2f}", flush=True)
    if fold_pfs:
        print(f"    => Avg OOS PF: {np.mean(fold_pfs):.2f}, Profitable: {sum(1 for p in fold_pfs if p > 1.0)}/{len(fold_pfs)}", flush=True)

# ══════════════════════════════════════════════════════════════
# FINAL SUMMARY
# ══════════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print("  FINAL RANKING — All MTF results")
print(f"{'='*70}\n")

all_results.sort(key=lambda x: x.get("exp", 0) * x.get("count", 0), reverse=True)
for i, r in enumerate(all_results[:20], 1):
    sig = "*" if r.get("p", 1) < 0.1 else " "
    print(f"  {i:2d}. {r.get('config',''):12s} {r['symbol']:8s} n={r['count']:3d} PF={r.get('pf',0):5.2f} "
          f"exp={r['exp']:+.2f} net={r['net']:+.1f} p={r.get('p',1):.3f}{sig}", flush=True)

# Save
output = RESULTS_DIR / "mtf_strategy_results.json"
with open(output, "w") as f:
    json.dump(all_results, f, indent=2, default=str)
print(f"\nSaved to {output}")
