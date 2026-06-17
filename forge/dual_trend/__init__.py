"""forge.dual_trend — dual-momentum trend-following on cross-asset universe.

STATUS: BACKTEST_ONLY (2026-05-24)

EDGE THESIS

Per-asset trend-following (absolute momentum) on the same broad-8
universe xs_momentum uses (SPY/QQQ/IWM/DIA/EFA/EEM/GLD/TLT), with
a regime filter. For each asset:
  - Hold long if asset's trailing 12-month return > T-bill return
  - Else hold cash (T-bill yield)

Equal-weight portfolio across holdings. Rebalances monthly. Distinct
from xs_momentum because:
  - xs_momentum: cross-sectional RANK (top 2 of 8, picks rotate)
  - dual_trend:  absolute MOMENTUM (any/all assets can hold or be
                  flat; no fixed count)

Antonacci (2014) "Dual Momentum" combines absolute + relative momentum.
This is the "absolute" leg in isolation — simpler to test, gives a
clean comparison vs xs_momentum's "relative" leg.

CAVEAT

Cross-asset trend-following has been heavily studied (CTAs, Asness/AQR).
Modern post-2010 performance has been mixed. Tonight's test is whether
the EQUAL-WEIGHTED variant on this specific 8-asset universe survives
the disciplined gate at 10bp.
"""
