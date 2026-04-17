# Label-First Research — Phase 2C Findings (2026-04-16)

## Phase 2C: hour-of-day session edge for ES, NQ, GLD on 1H bars

Methodology: for each (instrument, hour-of-day) cell, measure forward-N-bar OCO outcome (target +1.0 ATR / stop -0.5 ATR / 4-bar hold). Walk-forward 6 chronological folds. Robust = ≥4/6 folds positive AND ≥50 trades.

## TIER S — GLD afternoon long (the standout finding)

**Strongest finding of the entire research project.** All 4 hours pass walk-forward with 6/6 folds positive.

| Hour UTC | NY time | n trades | exp ATR | PF | Win rate | t-stat | Folds positive |
|---|---|---|---|---|---|---|---|
| 17 | 1pm | 717 | +0.098 | 1.33 | 40.7% | **3.6** | 6/6 |
| 18 | 2pm | 718 | **+0.171** | **1.63** | 45.4% | **6.2** | 6/6 |
| 19 | 3pm | 718 | **+0.202** | **1.77** | 47.2% | **7.3** | 6/6 |
| 20 | 4pm (close) | 248 | **+0.296** | **2.28** | 53.2% | 6.2 | 6/6 |

**Productionized as `forge/gld_pm_long/` (Phase 2 deliverable).** Backtest of the runner's own implementation (with no-overlap one-trade-at-a-time logic): **PF 1.73 over 530 trades / 2 years**.

## TIER 1 — NQ overnight long

| Hour UTC | NY time | n | exp ATR | PF | Folds positive |
|---|---|---|---|---|---|
| 21 | 5pm (post-close) | 238 | +0.062 | 1.29 | 5/6 |
| 22 | 6pm | 358 | +0.036 | 1.17 | 5/6 |
| 20 | 4pm (close) | 588 | +0.057 | **1.25** | **6/6** |
| 0 | 8pm (Asian open) | 592 | +0.036 | 1.20 | 5/6 |

Modest but real overnight long bias. Could be productionized as a multi-hour rotating long-only NQ system. Sample size strong, t-stats lower than GLD but positive.

## TIER 2 — ES overnight long

| Hour UTC | NY time | n | exp ATR | PF | Folds positive |
|---|---|---|---|---|---|
| 0 | 8pm (Asian) | 592 | +0.045 | 1.26 | 5/6 |
| 20 | 4pm (close) | 590 | +0.042 | 1.18 | 4/6 |
| 3 | 11pm | 592 | +0.028 | 1.12 | 5/6 |

Same overnight bias as NQ but weaker. Likely captures same systemic mechanism.

## Confirmed nulls

### Short side: weak across all instruments
- ES short: zero hours pass robust gate
- NQ short: only hour 19 (PF 0.98, weak)
- GLD short: hour 19 only (PF 1.08, marginal)

The market's short-side intraday edge is much weaker than long-side at 1H resolution. Long-only strategies dominate this dataset.

## Cross-strategy correlation outlook

| Strategy | Asset class | Cadence | Direction | Correlation w/ #1 |
|---|---|---|---|---|
| #1 GBPUSD wick | FX major | ~13 trades/year | Long | self |
| #2 GLD PM long | Commodity ETF | ~250 trades/year | Long | LOW |
| #3 NQ overnight | Tech futures | ~250-600 trades/year | Long | MEDIUM (both equity-correlated risk) |

GBPUSD and GLD are nearly uncorrelated (FX vs commodity, daily vs intraday). Adding NQ overnight as #3 would be EQUITY-correlated with whatever Argus eventually trades.

## Combined fleet potential
If all three productionize successfully (paper → real):
- Combined trade count: ~500-800/year
- Diversification: 3 asset classes (FX, commodity, equity)
- Funding gate (60 trades / PF ≥ 1.30 / DD ≤ 8%) achievable per-system in 2-12 months

## Phase 2 final decision
- ✅ #1 GBPUSD wick: productionized as `forge/wick_gbpusd/`
- ✅ #2 GLD PM long: productionized as `forge/gld_pm_long/`
- ⏸️ #3 NQ overnight: deferred — sample-size strong but expectancy modest. Wire after #1 and #2 prove out in paper.
- ⏸️ ES short / GLD short: not actionable, no walk-forward signal

## Files
- `phase2c_ES_long_hours.csv`, `phase2c_ES_short_hours.csv`
- `phase2c_NQ_long_hours.csv`, `phase2c_NQ_short_hours.csv`
- `phase2c_GLD_long_hours.csv`, `phase2c_GLD_short_hours.csv`
- `forge/wick_gbpusd/`, `forge/gld_pm_long/` — production runners with cohort tagging
