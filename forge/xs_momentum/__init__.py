"""forge.xs_momentum — cross-sectional 12-1 momentum, long-only top quintile.

STATUS (2026-05-19): RESEARCH → PAPER-CANDIDATE. Allocation 0.0 through
5/31 reset. Eligible for activation in post-reset window.

9-year backtest result (2017-2026, 15 ETF universe):
  n=79 trades  WR=51.9%  PF=2.05  avg_win=+11.5%/avg_loss=-6.1%
  portfolio growth +279.83%, CAGR +16.22%, max DD 37.35%
  Per-ticker spread: all 15 tickers produced trades (range 2-10 each).
  Top contributors XLK (10), XLI (10), XLF (8), FXI/EWZ/XLY/XLU (6 each).

HONEST CAVEAT: 37% max DD reflects the well-documented "momentum
crashes" feature — the strategy gets hit hard at regime turns
(March 2020 COVID, late 2018, 2022 rate shock). Capital ladder kill
rules (-10%/-20% drawdown halts per project_kill_pause_engine.md)
would pause the strategy well before it reaches that level.
Activation should be at half-size (0.5×) until 12+ months of clean
post-reset trades establish the real-money drawdown profile.

Strategy thesis (Architect audit 2026-05-19, #3 new-edge candidate):
  Asness/AQR 1997+: cross-sectional momentum is one of the most robust
  documented edges in factor literature. Rank a basket of liquid ETFs
  by 12-1 trailing return (12 months ago to 1 month ago, excluding the
  most recent month to dodge short-term reversal), long the top
  quintile, rebalance monthly. Long-only variant (no short leg) reduces
  borrow + tax friction; cuts theoretical Sharpe roughly in half but
  remains positive.

Universe (15 ETFs): 10 US sectors (XL series) + 5 country ETFs.
Equal-weight top-3 long, rebalance first trading day each month.

Pure decision logic lives in helio.xs_momentum (testable in isolation).
"""
