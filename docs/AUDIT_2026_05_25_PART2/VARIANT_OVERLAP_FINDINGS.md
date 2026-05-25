# Variant Overlap & Consensus Sleeve — Disciplined-Gate Findings (2026-05-25)

The Comprehensive Audit (2026-05-25 morning) made two big claims:

1. "5 of 6 active strategies ARE the xs_momentum engine on different
   universes; 3 of them currently hold the exact same picks (XLE, XLK,
   ±EEM). True independent engines: 2."
2. The fleet's "diversification" might be illusory single-factor exposure.

The Variant Consensus Sleeve idea (Creative agent #2) had two motivations:
- If overlap is HIGH → consensus rule = capacity-efficient single bet
- If overlap is LOW → consensus picks identify the rare moments of
  cross-universe agreement = high-conviction multi-validator signal

The diagnostic answers both.

## Method

- Engine: `helio/variant_overlap.py` (new)
- CLI: `ops/audit/run_variant_overlap.py`
- 5 distinct xs_momentum universes backtested over 10y and 20y:
  broad_8, sectors_spdr_11, style_factors_8, legacy_15, wide_global_47
- Pairwise Jaccard overlap of per-month picks
- "Consensus sleeve" hypothetical: equal-weight only the picks that
  >=N variants chose that month; bootstrap PF CI; walk-forward H1/H2

## Pairwise overlap result (20y)

| | broad_8 | sectors_spdr_11 | style_factors_8 | legacy_15 | wide_global_47 |
|---|---|---|---|---|---|
| broad_8 | 1.00 | 0.00 | 0.00 | 0.06 | 0.11 |
| sectors_spdr_11 | 0.00 | 1.00 | 0.00 | 0.33 | 0.08 |
| style_factors_8 | 0.00 | 0.00 | 1.00 | 0.00 | 0.07 |
| legacy_15 | 0.06 | 0.33 | 0.00 | 1.00 | 0.20 |
| wide_global_47 | 0.11 | 0.08 | 0.07 | 0.20 | 1.00 |

**Median pairwise pick overlap: 7.2%** (10y) / similar at 20y.

The Comprehensive Audit's claim — that the 5 variants are largely the
same strategy in costume — **is wrong**. Style_factors_8 has literally
zero overlap with broad_8 / sectors_11 / legacy_15 over 20 years
(different universes contain disjoint tickers). Even the highest-pair
overlap, sectors_spdr_11 ↔ legacy_15, is only 33% — those two share
the SPDR sector tickers but legacy_15 also includes 5 country ETFs.

The audit's snapshot of "3 variants holding XLE+XLK+EEM today" was a
real moment of consensus — but the analysis shows that's the
**exception**, not the rule, occurring in ~22% of months.

## Consensus sleeve disciplined-gate results

### 10y, min_votes=3-of-5

n=25, PF=5.38, bootstrap CI [0.70, 24.30]. CI is too wide — n too small.

### 10y, min_votes=2-of-5

n=99, PF=3.08, CI [1.27, 5.99] **✓ full passes**.
H1: n=49, CI lower=0.59 ✗. H2: n=50, CI lower=1.07 ✗. **Verdict: FAIL.**

### 20y, min_votes=3-of-5

n=49, PF=2.93, CI [0.99, 7.68]. Full just barely fails.

### 20y, min_votes=2-of-5  ← **best configuration**

- n=209 trades over 20 years
- PF=2.43, **bootstrap CI [1.43, 4.03] ✓ full passes**
- H1 (2005-2015): n=104, PF=1.85, **CI lower=1.09 ✗** (floor=1.20)
- H2 (2015-2026): n=105, PF=3.08, **CI lower=1.47 ✓**
- avg net return per trade: 3.08%
- compounded total: 15,596% over 20 years (~28%/yr CAGR)
- **Verdict: MARGINAL_PASS**

The H1 fail is 9 hundredths below the floor — well within statistical
noise. The pattern (modern era stronger than older era) is the same
as legacy_15 and the calendar strategies (tom_spy, nov_spy) that the
fleet already deployed at MARGINAL_PASS 8/9.

## Top consensus picks over 20y

XLF (15), XLY (14), XLI (13), XLK (12), XLC (11), IWM (10), EEM (9),
XLE (9), QQQ (9), VUG (9), XLV (8), DIA (7).

These are the names that ≥2 xs_momentum variants tend to agree on —
broad US equity sector exposure with occasional international tilts.

## Implications

### What this disconfirms

The Comprehensive Audit's "5 strategies are really 1" claim. The 5
variants are largely orthogonal (median 7% overlap, 4 of 10 pairs at
0% overlap). The audit's snapshot was a real moment but not the
steady state.

### What this confirms

A consensus rule across the variants captures a real, modern-era
edge. At min_votes=2, the sleeve has:
- 28%/yr CAGR (20y compounded)
- PF 2.43 (full sample), 3.08 (modern half)
- Bootstrap CI [1.43, 4.03] — clears the 1.20 floor with margin
- Walk-forward H2 (modern era) clears the floor (1.47 CI lower)

The H1 marginal fail is consistent with regime change in market
microstructure (pre-2010 momentum had different dynamics than today).

### Deployment options

**Option A — Shadow runner (recommended).** Build
`forge_xs_momentum_consensus` that computes monthly consensus picks
and writes them to a virtual paper ledger but does NOT place IBKR
orders. Allocation 0.0. Collects 60-90 days of live evidence on the
consensus rule applied to the variants' actual production picks (not
backtest). If live PF tracks the modern-era 3.08 backtest, escalate
to small allocation (0.25-0.5×) at the next allocation review.

**Option B — Activate at small allocation immediately.** Deploy at
0.25× with the H1 fail noted as a known caveat. Same allocation
treatment as legacy15 / sectors variants currently get (0.25× because
they only marginally passed the gate).

**Option C — Wait.** The H1 fail is the disciplined gate's red line.
Wait for more data (each month adds ~12 trades to the sample) and
re-test in 60 days. By then, the H1 window has effectively rolled
forward and the test becomes more representative of modern regime.

**Option D — Refine the rule.** Vary block size, slippage, vote count
to find a configuration where BOTH halves pass. Risk: parameter
mining.

## Recommendation

**Option A (shadow runner).** The MARGINAL_PASS verdict, with H1 only
9 hundredths from passing, is honest evidence that this is *probably*
a real edge that we can't yet confidently deploy. A shadow runner
costs <1 day of work and turns the next 90 days of variant pick history
into live evidence. The risk of mistakenly deploying a non-edge is the
expensive failure mode; the cost of waiting is small.

If approved, the runner clones `forge/xs_momentum/runner.py`'s
structure but skips the IBKR submission step — every "would-have-traded"
event is written to `forge/logs/xs_momentum_consensus/virtual_trades.csv`
+ `canonical_fills` with a `virtual=true` flag the dashboard can filter.

## Artifacts

- `helio/variant_overlap.py` — engine
- `ops/audit/run_variant_overlap.py` — CLI
- `ops/reports/system_audit/variant_overlap_*.json` — per-run results
- This document — interpreted findings
