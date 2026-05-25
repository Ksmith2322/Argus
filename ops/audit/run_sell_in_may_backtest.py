"""Disciplined-gate backtest of the Sell-in-May SPY/SHY rotation.

Strategy agent #7 (docs/AUDIT_2026_05_25_PART2/STRATEGY.md). Calendar
rotation: SPY (Nov-Apr) vs SHY (May-Oct). Always invested. The PASS
criteria are benchmark-relative (vs buy-and-hold SPY), not the
standard PF CI floor, because every leg is a 6-month directional bet
rather than a discrete trade.

PASS = strategy total return ≥ SPY-bench AND max DD < SPY-bench AND
       Sharpe ≥ SPY-bench AND H1/H2 both show outperformance.

Usage:
    python -m ops.audit.run_sell_in_may_backtest
    python -m ops.audit.run_sell_in_may_backtest --slippage-bps 5
    python -m ops.audit.run_sell_in_may_backtest --period 30y
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from helio.sell_in_may import (
    DEFAULT_SLIPPAGE_BPS_RT,
    backtest_sell_in_may,
    split_h1_h2,
)
from helio.yfinance_cache import download_cached

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


def _verdict(b, h1, h2) -> str:
    # Benchmark-relative PASS criteria for always-invested calendar strategies
    full_outperforms = (b.total_ret_pct_compounded > b.bench_total_ret_pct
                        and b.max_dd_pct < b.bench_max_dd_pct
                        and b.sharpe_ann > b.bench_sharpe_ann)
    h1_out = (h1.outperformance_pp > 0)
    h2_out = (h2.outperformance_pp > 0)
    if full_outperforms and h1_out and h2_out:
        return "PASS"
    if full_outperforms and (h1_out or h2_out):
        return "MARGINAL_PASS"
    if h1_out != h2_out:
        return "TIME_SPLIT_DIVERGES"
    return "FAIL"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", default="20y")
    parser.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS_RT)
    parser.add_argument("--risk-off", default="SHY",
                        help="Defensive ticker (default SHY = 1-3yr Treasuries)")
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()

    print(f"=== Sell-in-May calendar rotation gate ===")
    print(f"period={args.period}  slippage_rt={args.slippage_bps}bps  "
          f"risk_on=SPY  risk_off={args.risk_off}")
    print()

    b = backtest_sell_in_may(
        period=args.period,
        slippage_bps_rt=args.slippage_bps,
        risk_off_ticker=args.risk_off,
    )
    if b.error:
        print(f"ERROR: {b.error}")
        return 2

    # Re-fetch the data for the H1/H2 split (cheap from cache)
    from helio.sell_in_may import _fetch_close
    import pandas as pd
    spy = _fetch_close("SPY", args.period)
    shy = _fetch_close(args.risk_off, args.period)
    df = pd.concat([spy.rename("spy"), shy.rename("shy")], axis=1).dropna()
    h1, h2 = split_h1_h2(b, df)
    verdict = _verdict(b, h1, h2)

    print(f"n legs (6-month holds) = {b.n_legs}  avg hold = {b.avg_holding_days:.0f} days")
    print()
    print(f"{'':16} {'Strategy':>12} {'SPY bench':>12} {'Delta':>8}")
    print(f"{'Total return':16} {b.total_ret_pct_compounded:>11.1f}% "
          f"{b.bench_total_ret_pct:>11.1f}% "
          f"{b.total_ret_pct_compounded - b.bench_total_ret_pct:>+7.1f}pp")
    print(f"{'CAGR':16} {b.cagr_pct:>11.2f}% {b.bench_cagr_pct:>11.2f}% "
          f"{b.cagr_pct - b.bench_cagr_pct:>+7.2f}pp")
    print(f"{'Max DD':16} {b.max_dd_pct:>11.1f}% {b.bench_max_dd_pct:>11.1f}% "
          f"{b.max_dd_pct - b.bench_max_dd_pct:>+7.1f}pp")
    print(f"{'Sharpe (ann)':16} {b.sharpe_ann:>12.3f} {b.bench_sharpe_ann:>12.3f} "
          f"{b.sharpe_ann - b.bench_sharpe_ann:>+8.3f}")
    print()
    print(f"leg PF = {b.pf:.3f}  bootstrap CI = [{b.pf_ci_lower:.3f}, {b.pf_ci_upper:.3f}]")
    print()
    print(f"H1: n={h1.n:2d}  PF={h1.leg_pf:.2f}  "
          f"strat={h1.total_ret_pct:>+6.1f}%  SPY={h1.bench_total_pct:>+6.1f}%  "
          f"delta={h1.outperformance_pp:>+5.1f}pp  DD={h1.max_dd_pct:.1f}%")
    print(f"H2: n={h2.n:2d}  PF={h2.leg_pf:.2f}  "
          f"strat={h2.total_ret_pct:>+6.1f}%  SPY={h2.bench_total_pct:>+6.1f}%  "
          f"delta={h2.outperformance_pp:>+5.1f}pp  DD={h2.max_dd_pct:.1f}%")
    print()
    print(f"VERDICT: {verdict}")

    summary = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "period": args.period,
        "slippage_bps_rt": args.slippage_bps,
        "risk_off_ticker": args.risk_off,
        "n_legs": int(b.n_legs),
        "strategy": {
            "total_ret_pct": round(b.total_ret_pct_compounded, 2),
            "cagr_pct": round(b.cagr_pct, 3),
            "max_dd_pct": round(b.max_dd_pct, 2),
            "sharpe_ann": round(b.sharpe_ann, 3),
            "pf": round(b.pf, 3),
            "pf_ci_lower": round(b.pf_ci_lower, 3),
            "pf_ci_upper": round(b.pf_ci_upper, 3),
        },
        "benchmark_spy": {
            "total_ret_pct": round(b.bench_total_ret_pct, 2),
            "cagr_pct": round(b.bench_cagr_pct, 3),
            "max_dd_pct": round(b.bench_max_dd_pct, 2),
            "sharpe_ann": round(b.bench_sharpe_ann, 3),
        },
        "delta_pp": {
            "total_ret_pct": round(b.total_ret_pct_compounded - b.bench_total_ret_pct, 2),
            "cagr_pct": round(b.cagr_pct - b.bench_cagr_pct, 3),
            "max_dd_pct": round(b.max_dd_pct - b.bench_max_dd_pct, 2),
            "sharpe_ann": round(b.sharpe_ann - b.bench_sharpe_ann, 3),
        },
        "h1": {"n": h1.n, "leg_pf": round(h1.leg_pf, 3),
               "strat_pct": round(h1.total_ret_pct, 2),
               "spy_pct": round(h1.bench_total_pct, 2),
               "delta_pp": round(h1.outperformance_pp, 2),
               "max_dd_pct": round(h1.max_dd_pct, 2)},
        "h2": {"n": h2.n, "leg_pf": round(h2.leg_pf, 3),
               "strat_pct": round(h2.total_ret_pct, 2),
               "spy_pct": round(h2.bench_total_pct, 2),
               "delta_pp": round(h2.outperformance_pp, 2),
               "max_dd_pct": round(h2.max_dd_pct, 2)},
        "verdict": verdict,
    }

    if not args.no_save:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = OUT_DIR / f"sell_in_may_{args.period}_{stamp}.json"
        out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"\nJSON: {out_path}")

    return 0 if verdict in ("PASS", "MARGINAL_PASS") else 1


if __name__ == "__main__":
    sys.exit(main())
