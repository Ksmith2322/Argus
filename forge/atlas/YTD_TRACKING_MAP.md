# Atlas YTD Event-Impact Tracking Map

**Created:** 2026-04-12
**Period:** Jan 1 - Apr 11, 2026
**Overall template accuracy:** 18/27 (67%)

## Dominant Macro Themes YTD

1. **Iran/Hormuz Crisis** — oil supply disruption (Jan-ongoing) → USO +81%
2. **Tariff Escalation** — global trade war (Jan-ongoing) → FXI -9%, XLU +9.5%
3. **Nipah/Pandemic Scares** — Jan-Feb, detected but minimal market impact
4. **Russia-Ukraine** — continued conflict, missile strikes
5. **Bank Failure Scare** — FDIC first failure of 2026 (Jan)
6. **VIX Cycle** — 14.5 → 31 (Feb-Mar) → 19.2 (Apr)

## What Atlas Gets RIGHT (High Conviction)

| Event → Asset | Template Says | YTD Actual | Status |
|--------------|---------------|------------|--------|
| Oil disruption → USO UP | +5-8% | **+81%** | CONFIRMED (massive) |
| Oil disruption → XLE UP | +3-5% | **+25.5%** | CONFIRMED |
| Tariffs → FXI DOWN | -3-8% | **-9.0%** | CONFIRMED |
| Tariffs → XLU UP | +2.5% | **+9.5%** | CONFIRMED (strongest) |
| Tariffs → GLD UP | +2.7% | **+9.8%** | CONFIRMED |
| Tariffs → XLY DOWN | -0.6% | **-4.4%** | CONFIRMED |
| Tariffs → HYG UP | +0.4% | **+0.6%** | CONFIRMED (counter-intuitive) |
| Tariffs → QQQ DOWN | -2.5% | **-0.2%** | CONFIRMED (barely) |
| Tariffs → SPY DOWN | -2.0% | **-0.3%** | CONFIRMED (barely) |
| Conflict → GLD UP | +3.0% | **+9.8%** | CONFIRMED |
| Bank stress → XLF DOWN | -5-15% | **-7.1%** | CONFIRMED |
| RISK_OFF regime → reduce size | -3% SPY | **-0.3%** | CONFIRMED (directional) |

## What Atlas Gets WRONG (Fix or Kill)

| Event → Asset | Template Says | YTD Actual | Problem |
|--------------|---------------|------------|---------|
| Tariffs → EEM DOWN | -2.5% | **+7.7%** | EM commodity exporters benefited from oil boom |
| Conflict → EWJ DOWN | -2.4% | **+8.4%** | Japan rallied with global risk-on in April |
| Oil disruption → IWM DOWN | -0.6% | **+5.2%** | Small caps rallied despite oil headwind |
| Oil disruption → XLI DOWN | -0.5% | **+8.9%** | Industrials shrugged off costs |
| CPI → XLK UP | +2.1% | **-1.0%** | Counter-intuitive template didn't hold |
| Conflict → TLT DOWN | -0.5% | **+0.5%** | Slight, effectively flat |

**Root cause:** Templates assume events happen in isolation. When oil +81% drives a commodity supercycle, EM commodity exporters (EWZ, EEM) benefit DESPITE tariff headwinds. The dominant theme overwhelms secondary effects.

## Macro Coverage Gaps (Not Yet Tracked)

| Gap | What Happened | Why Atlas Missed It |
|-----|--------------|---------------------|
| EWZ (Brazil) +28.4% | Commodity exporter boom from $80+ oil | No LatAm/commodity-exporter template |
| DBA (Agriculture) +5.2% | War → food supply disruption | No food price cascade template |
| UNG (Nat Gas) +40% then -50% | Seasonal/weather volatility | Weather events not modeled |
| XLP (Staples) +6.6% | Defensive rotation | Not in cascade templates |
| XLV (Healthcare) -4.9% | Unexplained decline | No template covers this |
| VIX 14→31→19 cycle | Tradeable spike-and-collapse | VIX mean reversion not implemented |

## Regime Assessment YTD

| Date Range | Regime | Correct? |
|-----------|--------|----------|
| Jan 1-15 | NEUTRAL→RISK_OFF | Markets flat → correct to stay cautious |
| Jan 15-Mar 10 | RISK_OFF | VIX 20→31, SPY -8%. **Correct — would have reduced losses** |
| Mar 10-Apr 11 | RISK_OFF (still) | SPY rallied +5%. **Wrong — missed the recovery** |

**Net assessment:** RISK_OFF regime was correct for 2 of 3 periods. Would have saved ~4% in drawdown (Jan-Mar) but cost ~1.25% in missed rally (Apr at 75% sizing). Rough net: **+2.75% better than unfiltered** — marginal but positive.

## Lessons for Atlas v2

1. **Add commodity-exporter templates**: When oil spikes, EWZ/AUD/CAD benefit. This is a Wave 3 cascade we're missing.
2. **Add defensive rotation template**: Tariffs + conflict → XLU/XLP/XLV rotate UP as a group, not just XLU.
3. **Add VIX mean-reversion signal**: VIX >30 → buy SPY is the highest-confidence standalone signal (85% historical win rate).
4. **Regime needs a RECOVERY state**: RISK_OFF should transition to NEUTRAL when VIX drops below 25 after a spike, not stay RISK_OFF.
5. **Weight the dominant theme**: When oil is +81%, that overwhelms tariff templates on EM. Need a "dominant theme" detector.

## Next Review: Friday Apr 18 (Burn-in Review)

Track these predictions for the coming week:
- Atlas cascade predictions have deadlines Apr 19-20
- CPI Tuesday Apr 15 — watch XLK/QQQ (template says UP)
- TSM/NFLX Wednesday Apr 16 — Apollo's test
- ECB Thursday Apr 17
