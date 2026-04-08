"""Test alternative strategies: FVG, Liquidity Sweep, Volume Profile on 3 pairs."""
import json, sys, itertools
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from argus_flow.backtest.engine import run_backtest

DATA = {
    "EURUSD": "argus_flow/data/ibkr_eurusd_1m.csv",
    "GBPUSD": "argus_flow/data/ibkr_gbpusd_1m.csv",
    "EURJPY": "argus_flow/data/ibkr_eurjpy_1m.csv",
}

STRATEGIES = {
    "fvg": {
        "strategy": "fvg", "instrument_type": "forex", "regime_gate": "LOG_ONLY",
        "trigger": {"min_displacement_mult": 1.5, "max_wait_bars": 60, "session_start_utc": 7, "session_end_utc": 17},
        "risk": {"stop_pips": 15, "target_pips": 45, "timeout_minutes": 120, "min_signal_gap_minutes": 30},
    },
    "liquidity_sweep": {
        "strategy": "liquidity_sweep", "instrument_type": "forex", "regime_gate": "LOG_ONLY",
        "trigger": {"swing_lookback": 20, "wick_ratio_min": 0.3, "session_start_utc": 7, "session_end_utc": 17},
        "risk": {"stop_pips": 10, "target_pips": 30, "timeout_minutes": 90, "min_signal_gap_minutes": 30},
    },
    "volume_profile": {
        "strategy": "volume_profile", "instrument_type": "forex", "regime_gate": "LOG_ONLY",
        "trigger": {"vp_lookback": 240, "entry_buffer_pct": 0.0002, "session_start_utc": 13, "session_end_utc": 20},
        "risk": {"stop_pips": 15, "target_pips": 30, "timeout_minutes": 60, "min_signal_gap_minutes": 15},
    },
}

results = []

# Phase 1: Base configs on all pairs
print("=" * 70)
print("  PHASE 1: Base strategy configs on all pairs (honest costs)")
print("=" * 70)

for strat_name, base_cfg in STRATEGIES.items():
    for symbol, data_path in DATA.items():
        cfg = json.loads(json.dumps(base_cfg))
        cfg["symbol"] = symbol
        bars = pd.read_csv(data_path)
        try:
            r = run_backtest(cfg, bars, slippage_pips=1.0, commission_per_lot_usd=2.0, friday_close=True)
            m = r.get("metrics", {})
            row = {
                "strategy": strat_name, "symbol": symbol, "params": "",
                "count": m.get("count", 0), "wr": m.get("win_rate", 0),
                "pf": m.get("profit_factor", 0), "exp": m.get("expectancy", 0),
                "net": m.get("net_pnl", 0), "sharpe": m.get("sharpe", 0),
                "exits": m.get("exits", {}),
            }
            results.append(row)
            print(f"  {strat_name:18s} {symbol:8s} | trades={row['count']:3d} WR={row['wr']:.1%} "
                  f"PF={row['pf']:6.2f} exp={row['exp']:+.2f} net={row['net']:+.1f} "
                  f"sharpe={row['sharpe']:+.2f} exits={row['exits']}")
        except Exception as e:
            print(f"  {strat_name:18s} {symbol:8s} | ERROR: {e}")

# Phase 2: Parameter sweep on FVG (most asymmetric)
print(f"\n{'=' * 70}")
print("  PHASE 2: FVG parameter sweep (EURUSD, honest costs)")
print("=" * 70)

fvg_sweep = []
bars_eu = pd.read_csv(DATA["EURUSD"])
for stop, target, disp in itertools.product([10, 15, 20], [30, 45, 60], [1.0, 1.5, 2.0]):
    cfg = json.loads(json.dumps(STRATEGIES["fvg"]))
    cfg["symbol"] = "EURUSD"
    cfg["risk"]["stop_pips"] = stop
    cfg["risk"]["target_pips"] = target
    cfg["trigger"]["min_displacement_mult"] = disp
    try:
        r = run_backtest(cfg, bars_eu, slippage_pips=1.0, commission_per_lot_usd=2.0, friday_close=True)
        m = r.get("metrics", {})
        if m.get("count", 0) >= 5:
            fvg_sweep.append({
                "params": f"stop={stop} tgt={target} disp={disp}",
                "count": m["count"], "wr": m.get("win_rate", 0),
                "pf": m.get("profit_factor", 0), "exp": m.get("expectancy", 0),
                "net": m.get("net_pnl", 0), "sharpe": m.get("sharpe", 0),
            })
    except Exception:
        pass

fvg_sweep.sort(key=lambda x: x["pf"], reverse=True)
for r in fvg_sweep[:15]:
    print(f"  FVG {r['params']:30s} | n={r['count']:3d} WR={r['wr']:.1%} PF={r['pf']:6.2f} "
          f"exp={r['exp']:+.2f} net={r['net']:+.1f} sharpe={r['sharpe']:+.2f}")

# Phase 3: Liquidity sweep parameter sweep
print(f"\n{'=' * 70}")
print("  PHASE 3: Liquidity Sweep parameter sweep (EURUSD, honest costs)")
print("=" * 70)

sweep_results = []
for stop, target, wick in itertools.product([8, 10, 15], [24, 30, 45], [0.2, 0.3, 0.5]):
    cfg = json.loads(json.dumps(STRATEGIES["liquidity_sweep"]))
    cfg["symbol"] = "EURUSD"
    cfg["risk"]["stop_pips"] = stop
    cfg["risk"]["target_pips"] = target
    cfg["trigger"]["wick_ratio_min"] = wick
    try:
        r = run_backtest(cfg, bars_eu, slippage_pips=1.0, commission_per_lot_usd=2.0, friday_close=True)
        m = r.get("metrics", {})
        if m.get("count", 0) >= 5:
            sweep_results.append({
                "params": f"stop={stop} tgt={target} wick={wick}",
                "count": m["count"], "wr": m.get("win_rate", 0),
                "pf": m.get("profit_factor", 0), "exp": m.get("expectancy", 0),
                "net": m.get("net_pnl", 0), "sharpe": m.get("sharpe", 0),
            })
    except Exception:
        pass

sweep_results.sort(key=lambda x: x["pf"], reverse=True)
for r in sweep_results[:15]:
    print(f"  SWEEP {r['params']:30s} | n={r['count']:3d} WR={r['wr']:.1%} PF={r['pf']:6.2f} "
          f"exp={r['exp']:+.2f} net={r['net']:+.1f} sharpe={r['sharpe']:+.2f}")

# Final summary
print(f"\n{'=' * 70}")
print("  ALL RESULTS SORTED BY PROFIT FACTOR")
print("=" * 70)
all_results = results + [dict(strategy="fvg_sweep", symbol="EURUSD", **r) for r in fvg_sweep] + \
              [dict(strategy="sweep_sweep", symbol="EURUSD", **r) for r in sweep_results]
all_results.sort(key=lambda x: x.get("pf", 0), reverse=True)
print(f"{'Strategy':18s} {'Symbol':8s} {'Params':30s} {'#':>4s} {'WR':>6s} {'PF':>7s} {'Exp':>7s} {'Net':>8s}")
print("-" * 95)
for r in all_results[:25]:
    print(f"{r.get('strategy',''):18s} {r.get('symbol',''):8s} {r.get('params',''):30s} "
          f"{r.get('count',0):4d} {r.get('wr',0):6.1%} {r.get('pf',0):7.2f} "
          f"{r.get('exp',0):+7.2f} {r.get('net',0):+8.1f}")
