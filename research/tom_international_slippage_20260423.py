"""research/tom_international_slippage_20260423.py — Decide: activate or kill?

tom_international backtest: PF 1.31 over 1,249 trades on turn-of-month effect
(EEM/EWJ/VGK). Per-trade return averages 0.003% — which is tiny relative to
retail transaction costs. This script overlays realistic cost assumptions
and reports whether the edge survives.

Assumptions (retail via IBKR Lite):
  - Commission: 0 (IBKR Lite = zero commission on US stocks/ETFs)
  - Spread: 1-3 bps typical on liquid international ETFs (EEM=$0.01, EWJ=$0.01, VGK=$0.02)
  - Slippage (actual vs. observed close): 2-5 bps on market-on-close orders
  - Combined round-trip friction: 10-20 bps per trade (5-10 bps per side × 2)

Verdict: does 0.003% (≈ 0.3 bp) per-trade net of friction remain positive?

Run:
    python research/tom_international_slippage_20260423.py

Writes result to stderr. Nothing to wire up — this is a one-shot analysis.
"""
from __future__ import annotations
import csv
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CSV = REPO / "forge" / "tom_international_trades.csv"


def main() -> None:
    with CSV.open() as f:
        rows = list(csv.DictReader(f))
    rets = [float(r["return"]) for r in rows if r.get("return")]
    n = len(rets)
    gross_mean = sum(rets) / n if n else 0
    gross_total = sum(rets)
    wins = sum(1 for r in rets if r > 0)
    win_rate = wins / n if n else 0
    gross_pf = (sum(r for r in rets if r > 0) /
                abs(sum(r for r in rets if r < 0))) if any(r < 0 for r in rets) else float('inf')

    print(f"=== tom_international slippage analysis ({n} trades) ===")
    print(f"GROSS (no friction):")
    print(f"  per-trade mean return: {gross_mean*100:+.4f}% = {gross_mean*10000:+.1f} bps")
    print(f"  total gross return:    {gross_total*100:+.1f}%")
    print(f"  win rate:              {win_rate*100:.1f}%")
    print(f"  profit factor:         {gross_pf:.2f}")
    print()

    # Overlay friction scenarios (bps per round-trip)
    print("NET (round-trip friction):")
    print(f"  {'friction_bps':>12} | {'per-trade mean':>15} | {'annual est':>12} | {'PF':>6} | verdict")
    print(f"  {'-'*12}-+-{'-'*15}-+-{'-'*12}-+-{'-'*6}-+--------")

    for friction_bps in [0, 5, 10, 15, 20, 30]:
        friction_pct = friction_bps / 10000.0
        net_rets = [r - friction_pct for r in rets]
        net_mean = sum(net_rets) / n
        net_annual = net_mean * 250  # ~250 trading days - loose approx assuming monthly turn-of-month
        # Actually tom_international is 12 trades/year × 3 instruments = 36 trades/year, adjust:
        net_annual_actual = net_mean * 36
        net_wins = sum(1 for r in net_rets if r > 0)
        net_losses = [r for r in net_rets if r < 0]
        net_pf = (sum(r for r in net_rets if r > 0) /
                  abs(sum(net_losses))) if net_losses else float('inf')
        verdict = "EDGE HOLDS" if net_mean > 0 else ("BREAK-EVEN" if abs(net_mean * 10000) < 0.5 else "EDGE DESTROYED")
        print(f"  {friction_bps:>12} | {net_mean*10000:>+12.2f} bps | {net_annual_actual*100:>+10.2f}% | {net_pf:>6.2f} | {verdict}")

    print()
    print("CONCLUSIONS:")
    if gross_mean * 10000 < 1.0:
        print("  • Gross edge is <1 bp per trade. Almost no friction band preserves it.")
    else:
        print(f"  • Gross edge is {gross_mean*10000:.1f} bps per trade.")
    # Retail realistic friction: 10-15 bps round-trip
    realistic_fric = 12 / 10000.0
    realistic_net = gross_mean - realistic_fric
    print(f"  • At realistic retail friction (12 bps round-trip): per-trade net = {realistic_net*10000:+.2f} bps")
    if realistic_net > 0:
        print(f"  • ACTIVATE: edge survives realistic costs (annual ≈ {realistic_net*36*100:+.2f}%)")
    else:
        print(f"  • DO NOT ACTIVATE: costs destroy the edge. Retire tom_international to _research_archive.")


if __name__ == "__main__":
    main()
