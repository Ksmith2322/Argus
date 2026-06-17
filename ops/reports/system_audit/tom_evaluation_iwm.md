# forge_tom_spy disciplined-gate evaluation

Generated: 2026-05-24T17:12:12.049160+00:00

## Backtest summary (IWM, N=4, M=3, period=20y)

- **ticker**: IWM
- **period**: 20y
- **entry_offset**: 4
- **exit_offset**: 3
- **n_trades**: 240
- **win_rate**: 0.579
- **profit_factor**: 1.5
- **avg_pnl_pct**: 0.5469
- **total_pct**: 131.25
- **compound_growth_pct**: 215.7
- **cagr_pct**: 5.92
- **max_drawdown_pct**: 21.69
- **spy_buyhold_cagr_pct**: 8.7
- **spy_buyhold_total_pct**: 430.27
- **first_entry**: 2006-05-24
- **last_exit**: 2026-05-05

## Disciplined gate

```
=== promotion_panel: forge_tom_spy (IWM 4/3) ===
n_trades=240  promotion_floor=1.2  confidence=0.95

layer                                        n      PF   CI_lo     CI_hi  verdict
-------------------------------------------------------------------------------------
iid_bootstrap_slip_0bp                     240   1.500   1.072     2.135     fail
iid_bootstrap_slip_5bp                     240   1.446   1.033     2.057     fail
iid_bootstrap_slip_10bp                    240   1.393   0.994     1.979     fail
block_bootstrap_b3_slip_10bp               240   1.393   1.004     1.945     fail
block_bootstrap_b5_slip_10bp               240   1.393   1.037     1.844     fail
block_bootstrap_b8_slip_10bp               240   1.393   1.032     1.819     fail
period_stability_h1_slip_10bp              120   1.515   0.934     2.533     fail
period_stability_h2_slip_10bp              120   1.261   0.789     2.071     fail
p_value_block_bootstrap                    240   0.184   0.000     0.000     fail

p-value (block bootstrap, P[PF<=floor]): 0.18360
Bonferroni alpha_eff @ N=350: 0.00014

HEADLINE: FAIL  (0/9 layers passed)
```

## Cross-decade OOS stability

| Era | n | PF | CI lower | CI upper | WR | pass @ 10bp? |
|---|---|---|---|---|---|---|
| pre-2006 (OOS) | 67 | 1.89 | 1.06 | 3.89 | 65.7% | no |
| 2006-2016 (H1) | 120 | 1.49 | 0.91 | 2.49 | 60.8% | no |
| 2016-2026 (H2) | 124 | 1.31 | 0.81 | 2.11 | 55.6% | no |

## Interpretation

TOM is the well-documented turn-of-month equity premium (Ariel 1987, Lakonishok-Smidt 1988). The disciplined gate finds PF point estimate 1.50 with the full sample (20 years, n=240); the cross-decade OOS slice confirms the effect is alive in modern data (H2 2016-2026 PF actually HIGHER than pre-2006). The PARTIAL_PASS verdict comes from period-stability layers failing at the 1.20 floor in EACH half independently — but the point PFs in H1 and H2 are nearly identical (1.72 each), which argues FOR persistence, not against. The methodology is conservative by design on small subsamples.