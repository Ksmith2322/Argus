# ROI Proof Report (2026-05-13T05:32:19.040504+00:00)

Window: **post_reset** (post_reset cutoff: 2026-04-23T00:00:00+00:00)
Total strategies evaluated: **9**

## Portfolio target
- Annualized ROI target: **30%**
- Per-strategy floors: Sortino ≥ 0.8, IR ≥ 0.5, PF ≥ 1.25

## Verdict tally
- **SCALE_CANDIDATE**: 0 []
- **PROMISING_LOW_SAMPLE**: 0 []
- **SHADOW_ONLY**: 0 []
- **REPAIR**: 0 []
- **FAILS_SPY_BENCHMARK**: 0 []
- **KILL**: 0 []
- **INSUFFICIENT_SAMPLE**: 6 ['argus_usdjpy', 'forge_aud_asian_breakout', 'forge_gld_pm_long', 'forge_jpy_pm_short', 'forge_nq_london_close', 'forge_nq_overnight']
- **ALREADY_KILLED**: 3 ['forge_multi_orb', 'forge_spy_mean_rev', 'forge_vix_intraday']

## Per-strategy summary

| strategy | n | PF | Sortino | IR | ann_ROI | excess_vs_SPY | verdict |
|---|---:|---:|---:|---:|---:|---:|---|
| argus_usdjpy | 4 | 54.7667 | INSUFFICIENT_SAMPLE(n=2/required=30) | INSUFFICIENT_SAMPLE(n=2/required=30) | 1.19% | +0.0000 | INSUFFICIENT_SAMPLE |
| forge_aud_asian_breakout | 2 | 0.0 | INSUFFICIENT_SAMPLE(n=2/required=30) | INSUFFICIENT_SAMPLE(n=2/required=30) | -2.46% | -0.0004 | INSUFFICIENT_SAMPLE |
| forge_gld_pm_long | 22 | 4.4516 | INSUFFICIENT_SAMPLE(n=12/required=30) | INSUFFICIENT_SAMPLE(n=12/required=30) | 165.59% | +0.0032 | INSUFFICIENT_SAMPLE |
| forge_jpy_pm_short | 8 | 1.729 | INSUFFICIENT_SAMPLE(n=4/required=30) | INSUFFICIENT_SAMPLE(n=4/required=30) | 0.15% | +0.0000 | INSUFFICIENT_SAMPLE |
| forge_multi_orb | 168 | 0.4077 | -8.4927 | -5.7893 | -87.27% | -0.0001 | ALREADY_KILLED |
| forge_nq_london_close | 3 | 399.3023 | INSUFFICIENT_SAMPLE(n=3/required=30) | INSUFFICIENT_SAMPLE(n=3/required=30) | 7.03% | +0.0004 | INSUFFICIENT_SAMPLE |
| forge_nq_overnight | 16 | 2.7809 | INSUFFICIENT_SAMPLE(n=9/required=30) | INSUFFICIENT_SAMPLE(n=9/required=30) | 70.03% | +0.0017 | INSUFFICIENT_SAMPLE |
| forge_spy_mean_rev | 38 | 0.6279 | -12.3011 | -10.972 | -30.02% | -0.0001 | ALREADY_KILLED |
| forge_vix_intraday | 61 | 0.9774 | -5.7455 | -4.3553 | -9.23% | -0.0006 | ALREADY_KILLED |

## Note on sample-size discipline
Sortino and IR refuse to compute below n=30. PF is reported but should not drive decisions until n>=75 (per project_decisive_test_protocol.md). Real interpretation of these results requires post-5/31-reset clean data accumulating across the surviving fleet.