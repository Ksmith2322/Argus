"""Run the variant overlap diagnostic on the xs_momentum fleet.

Answers the Comprehensive Audit's open question: are the 5 xs_momentum
variants actually independent strategies or are they all picking the
same names? If the median pairwise overlap is high, the fleet's
"5 strategies" is largely 1 strategy in costume.

Usage:
    python -m ops.audit.run_variant_overlap
    python -m ops.audit.run_variant_overlap --period 20y
    python -m ops.audit.run_variant_overlap --min-votes 4  --slippage-bps 10
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from helio.variant_overlap import ConsensusConfig, analyze_variant_overlap

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"
DOC_DIR = REPO / "docs" / "AUDIT_2026_05_25_PART2"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", default="10y",
                        help="Backtest period for each variant (default 10y; "
                             "20y slower but richer signal)")
    parser.add_argument("--min-votes", type=int, default=3,
                        help="Min variants that must pick a ticker for it to "
                             "be a consensus pick (default 3 of 5)")
    parser.add_argument("--slippage-bps", type=float, default=10.0,
                        help="Round-trip slippage in bps applied to consensus PnL")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--save-md", action="store_true",
                        help="Write a markdown summary to docs/AUDIT_2026_05_25_PART2/")
    args = parser.parse_args()

    cfg = ConsensusConfig(
        period=args.period, min_votes=args.min_votes,
        slippage_bps_rt=args.slippage_bps,
    )
    print(f"Running variant overlap (period={args.period}, "
          f"min_votes={args.min_votes}, slippage_rt={args.slippage_bps}bps)...")
    result = analyze_variant_overlap(cfg)

    md = result.summary_md()
    print()
    print(md)

    if args.save_md:
        DOC_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        md_path = DOC_DIR / f"VARIANT_OVERLAP_{stamp}.md"
        md_path.write_text(md, encoding="utf-8")
        print(f"\nSaved: {md_path}")

    # Always save JSON
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    summary = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "period": result.period,
        "min_votes": result.min_votes,
        "total_months": result.total_months,
        "months_with_consensus_pick": result.months_with_consensus_pick,
        "median_pairwise_jaccard": result.median_pairwise,
        "pairwise_overlap": {
            f"{a}__{b}": v for (a, b), v in result.pairwise_overlap.items()
        },
        "top_consensus_tickers": result.top_consensus_tickers[:25],
        "consensus_sleeve": {
            "n_trades": len(result.consensus_pnl_pcts),
            "pf": round(result.consensus_pf, 3),
            "pf_ci_lower": round(result.consensus_pf_ci_lower, 3),
            "pf_ci_upper": round(result.consensus_pf_ci_upper, 3),
            "avg_ret_pct": round(result.consensus_avg_ret_pct, 3),
            "total_compound_pct": round(result.consensus_total_compound, 1),
        },
    }
    out_path = OUT_DIR / f"variant_overlap_{stamp}.json"
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"JSON: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
