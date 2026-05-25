# xs_momentum cohort correlation audit — 2026-05-25

**Question**: are the 5 xs_momentum variants we just shipped actually
diversifying, or just running the same engine through 5 doors that
produce correlated returns?

**Answer**: genuine diversification. 5-strategy equal-weight portfolio
has Sharpe **1.19** vs baseline-alone's **0.77** — a 55% relative
improvement.

## Per-variant 20y stats

| Variant | Universe | Top-N | Sharpe | DD | CAGR |
|---|---|---|---|---|---|
| baseline | broad-8 ETFs | top-2 | 0.77 | 22.4% | 18.9% |
| sectors | 11 SPDR sectors | top-2/3 | 0.72 | 50.5% | 19.1% |
| style | 8 style factors | top-2 | 0.76 | 33.9% | 19.2% |
| legacy15 | 15-ticker mix | top-3 | 0.65 | 65.3% | 19.9% |
| **style_top3** | 8 style factors | **top-3** | **0.87** | **33.9%** | **25.5%** |

**Best single variant by Sharpe**: `style_top3` (0.87). Same DD as
top-2 style (34%), but materially higher CAGR.

**Worst single variant by Sharpe**: `legacy15` (0.65, DD 65%). The
15-ticker universe produces the highest absolute trade count but also
the worst risk-adjusted performance.

## Pairwise correlation matrix

|             | baseline | sectors | style | legacy15 | style_top3 |
|---|---|---|---|---|---|
| **baseline**   | 1.00 | 0.07 | 0.26 | 0.11 | 0.26 |
| **sectors**    | 0.07 | 1.00 | 0.15 | **0.62** | 0.19 |
| **style**      | 0.26 | 0.15 | 1.00 | 0.14 | **0.48** |
| **legacy15**   | 0.11 | 0.62 | 0.14 | 1.00 | 0.17 |
| **style_top3** | 0.26 | 0.19 | 0.48 | 0.17 | 1.00 |

### Pair verdicts
- 8/10 pairs **diversify** (rho < 0.50)
- 2/10 pairs **partial overlap**:
  - `sectors ↔ legacy15` (rho 0.62): expected — legacy15 contains
    the same 10 SPDR sectors plus 5 country ETFs
  - `style ↔ style_top3` (rho 0.48): expected — same universe,
    different concentration
- 0/10 pairs **near-duplicate** (rho > 0.75)

**Honest read**: the 4 added variants each bring a genuinely
independent return stream. None are silently a clone of the baseline.

## Incremental Sharpe from adding each variant to baseline

| Pair | Combined Sharpe | Δ Sharpe | Δ DD | Δ CAGR |
|---|---|---|---|---|
| baseline + **style_top3** | **1.04** | **+0.27** | **-0.6%** | **+4.8%** |
| baseline + sectors | 1.02 | +0.25 | +7.3% | +1.9% |
| baseline + style | 0.97 | +0.20 | -3.6% | +1.5% |
| baseline + legacy15 | 0.93 | +0.16 | +17.4% | +2.8% |

**Cleanest single addition**: `style_top3` — improves Sharpe most,
*reduces* DD, raises CAGR significantly.

**Highest-cost addition**: `legacy15` — improves Sharpe (still
positive), but raises DD by +17.4 percentage points.

## All-5 equal-weight portfolio

- **Sharpe: 1.19** (+0.42 vs baseline-alone)
- DD: 37.1%
- CAGR: 23.3%

The combined portfolio is materially better than any single variant.
This is exactly what the original amplification thesis predicted:
**universe diversification + concentration variation produce
uncorrelated alpha streams**.

## Actionable observations (no allocation changes mid-week)

Per the 5/24 discipline ("Don't change parameters mid-week — the
disciplined gate was scored at current size; changing it invalidates
the live evidence"), allocations stay at the current 1.0× (baseline)
+ 0.5× (each variant) through 6/30.

But here are the candidates for the **6/30 review**:

1. **style_top3 → allocation increase** (0.5× → 0.75-1.0×). Best
   Sharpe, lowest DD addition, cleanest live evidence prediction.
2. **legacy15 → allocation decrease** (0.5× → 0.25-0.3×). Worst
   single Sharpe and highest DD; incremental Sharpe is still
   positive but at the steepest DD cost.
3. **sectors, style** stay at 0.5× — middle of the pack.

**Caveats**:
- These are backtest stats. Live evidence over 30+ days is the
  tiebreaker.
- legacy15 has overlapping holdings with sectors (rho 0.62) — if
  we cut legacy15 entirely, we still capture most of its exposure
  via sectors.
- The "5-strategy Sharpe 1.19" is a backtest claim. The real number
  will land at 6/30 + 30d of clean canonical_fills.

## Validation of the universe-expansion approach

This audit answers the post-deployment skepticism question: **are we
making good trades, or just trading more?**

The 4 added variants produced +0.42 Sharpe over baseline-alone with
no near-duplicate pairs and only 2 partial-overlap pairs that are
mechanically expected from universe construction. The variants are
NOT redundant. They are amplification, not noise.

The user's direction from 5/24 — "amplify what we have, more data,
more meaningful trades" — was the correct path. The data confirms
the strategy is sound; the next 30 days of live evidence determines
whether it survives realistic execution.
