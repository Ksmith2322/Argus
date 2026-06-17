"""Disciplined-gate backtest of the Turn-of-Quarter SPY strategy.

Strategy agent #2 (docs/AUDIT_2026_05_25_PART2/STRATEGY.md). Tests
whether entry at MOC of last trading day of quarter + exit at MOC of
3rd trading day of next quarter clears the disciplined gate.

Usage:
    python -m ops.audit.run_turn_of_quarter_backtest
    python -m ops.audit.run_turn_of_quarter_backtest --period 30y
    python -m ops.audit.run_turn_of_quarter_backtest --slippage-bps 5
    python -m ops.audit.run_turn_of_quarter_backtest --exit-offset 5
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from helio.turn_of_quarter import (
    DEFAULT_PF_FLOOR,
    DEFAULT_SLIPPAGE_BPS_RT,
    DEFAULT_EXIT_OFFSET,
    backtest_turn_of_quarter,
    split_h1_h2,
)

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


def _verdict(p_full: bool, p_h1: bool, p_h2: bool) -> str:
    if p_full and p_h1 and p_h2:
        return "PASS"
    if p_full and (p_h1 or p_h2):
        return "MARGINAL_PASS"
    if p_h1 != p_h2:
        return "TIME_SPLIT_DIVERGES"
    return "FAIL"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default="SPY")
    parser.add_argument("--period", default="20y")
    parser.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS_RT)
    parser.add_argument("--exit-offset", type=int, default=DEFAULT_EXIT_OFFSET,
                        help="Nth trading day of new quarter to exit (default 3)")
    parser.add_argument("--pf-floor", type=float, default=DEFAULT_PF_FLOOR)
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()

    print(f"=== Turn-of-Quarter gate ===")
    print(f"ticker={args.ticker}  period={args.period}  "
          f"slippage_rt={args.slippage_bps}bps  exit_offset={args.exit_offset}  "
          f"pf_floor={args.pf_floor}")
    print()

    b = backtest_turn_of_quarter(
        ticker=args.ticker, period=args.period,
        slippage_bps_rt=args.slippage_bps, exit_offset=args.exit_offset,
    )
    if b.error:
        print(f"ERROR: {b.error}")
        return 2

    h1, h2 = split_h1_h2(b, pf_floor=args.pf_floor)
    passes_full = b.pf_ci_lower >= args.pf_floor
    verdict = _verdict(passes_full, h1.pass_floor, h2.pass_floor)

    print(f"n_trades={b.n_trades}  WR={b.win_rate*100:.1f}%  "
          f"avg_hold={b.avg_holding_days:.1f} days")
    print(f"PF={b.pf:.3f}  CI=[{b.pf_ci_lower:.3f}, {b.pf_ci_upper:.3f}]")
    print(f"avg/trade={b.avg_ret_pct:.2f}%  total compound={b.total_ret_pct_compounded:.1f}%  "
          f"max DD={b.max_dd_pct:.1f}%")
    print(f"H1: n={h1.n:3d}  PF={h1.pf:.2f}  CI_lo={h1.pf_ci_lower:.2f}  "
          f"avg={h1.avg_ret_pct:.2f}%  pass={h1.pass_floor}")
    print(f"H2: n={h2.n:3d}  PF={h2.pf:.2f}  CI_lo={h2.pf_ci_lower:.2f}  "
          f"avg={h2.avg_ret_pct:.2f}%  pass={h2.pass_floor}")
    print(f"VERDICT: {verdict}")

    summary = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ticker": args.ticker,
        "period": args.period,
        "slippage_bps_rt": args.slippage_bps,
        "exit_offset": args.exit_offset,
        "pf_floor": args.pf_floor,
        "n_trades": int(b.n_trades),
        "win_rate_pct": round(b.win_rate * 100, 2),
        "pf": round(b.pf, 3),
        "pf_ci_lower": round(b.pf_ci_lower, 3),
        "pf_ci_upper": round(b.pf_ci_upper, 3),
        "avg_holding_days": round(b.avg_holding_days, 1),
        "total_compound_pct": round(b.total_ret_pct_compounded, 1),
        "max_dd_pct": round(b.max_dd_pct, 1),
        "h1": {"n": int(h1.n), "pf": round(h1.pf, 3),
               "pf_ci_lower": round(h1.pf_ci_lower, 3),
               "avg_ret_pct": round(h1.avg_ret_pct, 3), "pass": bool(h1.pass_floor)},
        "h2": {"n": int(h2.n), "pf": round(h2.pf, 3),
               "pf_ci_lower": round(h2.pf_ci_lower, 3),
               "avg_ret_pct": round(h2.avg_ret_pct, 3), "pass": bool(h2.pass_floor)},
        "passes_full": bool(passes_full),
        "verdict": verdict,
    }
    if not args.no_save:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = OUT_DIR / f"turn_of_quarter_{args.ticker}_{args.period}_{stamp}.json"
        out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"\nJSON: {out_path}")
    return 0 if verdict in ("PASS", "MARGINAL_PASS") else 1


if __name__ == "__main__":
    sys.exit(main())
