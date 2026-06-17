# Cohort risk audit

Generated: 2026-05-24T13:19:24.339983+00:00
Period: 10y  (107 aligned months)

## Pairwise Pearson correlation

```
             xs_momentum  gld_pm_long  tom_spy  spy_buyhold
xs_momentum        1.000        0.484    0.028        0.721
gld_pm_long        0.484        1.000   -0.126        0.128
tom_spy            0.028       -0.126    1.000        0.221
spy_buyhold        0.721        0.128    0.221        1.000
```

## Portfolio comparison

| Portfolio | CAGR | Vol | Max DD | Sharpe |
|---|---|---|---|---|
| current (xs_mom 1.0 + gld_pm 0.5) | 10.21% | 10.78% | 17.11% | +0.54 |
| proposed (+ tom_spy 0.3) | 9.76% | 9.11% | 14.08% | +0.58 |
| 100% xs_momentum | 12.80% | 14.98% | 23.86% | +0.58 |
| 100% SPY buy-and-hold | 15.23% | 16.08% | 23.93% | +0.69 |

## Caveat

gld_pm_long's monthly contribution is approximated as 30%-of-GLD-monthly-return (its observed time-in-market fraction from the disciplined-gate audit). This is a CONSERVATIVE proxy — the actual strategy is intraday and may have weaker correlation with GLD direction than the proxy implies.