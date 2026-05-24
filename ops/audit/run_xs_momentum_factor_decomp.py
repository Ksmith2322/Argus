"""xs_momentum factor decomposition audit (2026-05-23 rigor sprint #2).

Answers the question: is forge_xs_momentum generating real alpha, or is
it just buying MOM factor beta that can be replicated by holding MTUM?

Procedure:
  1. Run the xs_momentum 10-year backtest (broad-8 universe, 252-21 lookback,
     top-2 monthly rebalance) and extract per-month equal-weight portfolio
     return.
  2. Fetch ETF-proxy factor returns over the same window (helio.factor_decomposition):
       - MKT_RF: SPY excess
       - SMB:    IWM - SPY
       - HML:    IWD - IWF
       - MOM:    MTUM - SPY
  3. OLS regression with Newey-West HAC standard errors (lag=3) since
     monthly strategy returns typically have positive serial correlation.
  4. Report annualized alpha + factor betas + t-stats + R² + IR.

USAGE:
    python -m ops.audit.run_xs_momentum_factor_decomp                  # default
    python -m ops.audit.run_xs_momentum_factor_decomp --period 5y      # shorter
    python -m ops.audit.run_xs_momentum_factor_decomp --no-newey-west  # plain OLS

OUTPUT:
    ops/reports/system_audit/xs_momentum_factor_decomp_<UTC>.json
    ops/reports/system_audit/xs_momentum_factor_decomp_summary.md
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", default="10y",
                        help="Backtest period for xs_momentum (default 10y)")
    parser.add_argument("--no-newey-west", action="store_true",
                        help="Disable Newey-West HAC errors (use plain OLS)")
    parser.add_argument("--rf-annual", type=float, default=0.045,
                        help="Annual risk-free rate for excess-return calc")
    args = parser.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[step 1] running xs_momentum backtest (period={args.period})...")
    from forge.xs_momentum.runner import backtest as xsm_backtest, monthly_portfolio_returns
    bt = xsm_backtest(period=args.period)
    if "error" in bt:
        print(f"backtest error: {bt['error']}")
        return 1
    print(f"[step 1a] computing true monthly portfolio returns (held-position aware)...")
    monthly = monthly_portfolio_returns(period=args.period)
    print(f"[step 1b] {len(monthly)} calendar months of portfolio returns collected")
    print(f"         portfolio CAGR: {bt.get('cagr_pct')}%, "
          f"max DD: {bt.get('max_drawdown_pct')}%, "
          f"trades: {bt.get('trades')}")

    print()
    print(f"[step 2a] regression against classic 4-factor model (US equity only)...")
    from helio.factor_decomposition import regress_returns_against_factors, render_report

    try:
        result_classic = regress_returns_against_factors(
            monthly,
            label="forge_xs_momentum vs US 4-factor (MKT/SMB/HML/MOM)",
            rf_annual=args.rf_annual,
            use_newey_west=not args.no_newey_west,
            include_cross_asset=False,
        )
    except Exception as exc:
        print(f"classic regression failed: {exc}")
        return 1

    print()
    print(f"[step 2b] regression against 8-factor model (cross-asset extended)...")
    try:
        result_extended = regress_returns_against_factors(
            monthly,
            label="forge_xs_momentum vs 8-factor (+DUR/GOLD/INTL_DEV/INTL_EM)",
            rf_annual=args.rf_annual,
            use_newey_west=not args.no_newey_west,
            include_cross_asset=True,
        )
    except Exception as exc:
        print(f"extended regression failed: {exc}")
        return 1

    text_classic = render_report(result_classic)
    text_extended = render_report(result_extended)
    print()
    print(text_classic)
    print()
    print(text_extended)
    text_report = text_classic + "\n\n" + text_extended
    result = result_extended  # extended is the more honest decomp for this strategy

    # Persist
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = OUT_DIR / f"xs_momentum_factor_decomp_{ts}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period_argument": args.period,
        "rf_annual_used": args.rf_annual,
        "newey_west_lag3": not args.no_newey_west,
        "backtest_summary": {
            "trades": bt.get("trades"),
            "win_rate": bt.get("win_rate"),
            "profit_factor": bt.get("profit_factor"),
            "cagr_pct": bt.get("cagr_pct"),
            "max_drawdown_pct": bt.get("max_drawdown_pct"),
            "portfolio_growth_pct": bt.get("portfolio_growth_pct"),
            "first_entry": bt.get("first_entry"),
            "last_exit": bt.get("last_exit"),
        },
        "decomposition_classic_4factor": result_classic.to_dict(),
        "decomposition_extended_8factor": result_extended.to_dict(),
        "report_text": text_report,
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str),
                         encoding="utf-8")

    md_path = OUT_DIR / "xs_momentum_factor_decomp_summary.md"
    md_lines: list[str] = []
    md_lines.append("# xs_momentum factor decomposition")
    md_lines.append("")
    md_lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    md_lines.append("")
    md_lines.append(
        "ETF-proxy Fama-French + Momentum regression (SPY / IWM / IWD / "
        "IWF / MTUM). Caveats in helio/factor_decomposition.py module docstring."
    )
    md_lines.append("")
    md_lines.append("## Backtest summary")
    md_lines.append("")
    for k, v in payload["backtest_summary"].items():
        md_lines.append(f"- **{k}**: {v}")
    md_lines.append("")
    md_lines.append("## Decomposition result")
    md_lines.append("")
    md_lines.append("```")
    md_lines.append(text_report)
    md_lines.append("```")
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    print()
    print(f"JSON:     {json_path}")
    print(f"Markdown: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
