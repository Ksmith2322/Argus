---
name: Dormant strategy inventory — 2026-04-22
description: Scan of already-built but not-live research in the codebase. fomc_drift is the standout (PF 1.58 over 215 trades). Tier-ranked opportunities.
type: project
originSessionId: 81afc97b-10bf-49fd-ba79-e5a89a7f400b
---
Triggered by user question 2026-04-22: "any other edge like this we're not doing?"

## Tier 1 — dormant research with positive backtests (activate post-5/1)

| File | N | Win% | PF | Per-trade | Status |
|---|---:|---:|---:|---:|---|
| `forge/fomc_drift.py` | 215 | 55% | **1.58** | +0.22% | **High-confidence edge, just needs a runner wrapper** |
| `forge/tom_international.py` | 1,249 | 57% | 1.31 | +0.003% | Edge per-trade likely killed by commissions — needs slippage model before activating |
| `forge/form4_cluster.py` | 56 | 43% | 1.02 | +0.26% | Sample too small, marginal |
| `forge/fomc_drift_deep.py` | — | — | — | — | 344 lines, no backtest csv. Deeper fomc variant |
| `forge/options_flow.py` | — | — | — | — | 335 lines, no backtest csv. Unusual-options-activity scanner |
| `forge/macro_strategies.py` | — | — | — | — | 971 lines, no backtest csv. Macro overlay set |
| `forge/drawdown_recovery.py` | — | — | — | — | 447 lines, no backtest csv. Post-drawdown mean reversion |

**~3,300 lines of strategy code idle. fomc_drift is the single highest-ROI activation.**

## Tier 2 — signal generators needing executor (event_trader design)

See `project_event_trader_architecture_20260422.md`.
- atlas: classifies news (LOG_ONLY), no execution path
- themis: scans Congressional trades, no execution path
- Future: news-driven dynamic shortlist

## Tier 3 — archetypes missing entirely (new research)

- **Short-side versions of existing long strategies** — most of the fleet is long-biased. Would need fresh backtests.
- **Vol term structure** — VIX vs VIX3M divergence (related to but distinct from forge.vix_revert)
- **Social sentiment** — atlas master plan referenced IFTTT Twitter relay for @DeItaone / @FirstSquawk, never built
- **Cross-asset ratio signals** — gold/silver divergence, copper/gold as macro tell
- **Earnings straddles / defined-risk options** — no options strategies at all in current fleet
- **Post-IPO drift** — well-documented retail edge, no coverage
- **Short-squeeze / high short interest + catalyst** — no coverage

## Recommended activation order (post-5/1)

1. **fomc_drift runner** — 2hr job, biggest single opportunity (PF 1.58, 215-trade sample)
2. Validate atlas regime signal against fleet PnL (part of 5/1 deliverables)
3. Build event_trader layer — unlocks atlas + themis + future news
4. Short-side variants of existing validated longs
5. tom_international after slippage analysis
6. Everything else

## How to apply

- When user asks about expanding the fleet or adding strategies, reach for Tier 1 first (already-backtested → fastest ROI).
- Don't build new strategies in Tier 3 until Tier 1 and 2 are consumed — already-done research is always cheaper than new research.
- Before activating any Tier 1 strategy, re-run its backtest on fresh data to confirm edge hasn't decayed (backtest files dated — some may be stale).
- form4_cluster with only 56 trades is NOT ready — needs more data before live activation.
