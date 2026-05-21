---
name: $100K milestone goal & go-live roadmap
description: User's 5-phase roadmap from $1,500 (3x$500) paper to $100K real money in <1 year. $500/mo injection plan. Dashboard readiness tracker at 2/29 checks.
type: project
---

**Goal:** 3 coins x $500 paper -> all coins to $2K -> $5K -> portfolio to $100K in <1 year.

**Why:** Proving ground before real capital. User will inject $500/month real money once paper trading proves consistent edge.

**How to apply:**
- Every strategy change evaluated against: "does this move PF toward >1.2 and daily compound toward >0.6%?"
- Dashboard readiness tracker (29 checks across 5 phases) is the go/no-go scorecard
- User explicitly wants ALL 3 coins profitable before going live
- $500/month capital injection planned once live — dashboard projections updated to include this
- RANGE removed from regime block list (2026-03-16) to let trades flow via score penalty instead

**5-Phase Roadmap (dashboard tracker):**

Phase 1 — Strategy Validation: BT PF>=1.2, WR>=50%, expectancy>$0, MFE/MAE, exit optimization, BTC/SOL backtests
Phase 2 — Paper Proof: 50+ trades/coin, live PF>=1.1, <5% drawdown, <5 consec losses, governor retrain, 2wk consistency
Phase 3 — Financial Milestones: all 3 coins $500->$2K->$5K, portfolio $15K
Phase 4 — $100K Velocity: compound rate + $500/mo injection, projection <12 months
Phase 5 — Go-Live Infra: Coinbase API, real adapter test, kill switch, recovery test, Discord alerts

**Current status (2026-03-16): 2/29 checks passed (6.9%)**
- Passed: Recovery/Reconciliation tested, Discord alerts working
- Blockers: PF 0.88 (need 1.2), 0 live trades, MFE/MAE still zeros in completed runs

**Latest backtest results (2026-03-16):**
- blockOFF (efee8580): PF=0.88, WR=44.8%, expectancy=-$0.019, 29 trades, DD=0.70%
- with_block (f1116a60): PF=0.83, WR=41.9%, expectancy=-$0.027, 31 trades, DD=0.76%
- Improvement vs old baseline (PF=0.74, WR=25%) but still net negative

**Strategy tuning applied 2026-03-16:**
- CONFLUENCE_MIN_SCORE: 85, MAX_HOLD_SECONDS: 1800, USE_TRENDLINES: true
- ML_GOVERNOR_MODE: GATE@0.35, COMPOUND_SIZE_PCT: 0.10
- TAKE_PROFIT_PCT: 0.03->0.015, EXIT_BREAKEVEN_TRIGGER_PCT: 0.003
- REGIME_ENTRY_BLOCK_LIST: TREND_DOWN only (RANGE removed)
- MFE/MAE bug fixed (state reset before snapshot capture) — awaiting first run with fix

**$100K math with $500/mo injection (3 coins combined):**
- At 0.5%/day compound + $500/mo: ~20 months
- At 0.6%/day compound + $500/mo: ~16 months
- At 1.0%/day compound + $500/mo: ~12 months
- At 1.5%/day compound + $500/mo: ~8 months
- Dashboard updated to show compound+injection curves

**Critical gaps identified (2026-03-16 assessment):**
1. Strategy edge is #1 blocker — PF<1.0 means losing money, no timeline applies
2. Walk-forward validation missing — all backtests use same 30-day window (in-sample)
3. BTC/SOL backtests not run — no data on cross-coin performance
4. Correlation risk across 3 coins not quantified (ETH/BTC/SOL ~0.85 correlated)
5. MFE/MAE data needed to know if entries have untapped profit or entries themselves are bad
6. Backtest throughput bottleneck — 4.5hr/run, most screening runs FAILED on artifact drift
