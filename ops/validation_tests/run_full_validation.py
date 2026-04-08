"""FULL VALIDATION SUITE — All pairs, all strategies, all angles.
Saves results to ops/validation_tests/results/ as JSON for analysis.
"""
import json, sys, math, random, itertools, time
from pathlib import Path
from datetime import datetime, timezone
import numpy as np
import pandas as pd
from scipy import stats as sp_stats

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from argus_flow.backtest.engine import run_backtest

RESULTS_DIR = Path("ops/validation_tests/results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# All available data
ALL_DATA = {}
data_dir = Path("argus_flow/data")
for f in sorted(data_dir.glob("ibkr_*_1m.csv")):
    sym = f.stem.replace("ibkr_", "").replace("_1m", "").upper()
    ALL_DATA[sym] = str(f)
# Add extended
ext = data_dir / "ibkr_eurusd_1m_extended.csv"
if ext.exists():
    ALL_DATA["EURUSD_EXT"] = str(ext)

print(f"Available data: {len(ALL_DATA)} datasets")
for sym, path in ALL_DATA.items():
    bars = pd.read_csv(path)
    print(f"  {sym:15s}: {len(bars):6d} bars")

# Strategy configs
def make_config(strategy, symbol, stop_pips=20, target_pips=40, timeout_min=60,
                session_start=7, session_end=17, min_gap=15, regime_gate="LOG_ONLY", **extra_trigger):
    is_jpy = "JPY" in symbol.upper()
    cfg = {
        "strategy": strategy, "symbol": symbol, "instrument_type": "forex",
        "regime_gate": regime_gate,
        "trigger": {"range_pct_min": 0.001, "range_accel_min": 0.0,
                     "session_start_utc": session_start, "session_end_utc": session_end,
                     **extra_trigger},
        "risk": {"stop_pips": stop_pips, "target_pips": target_pips,
                 "timeout_minutes": timeout_min, "min_signal_gap_minutes": min_gap},
    }
    return cfg

STRATEGIES = {
    "range_accel": {"stop_pips": 20, "target_pips": 40, "timeout_min": 60},
    "fvg": {"stop_pips": 15, "target_pips": 45, "timeout_min": 120, "min_displacement_mult": 1.5, "max_wait_bars": 60},
    "liquidity_sweep": {"stop_pips": 10, "target_pips": 30, "timeout_min": 90, "swing_lookback": 20, "wick_ratio_min": 0.3},
    "volume_profile": {"stop_pips": 15, "target_pips": 30, "timeout_min": 60, "vp_lookback": 240, "entry_buffer_pct": 0.0002},
}

all_results = []

def run_and_record(label, cfg, bars_df, extra_info=None):
    """Run backtest, compute all stats, record result."""
    start = time.time()
    try:
        r = run_backtest(cfg, bars_df, slippage_pips=1.0, commission_per_lot_usd=2.0, friday_close=True)
    except Exception as e:
        print(f"  ERROR: {label} -- {e}", flush=True)
        return None
    elapsed = time.time() - start
    m = r.get("metrics", {})
    trades = r.get("trades", [])
    pnls = [t["pnl"] for t in trades]
    n = len(pnls)

    result = {
        "label": label, "symbol": cfg.get("symbol", "?"), "strategy": cfg.get("strategy", "?"),
        "count": n, "win_rate": m.get("win_rate", 0), "profit_factor": m.get("profit_factor", 0),
        "expectancy": m.get("expectancy", 0), "net_pnl": m.get("net_pnl", 0),
        "sharpe": m.get("sharpe", 0), "sortino": m.get("sortino", 0),
        "max_drawdown": m.get("max_drawdown", 0), "exits": m.get("exits", {}),
        "elapsed_s": round(elapsed, 1),
    }

    # Statistical tests (if enough trades)
    if n >= 10:
        t_stat, p_val = sp_stats.ttest_1samp(pnls, 0)
        result["ttest_p"] = round(p_val, 4)

        # Bootstrap CI
        boots = [float(np.mean(np.random.choice(pnls, n, replace=True))) for _ in range(2000)]
        result["ci_low"] = round(np.percentile(boots, 2.5), 2)
        result["ci_high"] = round(np.percentile(boots, 97.5), 2)
        result["ci_excludes_zero"] = not (result["ci_low"] <= 0 <= result["ci_high"])

        # Monte Carlo
        actual_net = sum(pnls)
        mc_better = sum(1 for _ in range(2000)
                       if sum(p * random.choice([-1,1]) for p in pnls) >= actual_net)
        result["mc_p"] = round(mc_better / 2000, 4)

        # Min sample
        std = np.std(pnls, ddof=1)
        mean = np.mean(pnls)
        if mean != 0 and std > 0:
            result["min_sample_95"] = int(math.ceil((1.96 * std / abs(mean)) ** 2))

        # Consecutive losses
        max_loss_streak = 0
        streak = 0
        for p in pnls:
            if p <= 0:
                streak += 1
                max_loss_streak = max(max_loss_streak, streak)
            else:
                streak = 0
        result["max_loss_streak"] = max_loss_streak

        # Hour distribution
        hours = {}
        for t in trades:
            try:
                h = pd.Timestamp(t["entry_time"]).hour
                hours[h] = hours.get(h, 0) + 1
            except: pass
        result["hour_distribution"] = hours

        # Day distribution
        days = {}
        for t in trades:
            try:
                d = pd.Timestamp(t["entry_time"]).strftime("%A")
                days[d] = days.get(d, 0) + 1
            except: pass
        result["day_distribution"] = days

        # Duration buckets
        dur_pnl = {"<30m": [], "30-60m": [], "60-120m": [], ">120m": []}
        for t in trades:
            d = t.get("duration_min", 60)
            if d < 30: dur_pnl["<30m"].append(t["pnl"])
            elif d < 60: dur_pnl["30-60m"].append(t["pnl"])
            elif d < 120: dur_pnl["60-120m"].append(t["pnl"])
            else: dur_pnl[">120m"].append(t["pnl"])
        result["duration_buckets"] = {k: {"n": len(v), "avg": round(np.mean(v), 2) if v else 0}
                                       for k, v in dur_pnl.items()}

    if extra_info:
        result.update(extra_info)
    all_results.append(result)
    return result

# ══════════════════════════════════════════════════════════════
# PHASE 1: Every strategy x every FX pair (base configs)
# ══════════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print("  PHASE 1: Strategy x Pair matrix (honest costs)")
print(f"{'='*70}\n")

fx_pairs = [s for s in ALL_DATA if not s.endswith("_EXT") and s not in ("M2K","MCL","MES_SP500_MICRO","MGC","MNQ_NASDAQ_MICRO","MYM_DOW_MICRO","NQ_NASDAQ_EMINI")]
# Filter to only FX pairs (6-char symbols)
fx_pairs = [s for s in fx_pairs if len(s) == 6 and not any(c.isdigit() for c in s)]

print(f"FX pairs: {fx_pairs}")
print(f"Strategies: {list(STRATEGIES.keys())}")
print(f"Total combos: {len(fx_pairs) * len(STRATEGIES)}\n")

print(f"{'Strategy':18s} {'Symbol':8s} {'#':>4s} {'WR':>6s} {'PF':>7s} {'Exp':>7s} {'Net':>8s} {'p-val':>6s} {'CI':>14s}", flush=True)
print("-" * 85, flush=True)

for strat_name, strat_params in STRATEGIES.items():
    for symbol in fx_pairs:
        data_path = ALL_DATA.get(symbol)
        if not data_path:
            continue
        bars = pd.read_csv(data_path)
        if len(bars) < 500:
            continue

        trigger_extra = {k: v for k, v in strat_params.items()
                        if k not in ("stop_pips", "target_pips", "timeout_min", "min_gap")}
        cfg = make_config(strat_name, symbol,
                         stop_pips=strat_params.get("stop_pips", 20),
                         target_pips=strat_params.get("target_pips", 40),
                         timeout_min=strat_params.get("timeout_min", 60),
                         **trigger_extra)
        label = f"{strat_name}_{symbol}"
        res = run_and_record(label, cfg, bars)
        if res:
            ci = f"[{res.get('ci_low','?')},{res.get('ci_high','?')}]" if 'ci_low' in res else "n/a"
            print(f"{strat_name:18s} {symbol:8s} {res['count']:4d} {res['win_rate']:6.1%} "
                  f"{res['profit_factor']:7.2f} {res['expectancy']:+7.2f} {res['net_pnl']:+8.1f} "
                  f"{res.get('ttest_p','n/a'):>6} {ci:>14s}", flush=True)

# ══════════════════════════════════════════════════════════════
# PHASE 2: Parameter sensitivity on profitable configs
# ══════════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print("  PHASE 2: Parameter sensitivity on top configs")
print(f"{'='*70}\n")

profitable = [r for r in all_results if r["profit_factor"] > 1.0 and r["count"] >= 10]
profitable.sort(key=lambda x: x["expectancy"] * x["count"], reverse=True)

print(f"Profitable configs (PF>1.0, n>=10): {len(profitable)}", flush=True)
for r in profitable[:10]:
    print(f"  {r['label']:30s} n={r['count']:3d} PF={r['profit_factor']:.2f} exp={r['expectancy']:+.2f} "
          f"p={r.get('ttest_p','?')} CI_0={'N' if r.get('ci_excludes_zero') else 'Y'}", flush=True)

# For each profitable config, test parameter sensitivity (±30%)
for r in profitable[:5]:
    sym = r["symbol"]
    strat = r["strategy"]
    data_path = ALL_DATA.get(sym)
    if not data_path:
        continue
    bars = pd.read_csv(data_path)
    base_cfg = make_config(strat, sym, **{k: v for k, v in STRATEGIES.get(strat, {}).items()
                                          if k not in ("min_gap",)})
    base_stop = base_cfg["risk"]["stop_pips"]
    base_target = base_cfg["risk"]["target_pips"]
    print(f"\n  Sensitivity: {strat}_{sym} (base stop={base_stop}, target={base_target})", flush=True)
    for mult in [0.5, 0.7, 0.85, 1.0, 1.15, 1.3, 1.5]:
        override = {"risk.stop_pips": max(5, int(base_stop * mult)),
                    "risk.target_pips": max(10, int(base_target * mult))}
        res = run_and_record(f"sens_{strat}_{sym}_{mult:.0%}", base_cfg, bars,
                            extra_info={"test": "sensitivity", "mult": mult})
        if res:
            print(f"    {mult:5.0%}: n={res['count']:3d} PF={res['profit_factor']:5.2f} exp={res['expectancy']:+.2f}", flush=True)

# ══════════════════════════════════════════════════════════════
# PHASE 3: Walk-forward on profitable configs
# ══════════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print("  PHASE 3: Walk-forward validation on top configs")
print(f"{'='*70}\n")

for r in profitable[:5]:
    sym = r["symbol"]
    strat = r["strategy"]
    data_path = ALL_DATA.get(sym)
    if not data_path:
        continue
    bars = pd.read_csv(data_path)
    n_bars = len(bars)
    fold_size = n_bars // 5
    cfg = make_config(strat, sym, **{k: v for k, v in STRATEGIES.get(strat, {}).items()
                                      if k not in ("min_gap",)})
    print(f"  Walk-forward: {strat}_{sym} ({n_bars} bars, {fold_size}/fold)", flush=True)
    wf_pfs = []
    for fold in range(4):
        test_start = fold_size * (fold + 1)
        test_end = min(test_start + fold_size, n_bars)
        test_bars = bars.iloc[test_start:test_end].reset_index(drop=True)
        if len(test_bars) < 300:
            continue
        res = run_and_record(f"wf_{strat}_{sym}_f{fold+1}", cfg, test_bars,
                            extra_info={"test": "walkforward", "fold": fold+1})
        if res:
            wf_pfs.append(res["profit_factor"])
            print(f"    Fold {fold+1}: n={res['count']:3d} PF={res['profit_factor']:5.2f} exp={res['expectancy']:+.2f}", flush=True)
    if wf_pfs:
        print(f"    Avg OOS PF: {np.mean(wf_pfs):.2f}, Profitable folds: {sum(1 for p in wf_pfs if p > 1.0)}/{len(wf_pfs)}", flush=True)

# ══════════════════════════════════════════════════════════════
# PHASE 4: Slippage sensitivity on top configs
# ══════════════════════════════════════════════════════════════
print(f"\n{'='*70}")
print("  PHASE 4: Cost sensitivity")
print(f"{'='*70}\n")

for r in profitable[:3]:
    sym = r["symbol"]
    strat = r["strategy"]
    data_path = ALL_DATA.get(sym)
    if not data_path:
        continue
    bars = pd.read_csv(data_path)
    cfg = make_config(strat, sym, **{k: v for k, v in STRATEGIES.get(strat, {}).items()
                                      if k not in ("min_gap",)})
    print(f"  Cost sensitivity: {strat}_{sym}", flush=True)
    for slip in [0, 0.5, 1.0, 1.5, 2.0, 3.0]:
        try:
            r_s = run_backtest(cfg, bars, slippage_pips=slip, commission_per_lot_usd=2.0, friday_close=True)
            m_s = r_s["metrics"]
            print(f"    slip={slip:.1f}: n={m_s['count']:3d} PF={m_s.get('profit_factor',0):5.2f} "
                  f"exp={m_s.get('expectancy',0):+.2f} net={m_s.get('net_pnl',0):+.1f}", flush=True)
        except: pass

# ══════════════════════════════════════════════════════════════
# SAVE ALL RESULTS
# ══════════════════════════════════════════════════════════════
output_file = RESULTS_DIR / f"full_validation_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.json"
with open(output_file, "w") as f:
    json.dump(all_results, f, indent=2, default=str)
print(f"\n{'='*70}")
print(f"  RESULTS SAVED: {output_file}")
print(f"  Total tests run: {len(all_results)}")
profitable_final = [r for r in all_results if r.get("profit_factor", 0) > 1.0
                    and r.get("count", 0) >= 10 and "test" not in r]
print(f"  Profitable strategy+pair combos: {len(profitable_final)}")
print(f"{'='*70}")

# Final ranking
print(f"\n  TOP 10 CONFIGS (by total edge = exp * count):")
ranked = sorted([r for r in all_results if r.get("count",0) >= 10 and "test" not in r],
                key=lambda x: x["expectancy"] * x["count"], reverse=True)
for i, r in enumerate(ranked[:10], 1):
    sig = "SIG" if r.get("ttest_p", 1) < 0.1 else "---"
    print(f"  {i:2d}. {r['label']:30s} n={r['count']:3d} PF={r['profit_factor']:5.2f} "
          f"exp={r['expectancy']:+.2f} net={r['net_pnl']:+.1f} p={r.get('ttest_p','?'):>5} [{sig}]", flush=True)
