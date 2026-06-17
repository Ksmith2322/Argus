"""forge.coint_pairs — cointegrated pairs trading across 8 default pairs.

STATUS (2026-05-20): RESEARCH ONLY — BACKTEST FAILED. Allocation 0.0.
Code retained as template; do NOT activate without alternative validation.

5-year backtest results (2021-2026, 8 default pairs):
  n=184 trades, WR=62%, PF=0.91 (below 1.0)
  avg_win +3.4% / avg_loss -6.2%  (wrong R:R for the WR)
  total -40.9% unscaled, CAGR -1.51% scaled, max DD 13%
  Per-pair winners: V_MA (+26%), EWZ_EWW (+23%), TLT_IEF (+7%), MSFT_GOOGL (+2%)
  Per-pair LOSERS: GLD_SLV (-42%), XOM_CVX (-37%), KO_PEP (-16%), HD_LOW (-4%)

Parameter sweep (entry z {2.0, 2.5, 3.0}, stop z {3.0, 4.0}): NONE positive.
Tighter entry makes it WORSE — extreme deviations continue trending, they
don't mean-revert. This is the architect's #5 candidate failing the gate,
joining vix_carry. 2 of 4 architect-recommended new strategies have now
failed clean backtests.

Why it failed:
  - GLD/SLV decoupled fundamentally during 2021-2026 (gold = inflation
    hedge, silver = industrial; the cointegration broke).
  - XOM/CVX had idiosyncratic 2022-2024 events (oil shock, specific
    company news) that pushed spreads outside the trading range.
  - Tight-win / wide-loss profile: 62% WR isn't enough when avg loss
    is 1.8× avg win. Would need ~65%+ WR to break even.

This validates the discipline of backtest-before-ship. Would have shipped
a money-loser if we'd trusted the architect blindly.

Strategy thesis (Architect audit 2026-05-19, #5 new-edge candidate):
  Pairs trading on cointegrated asset pairs. Each pair has a stable
  hedge ratio (beta) such that spread = a - beta*b is mean-reverting.
  When the spread deviates >= 2 standard deviations from its rolling
  mean, enter the convergence trade (long the cheap leg / short the
  expensive leg). Exit at z = 0 (mean revert) or |z| >= 3 (regime
  break stop). Half-life filter (3-60 trading days) keeps us out of
  pairs whose cointegration has broken.

  Replaces forge_gdx_gld (one pair) with 8 diversified pairs sharing
  one runner. Capacity-rich at $10-100K because per-pair notional is
  small (~$5K per leg at any moment).

Pairs (default universe):
  KO / PEP        — consumer staples beverages
  XOM / CVX       — integrated oil majors
  HD / LOW        — home improvement
  GLD / SLV       — precious metals (subsumes forge_gdx_gld)
  EWZ / EWW       — LatAm equities
  TLT / IEF       — duration curve
  V / MA          — payment networks
  MSFT / GOOGL    — mega-cap tech

Pure decision logic lives in helio.cointegration (testable in isolation)."""
