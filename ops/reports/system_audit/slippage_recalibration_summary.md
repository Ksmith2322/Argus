# Slippage Recalibration Report

Generated: 2026-05-24T04:12:33.090048+00:00

Compares the disciplined gate verdict at the published `slippage_bps_used` vs the realistic `_slippage_bps_realistic` recorded in `argus_flow/configs/promotion_gate_baseline.json` (v2).

## forge_pead (curated Apollo 16)

- n_trades: **69**
- headline verdict (combined): **FAIL** (0/8 layers)
- p-value (block bootstrap, P[PF<=1.20]): 0.07295

| Slippage | IID CI lower | All layers pass? |
|---|---|---|
| 10bp | 1.1043 | NO |
| 40bp | 1.0247 | NO |

<details><summary>Full panel report</summary>

```
=== promotion_panel: forge_pead (curated Apollo 16) ===
n_trades=69  promotion_floor=1.2  confidence=0.95

layer                                        n      PF   CI_lo     CI_hi  verdict
-------------------------------------------------------------------------------------
iid_bootstrap_slip_10bp                     69   2.244   1.104     4.040     fail
iid_bootstrap_slip_40bp                     69   2.086   1.025     3.756     fail
block_bootstrap_b3_slip_40bp                69   2.086   0.995     3.861     fail
block_bootstrap_b5_slip_40bp                69   2.086   0.955     4.084     fail
block_bootstrap_b8_slip_40bp                69   2.086   1.014     4.135     fail
period_stability_h1_slip_40bp               34   2.900   1.081     6.850     fail
period_stability_h2_slip_40bp               35   1.553   0.425     3.485     fail
p_value_block_bootstrap                     69   0.073   0.000     0.000     fail

p-value (block bootstrap, P[PF<=floor]): 0.07295
Bonferroni alpha_eff @ N=350: 0.00014

HEADLINE: FAIL  (0/8 layers passed)
```
</details>
