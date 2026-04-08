"""Exit parameter sweep — fixed stdout buffering."""
import json, sys, itertools
from pathlib import Path
import pandas as pd

sys.stdout.reconfigure(line_buffering=True)  # Force line buffering
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from argus_flow.backtest.engine import run_backtest

cfg = json.loads(Path("argus_flow/configs/eurusd_t4_paper_v1.json").read_text())
cfg["regime_gate"] = "LOG_ONLY"
bars = pd.read_csv("argus_flow/data/ibkr_eurusd_1m_extended.csv")
print(f"EXIT SWEEP: EURUSD extended ({len(bars)} bars)")

results = []
stops = [10, 15, 20, 25, 30]
targets = [20, 30, 40, 60, 80, 100]
timeouts = [30, 45, 60, 90, 120, 180]
done = 0
for stop, target, timeout in itertools.product(stops, targets, timeouts):
    if target <= stop:
        continue
    done += 1
    override = {"risk.stop_pips": stop, "risk.target_pips": target, "risk.timeout_minutes": timeout}
    try:
        r = run_backtest(cfg, bars, override_params=override,
                        slippage_pips=1.0, commission_per_lot_usd=2.0, friday_close=True)
        m = r.get("metrics", {})
        if m.get("count", 0) >= 10:
            total_edge = m["expectancy"] * m["count"]
            results.append({
                "stop": stop, "target": target, "timeout": timeout,
                "count": m["count"], "wr": m.get("win_rate", 0),
                "pf": m.get("profit_factor", 0), "exp": m.get("expectancy", 0),
                "net": m.get("net_pnl", 0), "sharpe": m.get("sharpe", 0),
                "total_edge": total_edge, "exits": m.get("exits", {}),
            })
    except Exception:
        pass
    if done % 20 == 0:
        print(f"  ... {done} combos tested", flush=True)

results.sort(key=lambda x: x.get("total_edge", 0), reverse=True)
profitable = [r for r in results if r["pf"] > 1.0]

print(f"\nPROFITABLE CONFIGS (PF > 1.0, n >= 10): {len(profitable)}/{len(results)}")
print(f"{'Stop':>5s} {'Tgt':>5s} {'TO':>5s} | {'#':>4s} {'WR':>6s} {'PF':>7s} {'Exp':>7s} {'Net':>8s} {'Sharpe':>7s} {'TotEdge':>8s}")
print("-" * 75)
for r in (profitable or results)[:25]:
    print(f"{r['stop']:5d} {r['target']:5d} {r['timeout']:5d} | "
          f"{r['count']:4d} {r['wr']:6.1%} {r['pf']:7.2f} {r['exp']:+7.2f} {r['net']:+8.1f} "
          f"{r['sharpe']:+7.2f} {r.get('total_edge',0):+8.1f}", flush=True)

if not profitable:
    print("\n** NO PROFITABLE CONFIGS FOUND AFTER COSTS **")
