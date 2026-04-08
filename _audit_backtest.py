"""Honest backtest: current configs with costs ON vs OFF, regime GATE vs LOG_ONLY."""
import json, sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from argus_flow.backtest.engine import run_backtest

PAIRS = [
    ("EURUSD", "argus_flow/configs/eurusd_t4_paper_v1.json", "argus_flow/data/ibkr_eurusd_1m.csv"),
    ("GBPUSD", "argus_flow/configs/gbpusd_range_paper_v1.json", "argus_flow/data/ibkr_gbpusd_1m.csv"),
    ("EURJPY", "argus_flow/configs/eurjpy_t4_paper_v1.json", "argus_flow/data/ibkr_eurjpy_1m.csv"),
]

results = []
for symbol, cfg_path, data_path in PAIRS:
    cfg = json.loads(Path(cfg_path).read_text())
    bars = pd.read_csv(data_path)
    print(f"\n{'='*60}")
    print(f"  {symbol}: {len(bars)} bars")
    print(f"{'='*60}")

    for regime in ["LOG_ONLY", "GATE"]:
        for costs_on in [False, True]:
            cfg_copy = json.loads(json.dumps(cfg))
            cfg_copy["regime_gate"] = regime
            slip = 1.0 if costs_on else 0.0
            comm = 2.0 if costs_on else 0.0
            label = f"{regime:8s} | costs={'ON' if costs_on else 'OFF':3s}"

            try:
                r = run_backtest(cfg_copy, bars, slippage_pips=slip, commission_per_lot_usd=comm, friday_close=True)
                m = r.get("metrics", {})
                results.append({
                    "symbol": symbol, "regime": regime, "costs": "ON" if costs_on else "OFF",
                    "count": m.get("count", 0), "wr": m.get("win_rate", 0),
                    "pf": m.get("profit_factor", 0), "exp": m.get("expectancy", 0),
                    "net": m.get("net_pnl", 0), "sharpe": m.get("sharpe", 0),
                    "sortino": m.get("sortino", 0), "max_dd": m.get("max_drawdown", 0),
                    "exits": m.get("exits", {}),
                })
                print(f"  {label} | trades={m.get('count',0):3d} WR={m.get('win_rate',0):.1%} "
                      f"PF={m.get('profit_factor',0):6.2f} exp={m.get('expectancy',0):+.2f} "
                      f"net={m.get('net_pnl',0):+.1f} sharpe={m.get('sharpe',0):+.2f} "
                      f"sortino={m.get('sortino',0):+.2f} dd={m.get('max_drawdown',0):.1f}")
                exits = m.get("exits", {})
                if exits:
                    print(f"           exits: {exits}")
            except Exception as e:
                print(f"  {label} | ERROR: {e}")

print(f"\n{'='*60}")
print("  SUMMARY TABLE")
print(f"{'='*60}")
print(f"{'Symbol':8s} {'Regime':8s} {'Costs':5s} {'#':>4s} {'WR':>6s} {'PF':>7s} {'Exp':>7s} {'Net':>8s} {'Sharpe':>7s} {'DD':>6s}")
print("-" * 72)
for r in results:
    print(f"{r['symbol']:8s} {r['regime']:8s} {r['costs']:5s} {r['count']:4d} {r['wr']:6.1%} {r['pf']:7.2f} {r['exp']:+7.2f} {r['net']:+8.1f} {r['sharpe']:+7.2f} {r['max_dd']:6.1f}")
