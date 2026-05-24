# forge_fomc_drift disciplined-gate evaluation

Generated: 2026-05-24T13:38:56.795984+00:00

## Backtest summary

- **n_trades**: 216
- **profit_factor**: 1.582
- **win_rate**: 0.551
- **avg_return_pct**: 0.2158
- **total_return_pct**: 46.61
- **best_year**: 2009
- **worst_year**: 2018

## Disciplined gate

```
=== promotion_panel: forge_fomc_drift (SPY 24h pre-FOMC) ===
n_trades=216  promotion_floor=1.2  confidence=0.95

layer                                        n      PF   CI_lo     CI_hi  verdict
-------------------------------------------------------------------------------------
iid_bootstrap_slip_0bp                     216   1.582   1.087     2.365     fail
iid_bootstrap_slip_5bp                     216   1.421   0.977     2.111     fail
iid_bootstrap_slip_10bp                    216   1.276   0.879     1.890     fail
block_bootstrap_b3_slip_10bp               216   1.276   0.898     1.832     fail
block_bootstrap_b5_slip_10bp               216   1.276   0.921     1.760     fail
block_bootstrap_b8_slip_10bp               216   1.276   0.940     1.769     fail
period_stability_h1_slip_10bp              108   1.660   0.993     2.916     fail
period_stability_h2_slip_10bp              108   0.893   0.523     1.498     fail
p_value_block_bootstrap                    216   0.362   0.000     0.000     fail

p-value (block bootstrap, P[PF<=floor]): 0.36210
Bonferroni alpha_eff @ N=350: 0.00014

HEADLINE: FAIL  (0/9 layers passed)
```

## Cross-era OOS

| Era | n | PF | CI lower | CI upper | WR | avg/trade | pass @ slip? |
|---|---|---|---|---|---|---|---|
| 2000-2010 (pre-QE) | 85 | 1.80 | 1.00 | 3.39 | 62.4% | +0.332% | no |
| 2010-2018 (QE era) | 64 | 1.53 | 0.73 | 3.20 | 48.4% | +0.151% | no |
| 2018-2026 (post-QE) | 67 | 0.93 | 0.49 | 1.79 | 40.3% | -0.032% | no |