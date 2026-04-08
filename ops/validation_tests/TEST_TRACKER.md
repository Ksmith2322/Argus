# Argus Validation Test Tracker — 80 Tests

## Status Key: [ ] Not started | [R] Running | [X] Complete | [-] Skipped (insufficient data)

## A. Statistical Edge Validation
- [X] 1. Walk-forward validation — 2/4 folds profitable, avg OOS PF 1.06 (not significant)
- [X] 2. K-fold time-series cross-validation — 3/5 folds profitable (PASS)
- [X] 3. Monte Carlo trade shuffle — p=0.29, NOT significant
- [X] 4. Bootstrap confidence intervals — 95% CI [-3.65, +6.34] includes zero
- [X] 5. Random entry benchmark — loose trigger PF=1.04 (barely beats random)
- [X] 6. Buy-and-hold benchmark — +9.6pip over 8d, 27 trades (PASS)
- [X] 7. p-value / t-test — p=0.60, NOT significant
- [X] 8. Minimum sample size — need 500 trades, have 27
- [X] 9. Overfitting correction (deflated Sharpe) — deflated Sharpe negative
- [X] 10. CPCV — 7/10 combos profitable (70%) (PASS)

## B. Entry Quality Analysis
- [X] 11. MAE analysis — avg 9.8 pips adverse excursion
- [X] 12. MFE analysis — avg 11.0 pips favorable, median 4-5 pips (targets too far)
- [X] 13. Entry timing efficiency — 50% avg efficiency (PASS)
- [ ] 14. Signal-to-entry delay impact
- [ ] 15. Entry price vs VWAP
- [X] 16. False signal rate — 0% immediate reversals (PASS)
- [X] 17. Edge decay curve — 90+ min holds profitable, <60 min lose

## C. Exit Optimization
- [X] 18. Optimal stop distance — p50=13.6 p75=15.3 pips (PASS)
- [X] 19. Optimal target distance — p50=6.5 p75=10.9 pips (PASS)
- [X] 20. Optimal hold time — best=60-90min avg_pnl=+2.0pip (PASS)
- [X] 21. Trailing stop vs fixed stop — fixed +9.6 vs trail -24.3 (fixed wins)
- [X] 22. Partial profit taking — +57.9pip vs full +9.6pip (partial wins)
- [X] 23. Breakeven stop simulation — same as original
- [X] 24. Multi-exit comparison — partial profit best (+57.9pip)

## D. Time & Session Analysis
- [X] 25. Hour-of-day heatmap — all 16 pairs analyzed (edge_discovery.json)
- [X] 26. Day-of-week breakdown — ALL trades on Friday (data artifact!)
- [X] 27. Session breakdown — London best avg=+0.4pip (PASS)
- [X] 28. Month-of-year seasonality — 2 months analyzed
- [ ] 29. Pre/post news event performance
- [X] 30. London open first-hour — n=2 wr=100% avg=+14.2pip (PASS)
- [X] 31. NY overlap — n=4 wr=50% avg=-5.0pip (FAIL)

## E. Market Regime Analysis
- [X] 32. Performance by regime — 2 regimes, CHOPPY dominant (PASS)
- [X] 33. Performance by volatility bucket — med vol best wr=67% (PASS)
- [-] 34. Performance by spread — insufficient spread data
- [X] 35. Performance by trend strength — weak trend -5pip, strong +1.2pip (PASS)
- [X] 36. Regime transition — 24 transitions in 27 trades (PASS)
- [ ] 37. VIX/volatility index correlation

## F. Risk & Drawdown Analysis
- [X] 38. Max consecutive losses — 5 in a row
- [X] 39. Drawdown duration — 8 trades to recover from max DD
- [X] 40. Recovery factor — 0.77 (net/DD)
- [X] 41. Calmar ratio — 11.26 (PASS)
- [X] 42. Ulcer Index — 11.10 (FAIL, >10)
- [X] 43. Tail risk — worst 5%: avg -20.2 pips
- [X] 44. VaR (95%) — -20.2 pips
- [X] 45. CVaR (95%) — -20.2 pips
- [X] 46. Drawdown-at-risk — 27.6 pips at 95th pct (PASS)
- [X] 47. Ruin probability — 0.4% (500 pip barrier)

## G. Portfolio & Correlation
- [X] 48. Cross-pair correlation — some NaN (thin data), AUDJPY/AUDUSD r=0.48 (FAIL: NaN)
- [X] 49. Portfolio equity curve — final +9.6pip, peak +48.3pip (PASS)
- [X] 50. Kelly criterion — kelly=-20% (FAIL: negative kelly = no edge proven)
- [X] 51. Diversification ratio — NaN (FAIL: insufficient overlap)
- [X] 52. Currency exposure — JPY dominant (14 trades), USD 11 (PASS)
- [ ] 53. Correlation regime shifts

## H. Cost & Execution Sensitivity
- [X] 54. Commission sensitivity sweep — costs ON vs OFF: PF drops from 1.54 to 1.23
- [X] 55. Slippage sensitivity — PF 1.50@0pip -> 1.23@1pip -> <1.0@2pip (edge dies at 2 pips)
- [X] 56. Spread widening stress — 1x +9.6pip, 3x -44.5pip (FAIL at 2x)
- [X] 57. Execution delay — 0bar +9.6, 3bar -31.0 (FAIL at 1 bar delay)
- [X] 58. Partial fill impact — 100%=+9.6, 60%=+5.7 (PASS, linear)
- [X] 59. Requote/rejection — 5% rejection wipes edge (FAIL)

## I. Data Quality & Robustness
- [ ] 60. Gap-excluded backtest
- [X] 61. Subsample stability — first half PF 1.52, second half PF 1.06 (degrading)
- [ ] 62. Data source comparison
- [ ] 63. Tick data vs 1m bar comparison
- [ ] 64. Bad tick injection stress test
- [ ] 65. Weekend gap stress test
- [ ] 66. Flash crash simulation

## J. Structural / Overfitting Guards
- [X] 67. Parameter sensitivity — PF range 0.24 across +/-40% (ROBUST plateau)
- [X] 68. Parameter plateau — confirmed smooth, no cliff edges
- [X] 69. Degrees of freedom — 2.2 trades/param (FAIL: need 10+, have 27/12)
- [X] 70. White's Reality Check — p=0.498 (FAIL: NOT significant)
- [X] 71. Haircut on in-sample — after 30% haircut still +6.7pip (PASS)
- [X] 72. Blind out-of-sample — IS +48.2pip, OOS -38.7pip (FAIL: OOS negative)
- [X] 73. Strategy decay — 1st half +2.06, 2nd half -1.23 (FAIL: -160% decay)

## K. Operational / Production Readiness
- [ ] 74. Kill switch response time test
- [ ] 75. Crash recovery validation
- [ ] 76. Network failure simulation
- [ ] 77. TWS restart survival
- [ ] 78. Dual-fill / phantom-fill test
- [ ] 79. Margin call simulation
- [ ] 80. End-of-day reconciliation accuracy

## Summary (2026-04-07)
- **Complete: 63/80** (79%)
- **Remaining: 17** (mostly operational tests + data quality)
- **Key findings:** Edge fragile (OOS negative, p=0.50, decay -160%), sensitive to costs/delay. Partial profit taking significantly outperforms. London session best. Need 10x more trades for statistical significance.
