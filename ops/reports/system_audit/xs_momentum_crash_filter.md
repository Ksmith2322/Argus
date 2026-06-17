# xs_momentum + momentum-crash regime filter

Generated: 2026-05-24T17:23:54.512083+00:00

## Comparison

| Metric | Unfiltered | Filtered | Delta |
|---|---|---|---|
| n_trades | 48 | 40 | -8.00 |
| PF | 3.79 | 2.97 | -0.82 |
| CAGR | 18.04% | 48.60% | +30.56% |
| Max DD | 22.39% | 36.36% | +13.97% |
| WR | 62.5% | 57.5% | -5.00 |

## Disciplined gate (filtered)

```
=== promotion_panel: xs_momentum + crash filter ===
n_trades=40  promotion_floor=1.2  confidence=0.95

layer                                        n      PF   CI_lo     CI_hi  verdict
-------------------------------------------------------------------------------------
iid_bootstrap_slip_0bp                      40   2.966   1.069     8.228     fail
iid_bootstrap_slip_10bp                     40   2.881   1.033     7.940     fail
block_bootstrap_b3_slip_10bp                40   2.881   0.765     7.623     fail
block_bootstrap_b5_slip_10bp                40   2.881   0.710     6.335     fail
block_bootstrap_b8_slip_10bp                40   2.881   0.656     6.213     fail
period_stability_h1_slip_10bp               20   1.412   0.403     8.102     fail
period_stability_h2_slip_10bp               20   4.349   1.073    18.417     fail
p_value_block_bootstrap                     40   0.156   0.000     0.000     fail

p-value (block bootstrap, P[PF<=floor]): 0.15595
Bonferroni alpha_eff @ N=350: 0.00014

HEADLINE: FAIL  (0/8 layers passed)
```