# Russell reconstitution research — IWM windowed returns

Generated: 2026-05-24T13:32:56.632123+00:00
Ticker: IWM  Years: 2010-2025

## Aggregate window statistics

| Window | n | mean | median | std | t-stat | p (two-sided) | WR |
|---|---|---|---|---|---|---|---|
| pre10_to_pre5 | 16 | -0.764% | +0.150% | 3.364% | -0.91 | 0.3638 | 56% |
| pre5_to_friday | 16 | +1.026% | +1.040% | 2.829% | +1.45 | 0.1468 | 69% |
| friday_to_post5 | 16 | +0.581% | +0.110% | 3.150% | +0.74 | 0.4607 | 50% |
| post5_to_post10 | 16 | +1.075% | +0.913% | 2.644% | +1.63 | 0.1038 | 62% |
| full_20day | 16 | +1.816% | +2.057% | 3.857% | +1.88 | 0.0596 | 75% |

## Interpretation

Each window is the compound return on IWM from close(day A) to close(day B), where A/B are offsets relative to the annual Russell reconstitution Friday (last Friday of June). Significance is tested with a one-sample t-statistic against zero mean. Bonferroni-adjust at 5 tests: significant at p < 0.01.