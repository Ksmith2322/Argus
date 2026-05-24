# forge_nov_spy disciplined-gate evaluation

Generated: 2026-05-24T16:43:53.827284+00:00

## Backtest summary (SPY, period=30y)

- **ticker**: SPY
- **period**: 30y
- **n_trades**: 30
- **win_rate**: 0.767
- **profit_factor**: 4.922
- **avg_pnl_pct**: 2.3844
- **total_pct**: 71.53
- **compound_growth_pct**: 98.72
- **cagr_when_invested_pct**: 2.32
- **max_drawdown_pct**: 8.68
- **spy_buyhold_cagr_pct**: 10.21
- **spy_buyhold_total_pct**: 1745.29
- **first_year**: 1996
- **last_year**: 2025

## Disciplined gate

```
=== promotion_panel: forge_nov_spy (SPY full November) ===
n_trades=30  promotion_floor=1.2  confidence=0.95

layer                                        n      PF   CI_lo     CI_hi  verdict
-------------------------------------------------------------------------------------
iid_bootstrap_slip_0bp                      30   4.922   1.853    42.644     PASS
iid_bootstrap_slip_5bp                      30   4.758   1.804    38.654     PASS
iid_bootstrap_slip_10bp                     30   4.601   1.747    35.470     PASS
block_bootstrap_b3_slip_10bp                30   4.601   1.754    26.264     PASS
block_bootstrap_b5_slip_10bp                30   4.601   1.718    17.123     PASS
block_bootstrap_b8_slip_10bp                30   4.601   1.797    14.100     PASS
period_stability_h1_slip_10bp               15   2.492   0.735   153.742     fail
period_stability_h2_slip_10bp               15  17.636   5.296   138.342     PASS
p_value_block_bootstrap                     30   0.000   0.000     0.000     PASS

p-value (block bootstrap, P[PF<=floor]): 0.00030
Bonferroni alpha_eff @ N=350: 0.00014

HEADLINE: MARGINAL_PASS  (8/9 layers passed)
```

## Cross-era OOS

| Era | n | PF | CI lower | CI upper | WR | avg | pass? |
|---|---|---|---|---|---|---|---|
| 2000-2010 | 10 | 1.59 | 0.39 | 18.49 | 70.0% | +0.95% | no |
| 2010-2026 | 16 | 18.66 | 5.63 | 233.32 | 68.8% | +2.80% | YES |