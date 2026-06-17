"""forge.vix_carry — VIX term-structure contango harvest via SVXY long.

STATUS (2026-05-19): RESEARCH / SHADOW ONLY. Do NOT enable for paper
trading without further validation. See backtest result below.

Strategy thesis (Architect audit 2026-05-19, top-ranked new edge):
  VIX1/VIX2 futures spend ~80% of trading days in contango. SVXY
  passively collects this roll-yield via its inverse-VIX-futures
  rebalance. Filtered for "calm regime only" (VIX<20, SPY realized
  vol<15%, term structure in clear contango), the architect-asserted
  historical Sharpe was 0.9-1.4 net of friction.

2026-05-19 BACKTEST FINDING:
  Run --backtest --period 5y over 2021-2026 shows the strategy LOSES
  MONEY across every reasonable parameter variant tried (baseline,
  tighter entry 1.10/1.15 contango, looser exits VIX>=30 / contango<0.95).
  Best variant produced -8% total / -1.75% CAGR / -28.7% max drawdown.
  Worst: -33% total. Win rate consistently 20-29%, n=10-15 trades.

  Two honest interpretations: (a) the edge is regime-dependent and
  the 2021-2026 window (Volmageddon + COVID + rate shock + Aug-2024
  carry unwind) was hostile — pre-2018 data may be required; or (b)
  the edge has decayed and the strategy is no longer alpha.

  Either way: n=15 trades over 5 years is statistically uninterpretable
  (95% CI on win rate ~[0%, 51%]). This strategy is NOT promotable on
  current evidence. Allocation factor in allocation_factors.json should
  remain 0.0; the code lives here as a research artifact + the template
  for the next edge candidate to be tested.

  Next steps (if pursued): (1) extend backtest to pre-2018, (2) consider
  using actual VIX futures (VX1/VX2 contracts) instead of SVXY proxy,
  (3) try the opposite-direction sister strategy (long VXX puts on
  backwardation entry — short-vol blow-up insurance).

This package replaces the conceptual hole left by killing
forge_vix_intraday (5/12; n=61, IR=-4.36 vs SPY). It does NOT replace
it operationally — neither strategy is currently producing alpha."""
