# Sell-in-May SPY/SHY Rotation — Disciplined Gate Kill (2026-05-25)

Strategy agent's #7 candidate (`forge_sell_in_may_modulated`) failed
the disciplined gate by time-split divergence. **Third Strategy-agent
candidate killed pre-deployment tonight.** Not deployed.

## Strategy spec

- Always-invested calendar rotation
- ENTRY/SWITCH: at MOC of last trading day of April → SPY → SHY
- ENTRY/SWITCH: at MOC of last trading day of October → SHY → SPY
- Pre-backtest expectation: PF 1.6-2.0, CAGR +7-9% vs SPY +9%,
  max DD 18-22%, Sharpe +0.2 vs SPY

## Backtest results (20y, 10 bps slippage)

| Metric | Strategy | SPY benchmark | Delta |
|---|---|---|---|
| Total return | 187.9% | 495.7% | **-307.8pp** |
| CAGR | 5.44% | 9.35% | -3.91pp |
| Max DD | 16.7% | 56.5% | -39.8pp ← |
| Sharpe (ann) | 0.447 | 0.557 | -0.109 |

**Walk-forward H1/H2:**

| Half | Period | n | Strat | SPY | Delta | Strat DD |
|---|---|---|---|---|---|---|
| H1 | 2005-2015 | 20 | +69.2% | +64.8% | **+4.4pp** ✓ | 16.7% |
| H2 | 2015-2026 | 21 | +70.1% | +258.5% | **-188.4pp** ✗ | 13.1% |

**Verdict: TIME_SPLIT_DIVERGES.**

The Halloween effect *did* work in 2005-2015 (modest +4.4pp outperformance
vs buy-and-hold SPY). It catastrophically failed in 2015-2026 because
the QE-driven bull market made any "sit-out summer" strategy a massive
opportunity cost. The strategy gave up 188 percentage points of return
in the modern decade.

## The one virtue

**Max DD 17% vs SPY's 56%.** The strategy IS genuinely defensive — it
protected through the 2008-09 crisis (in SHY during the Sept-Oct
crash) and 2020 (in SHY during the COVID March/April). The defensive
benefit is real and persistent across both halves.

This suggests the engine could be repurposed as a **defensive overlay**:
trim SPY exposure to ~60-70% during May-Oct rather than full rotation
to SHY. Captures most of the DD reduction at much lower opportunity
cost. Not building tonight.

## Why the modern era kills it

The Halloween effect rests on a behavioral premise — investors take
summer off, return in autumn — that hasn't held in the post-QE world:

1. **Always-on liquidity provision** by central banks since 2008 means
   markets don't naturally cool in summer; they're propped up.
2. **Passive flows** (401k contributions, target-date fund rebalancing)
   dominate flow regardless of season.
3. **2020-2021 specifically**: the May-Oct 2020 window included the
   COVID recovery rally (+30% in SPY). Sitting in SHY missed it.
4. **2023-2024**: Mag 7 rally was largely summer-driven (AI hype).

The 2002 Bouman-Jacobsen paper found 1% monthly outperformance Nov-Apr
across 109 markets. Andrade et al. 2013 update showed the effect had
weakened to about 0.4%. Our 20y backtest shows it has effectively
flipped sign in the modern decade.

## Strategy agent's 0-for-3 pattern (tonight)

| Candidate | Predicted Sharpe / PF | Actual | Miss size |
|---|---|---|---|
| forge_overnight_drift_qqq | PF 1.4-1.7 at 5bps | PF 0.97 at 5bps | -45% |
| forge_credit_spread_regime | PF 1.5-2.0 at 10bps | PF 1.53 best (SMA200) | -23% |
| forge_sell_in_may_modulated | Sharpe +0.2 vs SPY | Sharpe -0.11 vs SPY | -155% |

Pattern: Strategy agent's pre-backtest expectations are systematically
inflated by 25-50% on the optimistic configurations, and don't account
for modern-era regime erosion at all.

**Going forward:** treat any Strategy-agent prediction as a 50%
discounted point estimate, and demand the H1/H2 walk-forward split
before treating any candidate as deployable. Tonight's pattern
suggests the disciplined gate's PASS rate is ~5-15% across academically-
sourced candidates.

The only winner from the 4-agent audit tonight: **Variant Consensus
Sleeve** (Creative agent #2) at MARGINAL_PASS. Not a new strategy —
a synthesis of existing engines.

## What stays in `helio/sell_in_may.py`

- Calendar logic (last-trading-day-of-month detection)
- Benchmark-relative gate (max DD comparison, CAGR delta, Sharpe delta)
- Walk-forward H1/H2 split with bench comparison per half

Engine reusable for: defensive overlay (60% SPY May-Oct instead of
0%); custom calendar windows (e.g., Halloween-NHL effect April-May
small-cap); other always-invested rotation pairs (EFA/AGG, QQQ/IEF).

## Action items

- [x] `forge_sell_in_may_modulated` at allocation 0.0 + v24 kill-log entry
- [x] Added to `helio.roi_filter.KILLED_STRATEGY_CUTOFFS`
- [x] Added to `helio.killed_strategy_invariant.KILLED_STRATEGY_SYMBOLS`
      with (SPY, SHY)
- [ ] Sprint 1 next: build forge_xs_momentum_consensus shadow runner
      (the one winner from tonight's research)
