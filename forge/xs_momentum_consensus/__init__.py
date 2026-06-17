"""forge.xs_momentum_consensus — shadow runner for the Variant Consensus Sleeve.

A meta-strategy that takes positions only when >=2 of 5 xs_momentum
variants pick the same ticker in the same month. Validated tonight by
helio/variant_overlap.py at MARGINAL_PASS:

  20y window, 5 variants, min_votes=2:
    n=209 trades, PF=2.43, bootstrap CI [1.43, 4.03] -> full PASS
    H1 (2005-2015): n=104, PF=1.85, CI lower 1.09 (FAIL — 9 hundredths
                    below the 1.20 floor)
    H2 (2015-2026): n=105, PF=3.08, CI lower 1.47 (PASS)
    compounded total: 15,596% over 20y (~28%/yr CAGR)

This is a SHADOW runner — it computes the consensus picks each month
and writes them to a virtual ledger but does NOT submit IBKR orders.
It exists to collect 60-90 days of live evidence on the consensus rule
applied to the variants' actual production picks (which historically
match backtest exactly, but live-vs-backtest divergence is the gap
real evidence closes).

Allocation stays at 0.0 in argus_flow/configs/allocation_factors.json
until live evidence supports a real-money or paper-allocation decision.

Full design + gate verdict: docs/AUDIT_2026_05_25_PART2/VARIANT_OVERLAP_FINDINGS.md
"""
