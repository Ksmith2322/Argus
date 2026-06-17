---
name: Evidence Promotion Ladder — explicit maturity path from research to real money
description: 9-stage progression every strategy must climb. Bridges research → paper → real money via concrete evidence requirements at each step. Prevents the label blur where "approved", "live", and "real" smuggle different meanings.
type: project
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
## Why this exists

Without an explicit ladder, "WINNER" and "live" and "approved" all blur together. That blur is where capital decisions go wrong. This memory makes the maturity path concrete: a strategy can only be referred to by the highest stage it has actually passed.

## The 9 stages

| # | Stage | What it means | Required evidence |
|---|---|---|---|
| 1 | **Backtest Valid** | Logic produces results on historical data without obvious flaws | No lookahead bias, fills modeled with friction (slippage + commission), `experiment_valid` flag set on every recorded trade. PF ≥ 1.0 in-sample (just as a sanity floor). |
| 2 | **Walk-Forward Valid** | Survives out-of-sample testing | Train on segment A, freeze parameters, evaluate on segment B. PF ≥ 1.0 OOS. Repeat across 3+ folds. Result stored in `strategy_confidence/<name>.json`. |
| 3 | **Signal-Only Valid** | Produces expected live signals without execution | Run with `--signal-only` flag for 5+ trading days. Signal frequency within ±50% of replay expectation. No errors in signal generation path. |
| 4 | **Paper-Live Valid** | Executes cleanly in IBKR paper | Live IBKR-paper for 7+ trading days. Fills land. Stops/targets execute correctly. Reconciliation clean. canonical_fills.jsonl matches per-strategy CSV. No `REAL_ENTRY FAILED` patterns. |
| 5 | **KEEP-PAPER** | Behavior validated, not enough sample yet | n=5-15 valid trades, PF in 0.95-1.20 range, operational_maturity HEALTHY, behavior matches design |
| 6 | **WINNER-CANDIDATE** | Promising, more proof needed | n=15-30 valid trades, PF ≥ 1.20, operational_maturity HEALTHY 14+ days, no execution bugs |
| 7 | **REAL-CANDIDATE** | Eligible for real-money gate review | n ≥ 30 valid trades over 60+ days, PF ≥ 1.30, operational_maturity HEALTHY 21+ days, `p_expectancy_positive` ≥ 0.75, has cleared all 4 prior validity stages |
| 8 | **Real Pilot** | Single strategy on real money, tiny allocation | Cleared real-money readiness gate (per `project_real_money_readiness_gate_20260531.md`). Allocated $500-2000. risk_pct 0.5%. Daily review of every fill for 5 days. 30-day watch. |
| 9 | **Portfolio Member** | Survived the real pilot, can scale | 30+ days of real-money trading. Real PF ≥ paper PF × 0.7 (slippage haircut). No catastrophic incidents. Approved for capital scaling and inclusion in multi-strategy real fleet. |

## Movement rules

**Forward-only, with one exception.** A strategy can move down (demote) at any time if evidence reverses. A strategy can only move up by meeting the explicit criteria for the next stage.

**You cannot skip stages.** Examples:
- A strategy with strong backtest but no walk-forward = stage 1 only. Cannot claim KEEP-PAPER until walk-forward is run.
- A strategy with 50 paper trades but operational_maturity DEGRADED for a week = cannot become WINNER-CANDIDATE until 14 healthy days accumulate.
- A REAL-CANDIDATE that hits the readiness gate but has an open waiver on item 13 (per-instrument cap) = held at REAL-CANDIDATE, not promoted to Real Pilot, until waiver clears or item is GREEN.

**Demotion triggers (move DOWN one or more stages):**
- DEGRADED operational_maturity for 21+ consecutive days
- 2+ execution-path bugs in 30 days
- PF degrades below the threshold of current stage for 14 consecutive days
- Failed reconciliation that wasn't traced to root cause within 24h

## Current fleet placement (2026-04-26 snapshot)

Most strategies are at stage 4 (Paper-Live Valid) but haven't accumulated enough sample yet to claim stage 5. The MATURE-tier candidate from the test coverage matrix (vix_intraday, n=18, PF 1.45) is approaching WINNER-CANDIDATE but still 2 stages away from REAL-CANDIDATE.

| Stage | Strategies (4/26) |
|---|---|
| 1 — Backtest Valid | All forge strategies have at least this. atlas/themis informational. |
| 2 — Walk-Forward Valid | tori (partial), fomc_drift (per spec, not stress-tested across regimes). Most others NOT. |
| 3 — Signal-Only Valid | Most forge strategies have signal_only fallback path, but few have run a clean 5-day signal-only window. |
| 4 — Paper-Live Valid | argus pairs (partial), forge_vix_intraday, forge_spy_mean_rev, forge_multi_orb, forge_jpy_pm_short, forge_nq_overnight, forge_gld_pm_long, forge_nq_london_close. ~8 of 22. |
| 5 — KEEP-PAPER | TBD at 5/1 review |
| 6 — WINNER-CANDIDATE | forge_vix_intraday is the closest candidate at 5/1 review |
| 7 — REAL-CANDIDATE | None yet |
| 8 — Real Pilot | None yet (post-5/31) |
| 9 — Portfolio Member | None yet |

## How this interacts with the 5/31 freeze

After 5/31, **no new strategies enter at stage 1**. Only existing strategies progress along the ladder. Demotions still allowed. New strategies queued for "post-go-live" don't enter the ladder until they're greenlit (likely Q3 2026 at earliest).

The 3 Week-2 additions (zn_momentum, vix_short, spy_tlt_pair) need to clear stages 1-2 before going live in paper, and stage 4 by 5/31 to be in any verdict tier above OBSERVE.

## How to apply this memory

**Why:** the ladder is the bridge between research and capital. Without it, labels drift and bad capital decisions hide.

**How to apply:**
- When asked "what stage is X?": cite the ladder, not the audit.
- When verdicting at 5/1 / 5/31: each strategy's verdict label must be consistent with its ladder stage. A strategy at stage 4 (Paper-Live Valid) cannot get a REAL-CANDIDATE verdict; the math doesn't work.
- When promoting: walk the ladder. If a strategy claims WINNER-CANDIDATE, verify it has Paper-Live Valid + 15-30 valid trades + PF ≥ 1.20.
- The Capital Promotion Ledger (`project_capital_promotion_ledger.md`) records ladder transitions per strategy.
