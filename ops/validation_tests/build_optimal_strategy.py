"""Build and test an OPTIMAL strategy derived from edge discovery data.

Key findings from discovery:
1. EURUSD has short bias (-1.81 avg), USDJPY has long bias (+2.06 avg)
2. Targets should be 10-15 pips (not 40) — MFE p75 is only 9-10 pips
3. Best hours are pair-specific (not universal session filter)
4. RSI 40-60 is neutral; extremes (<30 or >70) have directional bias
5. Hold time optimal is 30-60 min (not 120)

Strategy: "Data-Driven Directional" — trade WITH the discovered bias per pair/hour/condition
"""
import json, sys, math, random
from pathlib import Path
import numpy as np
import pandas as pd
from scipy import stats as sp_stats

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from argus_flow.backtest.engine import run_backtest

RESULTS_DIR = Path("ops/validation_tests/results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# Load edge discovery data
discovery = json.loads(Path("ops/validation_tests/results/edge_discovery.json").read_text())

# ── PHASE 1: Optimized range_accel with discovery-driven parameters ──
print("=" * 70)
print("  PHASE 1: Optimized range_accel (discovery-driven params)")
print("=" * 70)

# Test matrix: for each pair, use discovery to pick direction + optimal params
PAIR_CONFIGS = {
    # Pairs with long bias -> keep long entries, block short
    "USDJPY": {"direction_bias": "long", "stop": 15, "target": 15, "timeout": 45, "best_hours": [0,1,3,4,7,8,14,15,20,22]},
    "AUDJPY": {"direction_bias": "long", "stop": 15, "target": 15, "timeout": 45, "best_hours": [0,1,4,7,8,14,15,20,22]},
    "USDCHF": {"direction_bias": "long", "stop": 10, "target": 10, "timeout": 30, "best_hours": [0,1,3,4,8,10,14,15]},
    "AUDCAD": {"direction_bias": "long", "stop": 10, "target": 10, "timeout": 45, "best_hours": [0,2,4,14,19]},
    # Pairs with short bias -> keep short entries, block long
    "EURUSD": {"direction_bias": "short", "stop": 15, "target": 15, "timeout": 45, "best_hours": [0,1,4,5,6,7,9,13,18,21]},
    "GBPUSD": {"direction_bias": "short", "stop": 15, "target": 15, "timeout": 45, "best_hours": [5,6,8,9,13,16,18,21]},
    "GBPAUD": {"direction_bias": "short", "stop": 20, "target": 20, "timeout": 60, "best_hours": [0,1,4,8,10,18,21]},
    "NZDUSD": {"direction_bias": "short", "stop": 10, "target": 10, "timeout": 30, "best_hours": [5,8,9,12,13,17,18,21]},
    # Pairs with no clear bias -> trade both directions during best hours
    "CADJPY": {"direction_bias": "both", "stop": 15, "target": 15, "timeout": 45, "best_hours": [0,1,4,8,14,15,20]},
    "EURJPY": {"direction_bias": "both", "stop": 20, "target": 20, "timeout": 60, "best_hours": [0,1,4,8,14,15,20]},
    "GBPJPY": {"direction_bias": "both", "stop": 25, "target": 25, "timeout": 60, "best_hours": [0,1,4,8,14,15]},
    "EURGBP": {"direction_bias": "both", "stop": 8, "target": 8, "timeout": 30, "best_hours": [0,2,3,4,10,14,19]},
}

DATA = {}
data_dir = Path("argus_flow/data")
for f in sorted(data_dir.glob("ibkr_*_1m.csv")):
    sym = f.stem.replace("ibkr_", "").replace("_1m", "").upper()
    if len(sym) == 6:
        DATA[sym] = str(f)

all_results = []

# For each pair, build optimized config and test
print(f"\n{'Pair':8s} {'Bias':6s} {'S/T':>5s} {'TO':>4s} {'#':>4s} {'WR':>6s} {'PF':>7s} {'Exp':>7s} {'Net':>8s} {'Sharpe':>7s} {'p-val':>6s}", flush=True)
print("-" * 80, flush=True)

for symbol, params in PAIR_CONFIGS.items():
    if symbol not in DATA:
        continue
    bars = pd.read_csv(DATA[symbol])

    # Build config with discovery-driven params
    cfg = {
        "strategy": "range_accel",
        "symbol": symbol,
        "instrument_type": "forex",
        "regime_gate": "LOG_ONLY",
        "trigger": {
            "range_pct_min": 0.0008,  # Slightly looser to get more signals
            "range_accel_min": 0.0,
            "session_start_utc": 0,
            "session_end_utc": 23,  # Full day — hour filter handles timing
        },
        "risk": {
            "stop_pips": params["stop"],
            "target_pips": params["target"],
            "timeout_minutes": params["timeout"],
            "min_signal_gap_minutes": 30,
        },
        "hour_filter": {
            "enabled": True,
            "hours": params["best_hours"],
        },
    }

    try:
        r = run_backtest(cfg, bars, slippage_pips=1.0, commission_per_lot_usd=2.0, friday_close=True)
        m = r.get("metrics", {})
        trades = r.get("trades", [])
        pnls = [t["pnl"] for t in trades]
        n = len(pnls)

        p_val = "n/a"
        if n >= 10:
            _, p_val = sp_stats.ttest_1samp(pnls, 0)
            p_val = f"{p_val:.3f}"

        result = {
            "symbol": symbol, "bias": params["direction_bias"],
            "stop": params["stop"], "target": params["target"], "timeout": params["timeout"],
            "count": n, "wr": m.get("win_rate", 0), "pf": m.get("profit_factor", 0),
            "exp": m.get("expectancy", 0), "net": m.get("net_pnl", 0),
            "sharpe": m.get("sharpe", 0), "p_val": p_val,
            "exits": m.get("exits", {}),
        }
        all_results.append(result)
        print(f"{symbol:8s} {params['direction_bias']:6s} {params['stop']:2d}/{params['target']:2d} {params['timeout']:4d} "
              f"{n:4d} {m.get('win_rate',0):6.1%} {m.get('profit_factor',0):7.2f} {m.get('expectancy',0):+7.2f} "
              f"{m.get('net_pnl',0):+8.1f} {m.get('sharpe',0):+7.2f} {p_val:>6s}", flush=True)
    except Exception as e:
        print(f"{symbol:8s} ERROR: {e}", flush=True)

# ── PHASE 2: Tight target sweep on best pairs ──
print(f"\n{'='*70}")
print("  PHASE 2: Tight target sweep (5-25 pip targets, 30-60 min timeout)")
print("=" * 70)

sweep_results = []
for symbol in ["USDJPY", "AUDJPY", "USDCHF", "EURUSD", "GBPUSD"]:
    if symbol not in DATA or symbol not in PAIR_CONFIGS:
        continue
    bars = pd.read_csv(DATA[symbol])
    params = PAIR_CONFIGS[symbol]

    print(f"\n  {symbol} ({params['direction_bias']} bias):", flush=True)
    best = None
    for stop in [8, 10, 12, 15]:
        for target in [8, 10, 12, 15, 20]:
            for timeout in [30, 45, 60]:
                if target < stop * 0.8:  # target at least 80% of stop
                    continue
                cfg = {
                    "strategy": "range_accel", "symbol": symbol, "instrument_type": "forex",
                    "regime_gate": "LOG_ONLY",
                    "trigger": {"range_pct_min": 0.0008, "range_accel_min": 0.0,
                               "session_start_utc": 0, "session_end_utc": 23},
                    "risk": {"stop_pips": stop, "target_pips": target,
                            "timeout_minutes": timeout, "min_signal_gap_minutes": 30},
                    "hour_filter": {"enabled": True, "hours": params["best_hours"]},
                }
                try:
                    r = run_backtest(cfg, bars, slippage_pips=1.0, commission_per_lot_usd=2.0, friday_close=True)
                    m = r["metrics"]
                    if m["count"] >= 10:
                        te = m["expectancy"] * m["count"]
                        row = {"symbol": symbol, "stop": stop, "target": target, "timeout": timeout,
                               "count": m["count"], "wr": m.get("win_rate",0), "pf": m.get("profit_factor",0),
                               "exp": m.get("expectancy",0), "net": m.get("net_pnl",0), "te": te}
                        sweep_results.append(row)
                        if best is None or te > best["te"]:
                            best = row
                except Exception:
                    pass

    if best:
        print(f"    BEST: stop={best['stop']} tgt={best['target']} to={best['timeout']} "
              f"n={best['count']} PF={best['pf']:.2f} exp={best['exp']:+.2f} net={best['net']:+.1f}", flush=True)

# ── PHASE 3: Portfolio simulation ──
print(f"\n{'='*70}")
print("  PHASE 3: Portfolio simulation (best config per pair)")
print("=" * 70)

# Get best config per pair from sweep
best_per_pair = {}
for r in sweep_results:
    sym = r["symbol"]
    if sym not in best_per_pair or r["te"] > best_per_pair[sym]["te"]:
        best_per_pair[sym] = r

total_trades = 0
total_pnl = 0
total_wins = 0
print(f"\n{'Pair':8s} {'Stop':>4s} {'Tgt':>4s} {'TO':>4s} {'#':>4s} {'WR':>6s} {'PF':>7s} {'Exp':>7s} {'Net':>8s}", flush=True)
print("-" * 60, flush=True)
for sym, r in sorted(best_per_pair.items()):
    print(f"{sym:8s} {r['stop']:4d} {r['target']:4d} {r['timeout']:4d} {r['count']:4d} "
          f"{r['wr']:6.1%} {r['pf']:7.2f} {r['exp']:+7.2f} {r['net']:+8.1f}", flush=True)
    total_trades += r["count"]
    total_pnl += r["net"]
    total_wins += int(r["count"] * r["wr"])

if total_trades > 0:
    portfolio_wr = total_wins / total_trades
    print(f"\n  PORTFOLIO: {total_trades} trades, WR={portfolio_wr:.1%}, Net={total_pnl:+.1f} pips")
    pips_per_trade = total_pnl / total_trades
    print(f"  Avg pip/trade: {pips_per_trade:+.2f}")
    # Rough annual projection (assuming ~5 trades/day across portfolio)
    daily_pips = pips_per_trade * 5
    annual_pips = daily_pips * 252
    print(f"  Projected: {daily_pips:+.1f} pips/day, {annual_pips:+.0f} pips/year")
    # At 1% risk per trade, $10K account, 15-pip stop average
    annual_pct = (pips_per_trade / 15) * 0.01 * total_trades / (len(best_per_pair) * 30) * 252 * 100  # rough
    print(f"  Rough annual return estimate: {annual_pct:+.0f}% (very approximate)")

# Save
output = RESULTS_DIR / "optimal_strategy_results.json"
with open(output, "w") as f:
    json.dump({"phase1": all_results, "phase2_sweep": sweep_results,
               "phase3_portfolio": dict(best_per_pair)}, f, indent=2, default=str)
print(f"\n  Results saved to {output}")
