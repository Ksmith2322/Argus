"""Exit parameter optimization: find stop/target/timeout combos that maximize edge after costs."""
import json, sys, itertools
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from argus_flow.backtest.engine import run_backtest

TESTS = [
    ("EURUSD", "argus_flow/configs/eurusd_t4_paper_v1.json", "argus_flow/data/ibkr_eurusd_1m_extended.csv"),
    ("GBPUSD", "argus_flow/configs/gbpusd_range_paper_v1.json", "argus_flow/data/ibkr_gbpusd_1m.csv"),
]

for symbol, cfg_path, data_path in TESTS:
    cfg = json.loads(Path(cfg_path).read_text())
    bars = pd.read_csv(data_path)
    print(f"\n{'='*70}")
    print(f"  EXIT SWEEP: {symbol} ({len(bars)} bars)")
    print(f"{'='*70}")

    results = []
    # Phase 1: Fixed pip sweep (stop x target x timeout)
    stops = [10, 15, 20, 25, 30]
    targets = [20, 30, 40, 60, 80, 100]
    timeouts = [30, 45, 60, 90, 120, 180]

    total = len(stops) * len(targets) * len(timeouts)
    done = 0
    for stop, target, timeout in itertools.product(stops, targets, timeouts):
        done += 1
        if target <= stop:  # skip nonsensical R:R
            continue
        override = {
            "risk.stop_pips": stop,
            "risk.target_pips": target,
            "risk.timeout_minutes": timeout,
        }
        try:
            r = run_backtest(cfg, bars, override_params=override,
                           slippage_pips=1.0, commission_per_lot_usd=2.0, friday_close=True)
            m = r.get("metrics", {})
            if m.get("count", 0) >= 10 and m.get("profit_factor", 0) > 0:
                total_edge = m["expectancy"] * m["count"]
                results.append({
                    "stop": stop, "target": target, "timeout": timeout,
                    "atr": 0,
                    "count": m["count"], "wr": m.get("win_rate", 0),
                    "pf": m.get("profit_factor", 0), "exp": m.get("expectancy", 0),
                    "net": m.get("net_pnl", 0), "sharpe": m.get("sharpe", 0),
                    "sortino": m.get("sortino", 0), "total_edge": total_edge,
                    "exits": m.get("exits", {}),
                })
        except Exception:
            pass
        if done % 30 == 0:
            print(f"  ... {done}/{total} combos tested")

    # Phase 2: ATR-based stops on top 5 fixed-pip configs
    results.sort(key=lambda x: x["total_edge"], reverse=True)
    top5 = results[:5]

    print(f"\n  Top 5 fixed-pip configs (testing ATR variants):")
    for r in top5:
        print(f"    stop={r['stop']} tgt={r['target']} to={r['timeout']} | "
              f"n={r['count']} WR={r['wr']:.1%} PF={r['pf']:.2f} exp={r['exp']:+.2f} net={r['net']:+.1f}")

    for base in top5:
        for atr_s, atr_t in [(1.5, 3.0), (2.0, 4.0), (2.5, 5.0), (3.0, 6.0)]:
            override = {
                "risk.stop_pips": base["stop"],
                "risk.target_pips": base["target"],
                "risk.timeout_minutes": base["timeout"],
                "risk.atr_stop_mult": atr_s,
                "risk.atr_target_mult": atr_t,
            }
            try:
                r = run_backtest(cfg, bars, override_params=override,
                               slippage_pips=1.0, commission_per_lot_usd=2.0, friday_close=True)
                m = r.get("metrics", {})
                if m.get("count", 0) >= 10:
                    total_edge = m["expectancy"] * m["count"]
                    results.append({
                        "stop": base["stop"], "target": base["target"], "timeout": base["timeout"],
                        "atr": f"{atr_s}/{atr_t}",
                        "count": m["count"], "wr": m.get("win_rate", 0),
                        "pf": m.get("profit_factor", 0), "exp": m.get("expectancy", 0),
                        "net": m.get("net_pnl", 0), "sharpe": m.get("sharpe", 0),
                        "sortino": m.get("sortino", 0), "total_edge": total_edge,
                        "exits": m.get("exits", {}),
                    })
            except Exception:
                pass

    # Print top 20 sorted by total edge
    results.sort(key=lambda x: x["total_edge"], reverse=True)
    profitable = [r for r in results if r["pf"] > 1.0]

    print(f"\n  PROFITABLE CONFIGS (PF > 1.0, n >= 10): {len(profitable)}/{len(results)}")
    print(f"  {'Stop':>5s} {'Tgt':>5s} {'TO':>5s} {'ATR':>7s} | {'#':>4s} {'WR':>6s} {'PF':>7s} {'Exp':>7s} {'Net':>8s} {'Sharpe':>7s} {'TotalEdge':>10s} | Exits")
    print("  " + "-" * 95)
    for r in profitable[:20]:
        print(f"  {r['stop']:5d} {r['target']:5d} {r['timeout']:5d} {str(r['atr']):>7s} | "
              f"{r['count']:4d} {r['wr']:6.1%} {r['pf']:7.2f} {r['exp']:+7.2f} {r['net']:+8.1f} "
              f"{r['sharpe']:+7.2f} {r['total_edge']:+10.1f} | {r['exits']}")

    if not profitable:
        print("  ** NO PROFITABLE CONFIGS FOUND **")
        print(f"\n  Best losing configs:")
        results.sort(key=lambda x: x["pf"], reverse=True)
        for r in results[:10]:
            print(f"  {r['stop']:5d} {r['target']:5d} {r['timeout']:5d} {str(r['atr']):>7s} | "
                  f"{r['count']:4d} {r['wr']:6.1%} {r['pf']:7.2f} {r['exp']:+7.2f} {r['net']:+8.1f}")
