"""Helio Parameter Sweep — find optimal hold period, stops, and entry filters for GLD/DIA."""
import itertools
from poc_backtest import run_swing_backtest

SYMBOLS = ["GLD", "DIA", "SPY"]

# Parameter grid
PARAMS = {
    "max_hold_days": [3, 5, 7, 10, 15],
    "atr_stop_mult": [1.0, 1.5, 2.0, 2.5],
    "atr_trail_mult": [0.75, 1.0, 1.5],
    "ema_period": [10, 20, 50],
}


def sweep():
    keys = list(PARAMS.keys())
    vals = list(PARAMS.values())
    combos = list(itertools.product(*vals))

    print(f"Running {len(combos)} parameter combos x {len(SYMBOLS)} symbols = {len(combos) * len(SYMBOLS)} backtests")
    print()

    best = {}
    for sym in SYMBOLS:
        results = []
        for combo in combos:
            params = dict(zip(keys, combo))
            try:
                trades = run_swing_backtest(sym, **params)
            except Exception:
                continue

            if len(trades) < 10:
                continue

            pnls = [t["pnl_pct"] for t in trades]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            gw = sum(wins)
            gl = abs(sum(losses))
            pf = gw / gl if gl > 0 else 0
            wr = len(wins) / len(pnls)
            total = sum(pnls)
            avg = total / len(pnls)

            # Max drawdown
            equity = [0]
            for p in pnls:
                equity.append(equity[-1] + p)
            peak = 0
            max_dd = 0
            for e in equity:
                peak = max(peak, e)
                max_dd = max(max_dd, peak - e)

            # Timeout rate
            to_rate = sum(1 for t in trades if t["exit_reason"] == "timeout") / len(trades)

            results.append({
                **params,
                "trades": len(trades),
                "pf": round(pf, 3),
                "wr": round(wr, 3),
                "total": round(total, 2),
                "avg": round(avg, 3),
                "max_dd": round(max_dd, 2),
                "timeout_rate": round(to_rate, 2),
            })

        # Sort by PF, then by total return
        results.sort(key=lambda x: (-x["pf"], -x["total"]))

        print(f"{'=' * 90}")
        print(f"  {sym} — Top 10 Configurations")
        print(f"{'=' * 90}")
        print(f"{'Hold':>5s} {'Stop':>5s} {'Trail':>6s} {'EMA':>4s} | {'T':>4s} {'WR':>5s} {'PF':>6s} {'Avg%':>7s} {'Tot%':>8s} {'DD%':>6s} {'TO%':>5s}")
        print("-" * 90)

        for r in results[:10]:
            print(
                f"{r['max_hold_days']:>5d} {r['atr_stop_mult']:>5.1f} {r['atr_trail_mult']:>6.2f} {r['ema_period']:>4d} | "
                f"{r['trades']:>4d} {r['wr']:>4.0%} {r['pf']:>5.2f} {r['avg']:>+6.3f}% {r['total']:>+7.1f}% "
                f"{r['max_dd']:>5.1f}% {r['timeout_rate']:>4.0%}"
            )

        if results:
            best[sym] = results[0]
        print()

    print("=" * 90)
    print("  OPTIMAL CONFIGS")
    print("=" * 90)
    for sym, b in best.items():
        print(f"  {sym}: hold={b['max_hold_days']}d stop={b['atr_stop_mult']}x trail={b['atr_trail_mult']}x ema={b['ema_period']}")
        print(f"         PF={b['pf']} WR={b['wr']:.0%} Total={b['total']:+.1f}% Trades={b['trades']} DD={b['max_dd']:.1f}%")


if __name__ == "__main__":
    sweep()
