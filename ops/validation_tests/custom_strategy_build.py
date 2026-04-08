"""CUSTOM STRATEGY BUILD — Data-up design, not theory-down.

Uses edge discovery results to build and test a custom strategy that trades
the ACTUAL patterns found in data, not pre-conceived notions.

Approach:
1. For each pair, load forward return data
2. Build conditional entry rules from discovered profitable combinations
3. Test with tight stops matching actual MFE
4. Walk-forward validate to confirm out-of-sample
5. Portfolio combine the survivors

This bypasses the existing backtest engine — it's a pure forward-return analysis.
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
print("  CUSTOM STRATEGY BUILDER v1")
print("  Pure data-driven: entry conditions from forward return analysis")
print("=" * 70)

# Load all FX data
data_dir = Path("argus_flow/data")
datasets = {}
for f in sorted(data_dir.glob("ibkr_*_1m.csv")):
    sym = f.stem.replace("ibkr_", "").replace("_1m", "").replace("_extended", "").upper()
    if len(sym) == 6 and not any(c.isdigit() for c in sym) and sym not in datasets:
        datasets[sym] = pd.read_csv(f)
        datasets[sym]["ts"] = pd.to_datetime(datasets[sym]["ts"], utc=True)

def compute_features(df):
    """Compute features needed for strategy."""
    df = df.copy()
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    o = df["open"].astype(float)

    df["range"] = h - l
    df["atr_14"] = df["range"].rolling(14).mean()
    df["range_pct"] = df["range"] / c
    df["sma_20"] = c.rolling(20).mean()
    df["sma_60"] = c.rolling(60).mean()

    # RSI
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.clip(lower=1e-10)
    df["rsi"] = 100 - (100 / (1 + rs))

    # Position in range
    h20 = h.rolling(20).max()
    l20 = l.rolling(20).min()
    r20 = (h20 - l20).clip(lower=1e-10)
    df["dist_from_low"] = (c - l20) / r20

    # Efficiency
    df["eff_ratio"] = abs(c - c.shift(20)) / df["range"].rolling(20).sum().clip(lower=1e-10)

    # Time
    df["hour"] = df["ts"].dt.hour
    df["dow"] = df["ts"].dt.dayofweek

    # Forward returns (for labeling)
    for n in [15, 30, 45, 60]:
        df[f"fwd_{n}"] = c.shift(-n) - c
        df[f"fwd_{n}_high"] = h.rolling(n).max().shift(-n) - c  # MFE long
        df[f"fwd_{n}_low"] = c - l.rolling(n).min().shift(-n)   # MFE short

    return df

def simulate_strategy(df, rules, pip_size, stop_pips, target_pips, timeout_bars, min_gap_bars=30,
                      slippage_pips=1.0, commission_pips=0.2):
    """Simulate a strategy with given rules on labeled data.

    rules: list of dicts, each with:
        - conditions: dict of {feature: (min, max)} filters
        - direction: "long" or "short"
    """
    trades = []
    last_entry_idx = -min_gap_bars

    for i in range(200, len(df) - timeout_bars - 1):
        if i - last_entry_idx < min_gap_bars:
            continue

        row = df.iloc[i]

        # Check rules
        direction = None
        for rule in rules:
            match = True
            for feat, (lo, hi) in rule["conditions"].items():
                val = row.get(feat, None)
                if val is None or pd.isna(val):
                    match = False
                    break
                if not (lo <= val <= hi):
                    match = False
                    break
            if match:
                direction = rule["direction"]
                break

        if direction is None:
            continue

        # Simulate trade using forward bar data
        entry_px = df.iloc[i + 1]["open"] if i + 1 < len(df) else row["close"]
        entry_px_adj = entry_px + (slippage_pips * pip_size if direction == "long" else -slippage_pips * pip_size)

        stop_dist = stop_pips * pip_size
        target_dist = target_pips * pip_size

        if direction == "long":
            stop_px = entry_px_adj - stop_dist
            target_px = entry_px_adj + target_dist
        else:
            stop_px = entry_px_adj + stop_dist
            target_px = entry_px_adj - target_dist

        exit_reason = "timeout"
        exit_px = None
        exit_bar = min(i + 1 + timeout_bars, len(df) - 1)

        # Check each bar for stop/target hit
        for j in range(i + 1, exit_bar + 1):
            bar = df.iloc[j]
            if direction == "long":
                if bar["low"] <= stop_px:
                    exit_reason = "stop"
                    exit_px = stop_px
                    exit_bar = j
                    break
                if bar["high"] >= target_px:
                    exit_reason = "target"
                    exit_px = target_px
                    exit_bar = j
                    break
            else:
                if bar["high"] >= stop_px:
                    exit_reason = "stop"
                    exit_px = stop_px
                    exit_bar = j
                    break
                if bar["low"] <= target_px:
                    exit_reason = "target"
                    exit_px = target_px
                    exit_bar = j
                    break

        if exit_px is None:
            exit_px = df.iloc[exit_bar]["close"]
            # Apply slippage to timeout exit
            exit_px = exit_px - (slippage_pips * pip_size if direction == "long" else -slippage_pips * pip_size)

        # PnL in pips
        if direction == "long":
            pnl = (exit_px - entry_px_adj) / pip_size
        else:
            pnl = (entry_px_adj - exit_px) / pip_size

        pnl -= commission_pips  # round-trip commission

        trades.append({
            "entry_bar": i, "exit_bar": exit_bar, "direction": direction,
            "entry_px": entry_px_adj, "exit_px": exit_px,
            "pnl": round(pnl, 2), "exit_reason": exit_reason,
            "hour": int(row["hour"]), "dow": int(row["dow"]),
            "duration_bars": exit_bar - i,
        })
        last_entry_idx = i

    return trades

def evaluate_trades(trades):
    """Compute metrics from trade list."""
    if not trades:
        return {"count": 0}
    pnls = [t["pnl"] for t in trades]
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    net = sum(pnls)
    wr = len(wins) / n if n > 0 else 0
    pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else (999 if wins else 0)
    exp = net / n if n > 0 else 0

    p_val = 1.0
    if n >= 10:
        _, p_val = sp_stats.ttest_1samp(pnls, 0)

    exits = {}
    for t in trades:
        exits[t["exit_reason"]] = exits.get(t["exit_reason"], 0) + 1

    return {
        "count": n, "win_rate": round(wr, 4), "profit_factor": round(pf, 4) if pf != 999 else 999,
        "expectancy": round(exp, 2), "net_pnl": round(net, 1),
        "p_value": round(p_val, 4), "exits": exits,
        "avg_duration": round(np.mean([t["duration_bars"] for t in trades]), 0),
    }

# ══════════════════════════════════════════════════════════════
# STRATEGY DEFINITIONS — built from edge discovery data
# ══════════════════��═══════════════════════════════════════════

# Strategy 1: "Scalp the drift" — tight targets, session-specific direction
STRATEGIES = {
    "drift_scalp_v1": {
        "description": "Tight targets (8-12 pips), trade with discovered directional bias per hour",
        "configs": {
            "GBPJPY": {
                "rules": [
                    {"conditions": {"hour": (0, 1), "rsi": (40, 70), "eff_ratio": (0, 0.3)}, "direction": "long"},
                    {"conditions": {"hour": (4, 4), "rsi": (40, 70)}, "direction": "long"},
                    {"conditions": {"hour": (8, 8), "rsi": (30, 60)}, "direction": "long"},
                    {"conditions": {"hour": (14, 15), "rsi": (40, 80)}, "direction": "long"},
                    {"conditions": {"hour": (9, 9), "rsi": (30, 60)}, "direction": "short"},
                    {"conditions": {"hour": (12, 13), "rsi": (40, 70)}, "direction": "short"},
                    {"conditions": {"hour": (18, 18), "rsi": (50, 80)}, "direction": "short"},
                ],
                "stop_pips": 15, "target_pips": 12, "timeout_bars": 45,
            },
            "USDJPY": {
                "rules": [
                    {"conditions": {"hour": (0, 1), "rsi": (40, 80), "dist_from_low": (0.3, 1.0)}, "direction": "long"},
                    {"conditions": {"hour": (7, 8), "rsi": (30, 70)}, "direction": "long"},
                    {"conditions": {"hour": (14, 15), "rsi": (40, 80)}, "direction": "long"},
                    {"conditions": {"hour": (9, 9), "rsi": (20, 50)}, "direction": "short"},
                    {"conditions": {"hour": (18, 18), "rsi": (50, 80)}, "direction": "short"},
                ],
                "stop_pips": 12, "target_pips": 10, "timeout_bars": 30,
            },
            "AUDJPY": {
                "rules": [
                    {"conditions": {"hour": (0, 1), "rsi": (30, 70), "dist_from_low": (0, 0.5)}, "direction": "long"},
                    {"conditions": {"hour": (4, 4), "rsi": (30, 70)}, "direction": "long"},
                    {"conditions": {"hour": (8, 8), "rsi": (30, 70)}, "direction": "long"},
                    {"conditions": {"hour": (14, 15), "rsi": (40, 80)}, "direction": "long"},
                    {"conditions": {"hour": (9, 9), "rsi": (20, 50)}, "direction": "short"},
                    {"conditions": {"hour": (12, 12), "rsi": (30, 60)}, "direction": "short"},
                ],
                "stop_pips": 15, "target_pips": 12, "timeout_bars": 45,
            },
            "EURUSD": {
                "rules": [
                    {"conditions": {"hour": (9, 9), "rsi": (50, 80)}, "direction": "short"},
                    {"conditions": {"hour": (6, 7), "rsi": (60, 90)}, "direction": "short"},
                    {"conditions": {"hour": (13, 13), "rsi": (50, 80)}, "direction": "short"},
                    {"conditions": {"hour": (18, 18), "rsi": (60, 90)}, "direction": "short"},
                    {"conditions": {"hour": (21, 21), "rsi": (50, 80)}, "direction": "short"},
                    {"conditions": {"hour": (12, 12), "rsi": (30, 50)}, "direction": "long"},
                    {"conditions": {"hour": (14, 15), "rsi": (30, 60)}, "direction": "long"},
                ],
                "stop_pips": 12, "target_pips": 10, "timeout_bars": 30,
            },
            "GBPUSD": {
                "rules": [
                    {"conditions": {"hour": (5, 6), "rsi": (50, 80)}, "direction": "short"},
                    {"conditions": {"hour": (8, 9), "rsi": (50, 80)}, "direction": "short"},
                    {"conditions": {"hour": (13, 13), "rsi": (50, 80)}, "direction": "short"},
                    {"conditions": {"hour": (16, 16), "rsi": (50, 80)}, "direction": "short"},
                    {"conditions": {"hour": (14, 14), "rsi": (30, 50)}, "direction": "long"},
                    {"conditions": {"hour": (19, 19), "rsi": (20, 50)}, "direction": "long"},
                ],
                "stop_pips": 12, "target_pips": 10, "timeout_bars": 30,
            },
        }
    },
    "mean_revert_rsi_v1": {
        "description": "Mean reversion: buy oversold (<30 RSI at range bottom), sell overbought (>70 at range top)",
        "configs": {
            sym: {
                "rules": [
                    {"conditions": {"rsi": (0, 30), "dist_from_low": (0, 0.3)}, "direction": "long"},
                    {"conditions": {"rsi": (70, 100), "dist_from_low": (0.7, 1.0)}, "direction": "short"},
                ],
                "stop_pips": 15 if "JPY" not in sym else 20,
                "target_pips": 10 if "JPY" not in sym else 15,
                "timeout_bars": 60,
            }
            for sym in ["EURUSD", "GBPUSD", "USDJPY", "AUDJPY", "GBPJPY", "AUDCAD", "USDCHF", "EURJPY",
                        "AUDUSD", "GBPAUD", "EURGBP", "NZDUSD", "NZDJPY", "CADJPY", "CHFJPY", "EURAUD"]
        }
    },
    "momentum_burst_v1": {
        "description": "Enter on strong momentum (high range_pct + RSI extreme) in trend direction",
        "configs": {
            sym: {
                "rules": [
                    {"conditions": {"range_pct": (0.001, 1.0), "rsi": (55, 80), "eff_ratio": (0.2, 1.0)}, "direction": "long"},
                    {"conditions": {"range_pct": (0.001, 1.0), "rsi": (20, 45), "eff_ratio": (0.2, 1.0)}, "direction": "short"},
                ],
                "stop_pips": 20 if "JPY" not in sym else 25,
                "target_pips": 15 if "JPY" not in sym else 20,
                "timeout_bars": 45,
            }
            for sym in ["EURUSD", "GBPUSD", "USDJPY", "AUDJPY", "GBPJPY", "AUDCAD", "USDCHF", "EURJPY",
                        "AUDUSD", "GBPAUD", "EURGBP", "NZDUSD", "NZDJPY", "CADJPY", "CHFJPY", "EURAUD"]
        }
    },
}

# ══════════════════════════════════════════════════════════════
# RUN ALL CUSTOM STRATEGIES
# ══════════════��═══════════════════════════════════════════════

all_results = []

for strat_name, strat_def in STRATEGIES.items():
    print(f"\n{'='*70}")
    print(f"  STRATEGY: {strat_name}")
    print(f"  {strat_def['description']}")
    print(f"{'='*70}")
    print(f"{'Pair':8s} {'#':>4s} {'WR':>6s} {'PF':>7s} {'Exp':>7s} {'Net':>8s} {'p-val':>6s} {'Exits':>30s}", flush=True)
    print("-" * 80, flush=True)

    for symbol, cfg in strat_def["configs"].items():
        if symbol not in datasets:
            continue
        df = datasets[symbol]
        pip_size = 0.01 if "JPY" in symbol else 0.0001

        df_feat = compute_features(df)
        df_feat = df_feat.dropna(subset=["rsi", "atr_14"])

        trades = simulate_strategy(
            df_feat, cfg["rules"], pip_size,
            stop_pips=cfg["stop_pips"], target_pips=cfg["target_pips"],
            timeout_bars=cfg["timeout_bars"], min_gap_bars=30,
            slippage_pips=1.0, commission_pips=0.2,
        )
        metrics = evaluate_trades(trades)

        result = {"strategy": strat_name, "symbol": symbol, **metrics, **cfg}
        all_results.append(result)

        exits_str = str(metrics.get("exits", {}))[:30]
        print(f"{symbol:8s} {metrics['count']:4d} {metrics.get('win_rate',0):6.1%} "
              f"{metrics.get('profit_factor',0):7.2f} {metrics.get('expectancy',0):+7.2f} "
              f"{metrics.get('net_pnl',0):+8.1f} {metrics.get('p_value',1):6.3f} {exits_str:>30s}", flush=True)

# ══════════════════════════════════════════════════════════════
# WALK-FORWARD on profitable configs
# ═════════════════════���════════════════════════════════════════
print(f"\n{'='*70}")
print("  WALK-FORWARD VALIDATION on profitable configs (PF > 1.0, n >= 15)")
print(f"{'='*70}\n")

profitable = [r for r in all_results if r.get("profit_factor", 0) > 1.0 and r.get("count", 0) >= 15]
profitable.sort(key=lambda x: x.get("expectancy", 0) * x.get("count", 0), reverse=True)

print(f"Profitable configs: {len(profitable)}", flush=True)
for r in profitable[:10]:
    print(f"  {r['strategy']:20s} {r['symbol']:8s} n={r['count']:3d} PF={r['profit_factor']:.2f} "
          f"exp={r['expectancy']:+.2f} p={r['p_value']:.3f}", flush=True)

wf_results = []
for r in profitable[:10]:
    sym = r["symbol"]
    strat_name = r["strategy"]
    strat_cfg = STRATEGIES[strat_name]["configs"].get(sym)
    if not strat_cfg or sym not in datasets:
        continue

    df = datasets[sym]
    pip_size = 0.01 if "JPY" in sym else 0.0001
    n_bars = len(df)
    fold_size = n_bars // 5

    print(f"\n  WF: {strat_name} {sym}", flush=True)
    fold_pfs = []
    for fold in range(4):
        test_start = fold_size * (fold + 1)
        test_end = min(test_start + fold_size, n_bars)
        test_df = df.iloc[test_start:test_end].copy().reset_index(drop=True)
        if len(test_df) < 300:
            continue

        test_df = compute_features(test_df)
        test_df = test_df.dropna(subset=["rsi", "atr_14"])

        trades = simulate_strategy(
            test_df, strat_cfg["rules"], pip_size,
            stop_pips=strat_cfg["stop_pips"], target_pips=strat_cfg["target_pips"],
            timeout_bars=strat_cfg["timeout_bars"], min_gap_bars=30,
            slippage_pips=1.0, commission_pips=0.2,
        )
        metrics = evaluate_trades(trades)
        fold_pfs.append(metrics.get("profit_factor", 0))
        print(f"    Fold {fold+1}: n={metrics['count']:3d} PF={metrics.get('profit_factor',0):5.2f} "
              f"exp={metrics.get('expectancy',0):+.2f}", flush=True)

    if fold_pfs:
        avg_pf = np.mean(fold_pfs)
        profitable_folds = sum(1 for p in fold_pfs if p > 1.0)
        print(f"    => Avg OOS PF: {avg_pf:.2f}, Profitable folds: {profitable_folds}/{len(fold_pfs)}", flush=True)
        wf_results.append({
            "strategy": strat_name, "symbol": sym,
            "insample_pf": r["profit_factor"], "avg_oos_pf": round(avg_pf, 2),
            "profitable_folds": f"{profitable_folds}/{len(fold_pfs)}",
        })

# ══════════════════════════════��═══════════════════════════════
# FINAL RANKING
# ═══════════════════���══════════════════════════════════════════
print(f"\n{'='*70}")
print("  FINAL RANKING — All strategies sorted by total edge")
print(f"{'='*70}\n")

all_results.sort(key=lambda x: x.get("expectancy", 0) * x.get("count", 0), reverse=True)
print(f"{'#':>3s} {'Strategy':20s} {'Pair':8s} {'N':>4s} {'WR':>6s} {'PF':>7s} {'Exp':>7s} {'Net':>8s} {'p':>6s}", flush=True)
print("-" * 75, flush=True)
for i, r in enumerate(all_results[:30], 1):
    sig = "*" if r.get("p_value", 1) < 0.1 else " "
    print(f"{i:3d} {r['strategy']:20s} {r['symbol']:8s} {r['count']:4d} {r.get('win_rate',0):6.1%} "
          f"{r.get('profit_factor',0):7.2f} {r.get('expectancy',0):+7.2f} {r.get('net_pnl',0):+8.1f} "
          f"{r.get('p_value',1):6.3f}{sig}", flush=True)

# Save
output = RESULTS_DIR / "custom_strategy_results.json"
with open(output, "w") as f:
    json.dump({"all_results": all_results, "walk_forward": wf_results}, f, indent=2, default=str)
print(f"\nSaved to {output}")
