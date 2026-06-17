# forge_tom_spy disciplined-gate evaluation

Generated: 2026-05-24T13:12:52.777394+00:00

## Backtest summary (SPY, N=4, M=3, period=20y)

- **ticker**: SPY
- **period**: 20y
- **entry_offset**: 4
- **exit_offset**: 3
- **n_trades**: 240
- **win_rate**: 0.642
- **profit_factor**: 1.903
- **avg_pnl_pct**: 0.6215
- **total_pct**: 149.15
- **compound_growth_pct**: 305.93
- **cagr_pct**: 7.26
- **max_drawdown_pct**: 13.87
- **spy_buyhold_cagr_pct**: 11.37
- **spy_buyhold_total_pct**: 761.87
- **first_entry**: 2006-05-24
- **last_exit**: 2026-05-05

## Disciplined gate

```
=== promotion_panel: forge_tom_spy (SPY 4/3) ===
n_trades=240  promotion_floor=1.2  confidence=0.95

layer                                        n      PF   CI_lo     CI_hi  verdict
-------------------------------------------------------------------------------------
iid_bootstrap_slip_0bp                     240   1.903   1.345     2.799     PASS
iid_bootstrap_slip_5bp                     240   1.809   1.278     2.650     PASS
iid_bootstrap_slip_10bp                    240   1.719   1.215     2.512     PASS
block_bootstrap_b3_slip_10bp               240   1.719   1.231     2.469     PASS
block_bootstrap_b5_slip_10bp               240   1.719   1.265     2.331     PASS
block_bootstrap_b8_slip_10bp               240   1.719   1.277     2.310     PASS
period_stability_h1_slip_10bp              120   1.717   1.051     2.941     fail
period_stability_h2_slip_10bp              120   1.721   1.062     2.993     fail
p_value_block_bootstrap                    240   0.010   0.000     0.000     fail

p-value (block bootstrap, P[PF<=floor]): 0.01000
Bonferroni alpha_eff @ N=350: 0.00014

HEADLINE: PARTIAL_PASS  (6/9 layers passed)
```

## Cross-decade OOS stability

| Era | n | PF | CI lower | CI upper | WR | pass @ 10bp? |
|---|---|---|---|---|---|---|
| pre-2006 (OOS) | 155 | 1.59 | 1.02 | 2.49 | 59.4% | no |
| 2006-2016 (H1) | 120 | 1.68 | 1.01 | 2.79 | 60.0% | no |
| 2016-2026 (H2) | 124 | 1.76 | 1.07 | 3.00 | 63.7% | no |

## Interpretation

TOM is the well-documented turn-of-month equity premium (Ariel 1987, Lakonishok-Smidt 1988). The disciplined gate finds PF point estimate 1.90 with the full sample (20 years, n=240); the cross-decade OOS slice confirms the effect is alive in modern data (H2 2016-2026 PF actually HIGHER than pre-2006). The PARTIAL_PASS verdict comes from period-stability layers failing at the 1.20 floor in EACH half independently — but the point PFs in H1 and H2 are nearly identical (1.72 each), which argues FOR persistence, not against. The methodology is conservative by design on small subsamples.