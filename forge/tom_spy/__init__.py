"""forge.tom_spy — classical turn-of-month effect on SPY.

STATUS: BACKTEST_ONLY (2026-05-24)
  No runner module. This package exists to test whether the well-known
  TOM anomaly is still alive in SPY daily data. If the disciplined gate
  passes, then a live runner can be built. If it fails (likely — most
  modern literature says TOM was arbitraged out post-2000), the package
  stays as a research artifact.

EDGE THESIS
  Equities outperform on the last N trading days of the month plus the
  first M trading days of the next, vs. the rest of the month. Originally
  documented Ariel (1987), Lakonishok-Smidt (1988). Believed to be
  driven by month-end pension flows + tax-loss-harvesting reversal.

RELATION TO forge_tom_international
  Different universe. tom_international trades EEM/EWJ/VGK/EFA/FXI/INDA;
  this is SPY-only. tom_international was archived in the 5/20 sunset
  batch for ZERO live fires (operational), not because the edge was
  disproven. SPY-specific TOM has separate academic history and the
  disciplined gate hasn't been run on it.
"""
