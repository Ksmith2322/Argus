# SPY monthly seasonality research

Generated: 2026-05-24T13:48:23.869658+00:00
Period: 1995-2025 (371 monthly observations)

## Per-calendar-month stats

| Month | n | mean | median | std | t-stat | p | WR | Bonf-sig? |
|---|---|---|---|---|---|---|---|---|
| Jan | 30 | +0.55% | +1.55% | 4.22% | +0.72 | 0.4730 | 56.7% | no |
| Feb | 31 | +0.09% | +0.57% | 4.30% | +0.12 | 0.9073 | 54.8% | no |
| Mar | 31 | +1.27% | +1.72% | 4.49% | +1.57 | 0.1153 | 71.0% | no |
| Apr | 31 | +1.90% | +1.28% | 4.52% | +2.34 | 0.0192 | 74.2% | no |
| May | 31 | +1.00% | +1.51% | 3.63% | +1.54 | 0.1236 | 67.7% | no |
| Jun | 31 | +0.57% | +0.88% | 3.93% | +0.81 | 0.4163 | 67.7% | no |
| Jul | 31 | +1.46% | +1.80% | 3.92% | +2.08 | 0.0377 | 64.5% | no |
| Aug | 31 | -0.26% | +0.45% | 4.29% | -0.34 | 0.7326 | 61.3% | no |
| Sep | 31 | -0.40% | +0.80% | 5.06% | -0.44 | 0.6613 | 58.1% | no |
| Oct | 31 | +1.64% | +2.21% | 5.37% | +1.70 | 0.0898 | 61.3% | no |
| Nov | 31 | +2.77% | +3.06% | 4.12% | +3.75 | 0.0002 | 80.6% | YES |
| Dec | 31 | +0.99% | +1.21% | 3.53% | +1.57 | 0.1173 | 67.7% | no |

Bonferroni-adjusted alpha at 12 tests: 0.00417

## Halloween indicator (winter vs summer)

- Summer (May–Oct): n=186, mean = +0.669%
- Winter (Nov–Apr): n=185, mean = +1.268%
- Difference: +0.598%, t = +1.33, p = 0.1838

## Interpretation

**November SPY is the only Bonferroni-significant month** (mean +2.77%/month,
t=+3.75, p=0.0002, WR=80.6%). All other months fail the multi-testing
correction. The classical "Halloween indicator" (winter vs summer) is
real in point estimate (+0.60%/month difference) but not statistically
significant at the cohort level (p=0.18) — most of the winter-half
outperformance is concentrated in November alone.

**Potential strategy**: `forge_nov_spy` — long SPY at Nov 1 open, sell
at Nov 30 close. Single trade per year. With n=30 historical
observations this is borderline for the disciplined gate (small-sample
CI lower will be wide). Worth prototyping in a follow-up batch IF the
operator wants a calendar-anomaly counterpart to tom_spy.

**Caveats**:
- The November effect is mechanistically explained by year-end mutual
  fund window-dressing + reduced summer-vacation profit-taking pressure
  + early-Santa-rally anticipation. The mechanism is plausible but
  arbitrage capacity is limited (you can't run it more than once per
  year).
- tom_spy's late-Oct + early-Nov window captures part of the November
  effect. The two strategies would overlap by ~7 days but tom_spy is
  in-and-out while nov_spy holds the full month. Correlation likely
  moderate, not zero.
- The Halloween-full-season strategy (Nov 1 → Apr 30) shows PF 6.20
  WR 80% but the apparent edge is mostly the Nov effect bleeding into
  the seasonal headline. April is the next-strongest single month
  (mean +1.90%, t=+2.34) but doesn't survive Bonferroni.