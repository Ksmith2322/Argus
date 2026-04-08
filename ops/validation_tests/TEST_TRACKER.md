# Argus Validation Test Tracker — 80 Tests

## Status Key: [ ] Not started | [R] Running | [X] Complete | [-] Skipped (insufficient data)

## A. Statistical Edge Validation
- [X] 1. Walk-forward validation — 2/4 folds profitable, avg OOS PF 1.06 (not significant)
- [ ] 2. K-fold time-series cross-validation
- [X] 3. Monte Carlo trade shuffle — p=0.29, NOT significant
- [X] 4. Bootstrap confidence intervals — 95% CI [-3.65, +6.34] includes zero
- [X] 5. Random entry benchmark — loose trigger PF=1.04 (barely beats random)
- [ ] 6. Buy-and-hold benchmark
- [X] 7. p-value / t-test — p=0.60, NOT significant
- [X] 8. Minimum sample size — need 500 trades, have 36
- [X] 9. Overfitting correction (deflated Sharpe) — deflated Sharpe negative
- [ ] 10. Combinatorial purged cross-validation (CPCV)

## B. Entry Quality Analysis
- [X] 11. MAE analysis — avg 9.8 pips adverse excursion
- [X] 12. MFE analysis — avg 11.0 pips favorable, median 4-5 pips (targets too far)
- [ ] 13. Entry timing efficiency
- [ ] 14. Signal-to-entry delay impact
- [ ] 15. Entry price vs VWAP
- [ ] 16. False signal rate
- [X] 17. Edge decay curve — 90+ min holds profitable, <60 min lose

## C. Exit Optimization
- [ ] 18. Optimal stop distance (MAE-derived)
- [ ] 19. Optimal target distance (MFE-derived)
- [ ] 20. Optimal hold time (time-based MFE)
- [ ] 21. Trailing stop vs fixed stop comparison
- [ ] 22. Partial profit taking simulation
- [ ] 23. Breakeven stop simulation
- [ ] 24. Multi-exit strategy comparison

## D. Time & Session Analysis
- [X] 25. Hour-of-day heatmap — all 16 pairs analyzed (edge_discovery.json)
- [X] 26. Day-of-week breakdown — ALL trades on Friday (data artifact!)
- [ ] 27. Session breakdown (Asian/London/NY)
- [ ] 28. Month-of-year seasonality
- [ ] 29. Pre/post news event performance
- [ ] 30. London open first-hour analysis
- [ ] 31. NY overlap (13:00-17:00 UTC) analysis

## E. Market Regime Analysis
- [ ] 32. Performance by regime (trend/range/chop)
- [ ] 33. Performance by volatility bucket (low/med/high ATR)
- [ ] 34. Performance by spread bucket
- [ ] 35. Performance by trend strength
- [ ] 36. Regime transition analysis
- [ ] 37. VIX/volatility index correlation

## F. Risk & Drawdown Analysis
- [X] 38. Max consecutive losses — 5 in a row
- [X] 39. Drawdown duration — 8 trades to recover from max DD
- [X] 40. Recovery factor — 0.77 (net/DD)
- [ ] 41. Calmar ratio (CAGR / max DD)
- [ ] 42. Ulcer Index
- [X] 43. Tail risk — worst 5%: avg -20.2 pips
- [X] 44. VaR (95%) — -20.2 pips
- [X] 45. CVaR (95%) — -20.2 pips
- [ ] 46. Drawdown-at-risk (95th percentile DD)
- [X] 47. Ruin probability — 0.4% (500 pip barrier)

## G. Portfolio & Correlation
- [ ] 48. Cross-pair trade correlation
- [ ] 49. Portfolio equity curve simulation
- [ ] 50. Optimal capital allocation (Kelly / risk parity)
- [ ] 51. Diversification ratio
- [ ] 52. Currency exposure analysis
- [ ] 53. Correlation regime shifts

## H. Cost & Execution Sensitivity
- [X] 54. Commission sensitivity sweep — costs ON vs OFF: PF drops from 1.54 to 1.23
- [X] 55. Slippage sensitivity — PF 1.50@0pip -> 1.23@1pip -> <1.0@2pip (edge dies at 2 pips)
- [ ] 56. Spread widening stress test (1x, 2x, 3x normal)
- [ ] 57. Execution delay simulation (1-5 bar lag)
- [ ] 58. Partial fill impact
- [ ] 59. Requote / rejection rate modeling

## I. Data Quality & Robustness
- [ ] 60. Gap-excluded backtest
- [X] 61. Subsample stability — first half PF 1.52, second half PF 1.06 (degrading)
- [ ] 62. Data source comparison
- [ ] 63. Tick data vs 1m bar comparison
- [ ] 64. Bad tick injection stress test
- [ ] 65. Weekend gap stress test
- [ ] 66. Flash crash simulation

## J. Structural / Overfitting Guards
- [X] 67. Parameter sensitivity — PF range 0.24 across ±40% (ROBUST plateau)
- [X] 68. Parameter plateau — confirmed smooth, no cliff edges
- [ ] 69. Degrees of freedom audit
- [ ] 70. Data snooping correction (White's Reality Check)
- [ ] 71. Haircut on in-sample results
- [ ] 72. Blind out-of-sample test
- [ ] 73. Strategy decay monitoring

## K. Operational / Production Readiness
- [ ] 74. Kill switch response time test
- [ ] 75. Crash recovery validation
- [ ] 76. Network failure simulation
- [ ] 77. TWS restart survival
- [ ] 78. Dual-fill / phantom-fill test
- [ ] 79. Margin call simulation
- [ ] 80. End-of-day reconciliation accuracy
