"""PEAD generalization test — does the edge survive on a non-curated universe?

The 5/19 backtest on Apollo's 16-name curated PEAD universe produced
PF=2.04 / CAGR +7.47%. Apollo curated those names BECAUSE they show
historical PEAD drift, so that result validates the implementation but
not the factor. This script runs the same PEAD strategy on a random
SPX-large-cap universe (30 names, none overlapping Apollo's list) to
test whether the edge generalizes.

  - If PF on the non-curated universe stays >= 1.5 → real factor, edge generalizes.
  - If PF drops to ~1.0 → the edge was Apollo's stock selection, not PEAD.
  - If PF is in between (1.2-1.5) → modest factor, weaker than the curated result.

Usage:
    python -m ops.audit.run_pead_generalization
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from forge.pead.runner import backtest as pead_backtest

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


# Top 30 SPX large-caps by market cap, NONE overlapping Apollo's curated
# PEAD watchlist (GOOGL, KLAC, PEP, WMT, MU, LRCX, MRNA, PLTR, SOFI, TSM,
# GILD, AMD, INTC, TMO, ARM, REGN). Distinct names so the comparison is
# clean.
NON_CURATED_UNIVERSE = [
    # Mega-cap tech (not in Apollo)
    "AAPL", "MSFT", "NVDA", "META", "AMZN", "TSLA", "AVGO",
    # Financials
    "JPM", "BAC", "WFC", "V", "MA",
    # Healthcare / pharma (not in Apollo)
    "UNH", "JNJ", "PFE", "ABT", "MRK", "LLY", "ABBV",
    # Consumer
    "HD", "PG", "COST", "KO", "DIS", "NFLX",
    # Energy / industrial / other
    "XOM", "CVX",
    # Software / enterprise
    "CSCO", "CRM", "ORCL",
]


def main() -> int:
    print("=" * 70)
    print("PEAD generalization test — non-curated SPX-30 universe")
    print("=" * 70)
    print(f"Universe ({len(NON_CURATED_UNIVERSE)} tickers): {NON_CURATED_UNIVERSE}")
    print()
    print("Running 5-year backtest... (this fetches earnings data per ticker)")
    print()

    result = pead_backtest(period="5y", verbose=True, universe=NON_CURATED_UNIVERSE)

    print(json.dumps(result, indent=2, default=str))

    # Verdict
    print()
    print("=" * 70)
    pf = result.get("profit_factor", 0)
    n = result.get("trades", 0)
    cagr = result.get("cagr_pct_scaled", 0)
    if n < 30:
        verdict = "INSUFFICIENT_SAMPLE"
        reason = f"n={n} < 30 — can't draw conclusions"
    elif pf >= 1.5:
        verdict = "GENERALIZES"
        reason = f"PF={pf} on non-curated universe stays >= 1.5; edge appears real"
    elif pf >= 1.2:
        verdict = "WEAK_GENERALIZATION"
        reason = f"PF={pf} between 1.2-1.5; modest factor; Apollo curation contributed materially"
    else:
        verdict = "DOES_NOT_GENERALIZE"
        reason = f"PF={pf} below 1.2; the edge was Apollo's curation, not PEAD"
    print(f"VERDICT: {verdict}")
    print(f"REASON:  {reason}")
    print("=" * 70)

    out_path = OUT_DIR / "pead_generalization_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "universe_label": "non_curated_spx_30",
        "universe": NON_CURATED_UNIVERSE,
        "apollo_curated_universe_excluded": [
            "GOOGL", "KLAC", "PEP", "WMT", "MU", "LRCX", "MRNA",
            "PLTR", "SOFI", "TSM", "GILD", "AMD", "INTC", "TMO", "ARM", "REGN",
        ],
        "result": result,
        "verdict": verdict,
        "reason": reason,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nreport written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
