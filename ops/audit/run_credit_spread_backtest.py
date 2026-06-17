"""Disciplined-gate backtest of the credit-spread-regime SPY strategy.

Strategy agent #3 from docs/AUDIT_2026_05_25_PART2/STRATEGY.md.
Tests whether a long-SPY position gated on HYG/LQD > 50d MA + VIX < 22
clears the disciplined gate (PF CI lower >= 1.20, walk-forward H1/H2 both pass).

Usage:
    python -m ops.audit.run_credit_spread_backtest
    python -m ops.audit.run_credit_spread_backtest --period 15y
    python -m ops.audit.run_credit_spread_backtest --slippage-bps 5
    python -m ops.audit.run_credit_spread_backtest --confirm-days 5
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from helio.credit_spread_regime import (
    DEFAULT_PF_FLOOR,
    DEFAULT_SLIPPAGE_BPS_RT,
    DEFAULT_SMA_WINDOW,
    DEFAULT_VIX_CAP,
    backtest_credit_spread,
    split_h1_h2,
)

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


def _verdict(passes_full: bool, passes_h1: bool, passes_h2: bool) -> str:
    if passes_full and passes_h1 and passes_h2:
        return "PASS"
    if passes_full and (passes_h1 or passes_h2):
        return "MARGINAL_PASS"
    if passes_h1 != passes_h2:
        return "TIME_SPLIT_DIVERGES"
    return "FAIL"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", default="20y")
    parser.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS_RT)
    parser.add_argument("--sma-window", type=int, default=DEFAULT_SMA_WINDOW)
    parser.add_argument("--vix-cap", type=float, default=DEFAULT_VIX_CAP)
    parser.add_argument("--confirm-days", type=int, default=0,
                        help="N consecutive entry_ok days required before entry "
                             "(default 0 = no confirmation). 5 halves whipsaw rate.")
    parser.add_argument("--pf-floor", type=float, default=DEFAULT_PF_FLOOR)
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()

    print(f"=== Credit-spread regime gate ===")
    print(f"period={args.period}  slippage_rt={args.slippage_bps}bps  "
          f"SMA={args.sma_window}  VIX_cap={args.vix_cap}  confirm_days={args.confirm_days}  "
          f"pf_floor={args.pf_floor}")
    print()

    b = backtest_credit_spread(
        period=args.period,
        slippage_bps_rt=args.slippage_bps,
        sma_window=args.sma_window,
        vix_cap=args.vix_cap,
        confirm_days=args.confirm_days,
    )
    if b.error:
        print(f"ERROR: {b.error}")
        return 2

    h1, h2 = split_h1_h2(b, pf_floor=args.pf_floor)
    passes_full = b.pf_ci_lower >= args.pf_floor
    verdict = _verdict(passes_full, h1.pass_floor, h2.pass_floor)

    print(f"n_trades = {b.n_trades}  WR = {b.win_rate*100:.1f}%  "
          f"avg_hold = {b.avg_holding_days:.1f} days")
    print(f"time in market: {b.time_in_market_pct:.1f}% "
          f"({b.n_days_engaged}/{b.n_days_total} trading days)")
    print(f"PF = {b.pf:.3f}  CI = [{b.pf_ci_lower:.3f}, {b.pf_ci_upper:.3f}]")
    print(f"avg per trade = {b.avg_ret_pct:.2f}%  "
          f"compounded total = {b.total_ret_pct_compounded:.1f}%  "
          f"max DD = {b.max_dd_pct:.1f}%")
    print(f"H1: n={h1.n:3d}  PF={h1.pf:.2f}  CI_lo={h1.pf_ci_lower:.2f}  "
          f"avg={h1.avg_ret_pct:.2f}%  pass={h1.pass_floor}")
    print(f"H2: n={h2.n:3d}  PF={h2.pf:.2f}  CI_lo={h2.pf_ci_lower:.2f}  "
          f"avg={h2.avg_ret_pct:.2f}%  pass={h2.pass_floor}")
    print(f"VERDICT: {verdict}")

    summary = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "period": args.period,
        "slippage_bps_rt": args.slippage_bps,
        "sma_window": args.sma_window,
        "vix_cap": args.vix_cap,
        "confirm_days": args.confirm_days,
        "pf_floor": args.pf_floor,
        "n_trades": int(b.n_trades),
        "win_rate_pct": round(b.win_rate * 100, 2),
        "pf": round(b.pf, 3),
        "pf_ci_lower": round(b.pf_ci_lower, 3),
        "pf_ci_upper": round(b.pf_ci_upper, 3),
        "avg_holding_days": round(b.avg_holding_days, 1),
        "time_in_market_pct": round(b.time_in_market_pct, 1),
        "compound_total_pct": round(b.total_ret_pct_compounded, 1),
        "max_dd_pct": round(b.max_dd_pct, 1),
        "h1": {"n": h1.n, "pf": round(h1.pf, 3), "pf_ci_lower": round(h1.pf_ci_lower, 3),
               "avg_ret_pct": round(h1.avg_ret_pct, 3), "pass": bool(h1.pass_floor)},
        "h2": {"n": h2.n, "pf": round(h2.pf, 3), "pf_ci_lower": round(h2.pf_ci_lower, 3),
               "avg_ret_pct": round(h2.avg_ret_pct, 3), "pass": bool(h2.pass_floor)},
        "passes_full": bool(passes_full),
        "verdict": verdict,
    }

    if not args.no_save:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = f"_conf{args.confirm_days}" if args.confirm_days else ""
        out_path = OUT_DIR / f"credit_spread_{args.period}{suffix}_{stamp}.json"
        out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"\nJSON: {out_path}")

    return 0 if (verdict in ("PASS", "MARGINAL_PASS")) else 1


if __name__ == "__main__":
    sys.exit(main())
