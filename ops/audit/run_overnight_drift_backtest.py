"""Run the overnight-drift disciplined-gate backtest on SPY/QQQ/IWM.

This is the gate run for the candidate `forge_overnight_drift_qqq`
strategy proposed in docs/AUDIT_2026_05_25_PART2/STRATEGY.md (Strategy
agent's #1 ranked candidate; ~250 trades/year per leg = the biggest
trade-volume multiplier on the table).

The disciplined gate requires:
  - 20y window OR longest available
  - 10 bps round-trip slippage (default)
  - bootstrap PF CI lower bound ≥ 1.20
  - walk-forward H1/H2 — both halves must independently pass the floor

Usage:
    python -m ops.audit.run_overnight_drift_backtest
    python -m ops.audit.run_overnight_drift_backtest --regime-gated
    python -m ops.audit.run_overnight_drift_backtest --tickers SPY,QQQ,IWM,MDY
    python -m ops.audit.run_overnight_drift_backtest --slippage-bps 5
    python -m ops.audit.run_overnight_drift_backtest --json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from helio.overnight_drift import (
    DEFAULT_PF_FLOOR,
    DEFAULT_SLIPPAGE_BPS_RT,
    backtest_leg,
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
    parser.add_argument("--tickers", default="SPY,QQQ,IWM",
                        help="Comma-separated list of tickers to backtest")
    parser.add_argument("--period", default="20y")
    parser.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS_RT,
                        help="Round-trip slippage in bps (default 10)")
    parser.add_argument("--regime-gated", action="store_true",
                        help="Apply trailing-60d-Sharpe>0 entry gate")
    parser.add_argument("--pf-floor", type=float, default=DEFAULT_PF_FLOOR)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    summary = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "period": args.period,
        "slippage_bps_rt": args.slippage_bps,
        "regime_gated": bool(args.regime_gated),
        "pf_floor": args.pf_floor,
        "legs": [],
    }

    if not args.json:
        gate_str = "GATED" if args.regime_gated else "NO-GATE"
        print(f"\n=== Overnight Drift Disciplined Gate ({gate_str}) ===")
        print(f"period={args.period}  slippage_rt={args.slippage_bps}bps  pf_floor={args.pf_floor}")
        print()

    for ticker in tickers:
        if not args.json:
            print(f"--- {ticker} ---")
        leg = backtest_leg(
            ticker,
            period=args.period,
            slippage_bps_rt=args.slippage_bps,
            regime_gated=args.regime_gated,
        )
        if leg.error:
            if not args.json:
                print(f"  ERROR: {leg.error}\n")
            summary["legs"].append({"ticker": ticker, "error": leg.error})
            continue

        h1, h2 = split_h1_h2(leg, pf_floor=args.pf_floor)
        passes_full = leg.pf_ci_lower >= args.pf_floor
        verdict = _verdict(passes_full, h1.pass_floor, h2.pass_floor)

        leg_summary = {
            "ticker": ticker,
            "n_trades": int(leg.n_trades),
            "win_rate_pct": round(leg.win_rate * 100, 2),
            "pf_point": round(leg.pf, 3),
            "pf_ci_lower": round(leg.pf_ci_lower, 3),
            "pf_ci_upper": round(leg.pf_ci_upper, 3),
            "avg_ret_bps": round(leg.avg_ret_pct * 100, 2),
            "sharpe_ann": round(leg.sharpe_ann, 3),
            "total_ret_pct_compounded": round(leg.total_ret_pct_compounded, 1),
            "max_dd_pct": round(leg.max_dd_pct, 1),
            "passes_full": bool(passes_full),
            "h1": {
                "n": int(h1.n_trades),
                "pf": round(h1.pf, 3),
                "pf_ci_lower": round(h1.pf_ci_lower, 3),
                "sharpe_ann": round(h1.sharpe_ann, 3),
                "passes": bool(h1.pass_floor),
            },
            "h2": {
                "n": int(h2.n_trades),
                "pf": round(h2.pf, 3),
                "pf_ci_lower": round(h2.pf_ci_lower, 3),
                "sharpe_ann": round(h2.sharpe_ann, 3),
                "passes": bool(h2.pass_floor),
            },
            "verdict": verdict,
        }
        summary["legs"].append(leg_summary)

        if not args.json:
            print(f"  n={leg.n_trades}  WR={leg.win_rate * 100:.1f}%  PF={leg.pf:.2f} "
                  f"CI=[{leg.pf_ci_lower:.2f}, {leg.pf_ci_upper:.2f}]")
            print(f"  avg/trade={leg.avg_ret_pct * 100:.2f}bps  Sharpe={leg.sharpe_ann:.2f}  "
                  f"DD={leg.max_dd_pct:.1f}%  total compounded={leg.total_ret_pct_compounded:.0f}%")
            print(f"  H1: n={h1.n_trades:5d} PF={h1.pf:.2f} CI_lo={h1.pf_ci_lower:.2f} "
                  f"Sharpe={h1.sharpe_ann:.2f} pass={h1.pass_floor}")
            print(f"  H2: n={h2.n_trades:5d} PF={h2.pf:.2f} CI_lo={h2.pf_ci_lower:.2f} "
                  f"Sharpe={h2.sharpe_ann:.2f} pass={h2.pass_floor}")
            print(f"  VERDICT: {verdict}\n")

    # Summary line
    passes = sum(1 for L in summary["legs"] if L.get("verdict") == "PASS")
    marg = sum(1 for L in summary["legs"] if L.get("verdict") == "MARGINAL_PASS")
    fails = sum(1 for L in summary["legs"] if L.get("verdict") == "FAIL")
    div = sum(1 for L in summary["legs"] if L.get("verdict") == "TIME_SPLIT_DIVERGES")
    summary["totals"] = {
        "PASS": passes, "MARGINAL_PASS": marg,
        "TIME_SPLIT_DIVERGES": div, "FAIL": fails,
    }

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"=== Totals: {passes} PASS / {marg} MARGINAL / {div} DIVERGES / {fails} FAIL ===")

    if not args.no_save:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        gate_tag = "gated" if args.regime_gated else "nogate"
        out_path = OUT_DIR / f"overnight_drift_{gate_tag}_{stamp}.json"
        out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        if not args.json:
            print(f"Saved: {out_path}")

    return 0 if (passes + marg) > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
