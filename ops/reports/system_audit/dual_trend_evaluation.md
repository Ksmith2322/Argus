# forge_dual_trend disciplined-gate evaluation

Generated: 2026-05-24T17:19:18.088323+00:00

## Backtest summary

- **universe**: ['SPY', 'QQQ', 'IWM', 'DIA', 'EFA', 'EEM', 'GLD', 'TLT']
- **lookback_months**: 12
- **rf_annual**: 0.045
- **n_trades_assetmonths**: 1146
- **n_portfolio_months**: 214
- **fraction_time_invested**: 0.628
- **win_rate**: 0.597
- **profit_factor**: 1.53
- **avg_pct_per_assetmonth**: 0.7521
- **compound_growth_pct**: 249.7
- **cagr_pct**: 7.27
- **max_drawdown_pct**: 26.41
- **first_month**: 2007-06
- **last_month**: 2026-05

## Disciplined gate

```
=== promotion_panel: forge_dual_trend (broad-8 lookback=12mo) ===
n_trades=1146  promotion_floor=1.2  confidence=0.95

layer                                        n      PF   CI_lo     CI_hi  verdict
-------------------------------------------------------------------------------------
iid_bootstrap_slip_0bp                    1146   1.530   1.314     1.789     PASS
iid_bootstrap_slip_10bp                   1146   1.446   1.243     1.689     PASS
block_bootstrap_b3_slip_10bp              1146   1.446   1.186     1.783     fail
block_bootstrap_b5_slip_10bp              1146   1.446   1.157     1.817     fail
block_bootstrap_b8_slip_10bp              1146   1.446   1.152     1.848     fail
period_stability_h1_slip_10bp              573   1.279   1.043     1.595     fail
period_stability_h2_slip_10bp              573   1.645   1.329     2.050     PASS
p_value_block_bootstrap                   1146   0.047   0.000     0.000     fail

p-value (block bootstrap, P[PF<=floor]): 0.04715
Bonferroni alpha_eff @ N=350: 0.00014

HEADLINE: FAIL  (3/8 layers passed)
```

## Cross-era OOS

| Era | n | PF | CI lower | CI upper | WR | pass? |
|---|---|---|---|---|---|---|
| 2006-2016 | 638 | 1.42 | 1.15 | 1.75 | 58.2% | no |
| 2016-2026 | 622 | 1.60 | 1.30 | 1.97 | 62.2% | YES |