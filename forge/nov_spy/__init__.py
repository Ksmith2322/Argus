"""forge.nov_spy — November SPY effect (single-month seasonality).

STATUS: BACKTEST_ONLY (2026-05-24)
  Tonight's monthly-seasonality research found November is the ONLY
  Bonferroni-significant month in 30y of SPY data (mean +2.77%, t=+3.75,
  p=0.0002, WR 80.6%). This module tests whether a "long SPY for all
  of November" strategy survives the disciplined gate. If it does,
  a live runner can be built. If n=30 is too small for the gate, the
  module stays as a research artifact.

EDGE THESIS

Equities show concentrated outperformance in November, driven by:
- Year-end mutual fund window-dressing (institutions add to winners)
- Reduced summer-vacation profit-taking pressure
- Early Santa-rally anticipation flows
- Post-Halloween end of tax-loss-harvesting selling pressure

The classical "sell in May / Halloween indicator" (long Nov-Apr) is
weakly significant overall (p=0.18 for full winter half) BUT November
itself is the dominant contributor.

RELATION TO forge_tom_spy

tom_spy holds the last 4 days of Oct + first 3 of Nov = ~7 days
in early November. nov_spy holds the ENTIRE month of November. The
two overlap on ~3 days (the first 3 of November). Expected
correlation: moderate, not zero.

CAVEAT

n=30 historical Novembers is small for the disciplined gate. The
bootstrap CI lower bound at this n will be wide. If the strategy
PARTIAL-PASSES, it should be allocated conservatively (e.g. 0.2× —
half of tom_spy's recommended 0.3×) until more data accumulates.
"""
