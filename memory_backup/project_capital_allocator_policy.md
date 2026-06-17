---
name: Capital Allocator Policy — tier rules + concentration caps + ladder
description: Defines how money moves between strategies. Tier → max allocation, promotion criteria, demotion triggers, concentration caps. The bridge from strategy performance to real exposure. User decisions locked 2026-04-26.
type: project
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
## Master frame

> Argus is a capital allocator over competing strategy hypotheses. Many strategies may compete; few receive capital; one starts real; capital flows only to evidence.

Strategies are not the product. Capital decisions are. This document is the rulebook for those decisions.

## Tier table — capital allowed per state

| Tier | State | Capital allowed | Conditions |
|---|---|---|---|
| Tier 0 | DISCOVERY / KEEP-PAPER / OBSERVE | $0 paper only | Default state for everything new |
| Tier 1 | WINNER-CANDIDATE | $0, paper only — closer review | Score ≥ 65 sustained 21d, n ≥ 15 |
| Tier 2 | REAL-CANDIDATE | Eligible for real-money gate | Score ≥ 80, n ≥ 30, PF ≥ 1.30 over 60d, runtime_clean 21d |
| Tier 3 | REAL-PILOT | $500 - $2,000 | Cleared 20-point readiness gate, single strategy only at first |
| Tier 4 | SCALE-1 | 5% of total capital | 30+ days clean real-money operation, real PF ≥ 0.7 × paper PF |
| Tier 5 | SCALE-2 | 10-15% of total capital | 60+ days clean real-money, real PF holds, no catastrophic incidents |
| Tier 6 | OFFENSIVE | 25-50% of capital, time-limited | Only after 90+ days at SCALE-2, regime fit ≥ 0.85, requires explicit user sign-off |
| Lockout | QUARANTINE / KILL | $0 | Implementation broken or evidence reversed |

Tier 0-2 = paper. Tier 3+ = real money. Tier 6 is **time-limited** (max 30 days per activation, requires re-justification).

## Concentration caps (locked 2026-04-26)

- **Single strategy: 25% of capital normal max.** No strategy gets more than a quarter of total real capital under standard operation.
- **Single strategy: 50% in OFFENSIVE mode max.** Only with 90+ days proof and explicit sign-off. Time-limited.
- **Never above 50% on a single strategy regardless of evidence.** Concentration above this is gambling, not investing.
- **No more than 3 strategies in Tier 4+ at one time** during the first 6 months of real-money operation. Forces focus.

## Capital ladder (locked 2026-04-26)

The ladder applies to the **REAL account balance**, which is separate from the paper account.

Mental model:
- **Paper account ($11,815)** = QA environment. Balance fluctuates as part of testing; it's not the capital ladder. We can reset it freely. Don't conflate paper drawdowns with real-money risk.
- **Real account (currently $50)** = production destination. The $50 is just to keep the account open. The ladder below is what real money looks like as strategies are promoted from paper → real.

```
$50 (current — minimum balance, account-keeper only, no trading expected)
  ↓ first strategy passes readiness gate after paper proof
$500-1,000 (first real pilot — single strategy)
  ↓ 30 days clean real-money operation
$2,000 (Tier 3 ceiling for one-strategy pilot)
  ↓ second strategy passes gate, 60 days clean on first
$5,000 (Tier 4 — multi-strategy real, allocation per cluster caps)
  ↓ 90 days clean, ≥2 strategies real
$10,000 (Tier 4 — original "prove edge then fund $10K" goal met)
  ↓ 6+ months sustained edge across multiple strategies
$25,000 (Tier 5 expansion)
  ↓ 12+ months
$100,000 (mature system, multiple proven strategies)
```

**Each rung is a separate decision, not automatic.** Capital does not auto-scale. User explicitly funds the real account when promoting a strategy. The capital flows TO strategies that earned it, not equally across all strategies.

**End state:** every strategy is either real-promoted (in real account, earning capital) or killed (stopped, archived). No permanent paper-only graveyard. The paper account exists only to validate new strategies before they're allowed to migrate.

## Promotion rules

A strategy moves UP a tier ONLY when:
1. Its `evidence_promotion_ladder` stage matches or exceeds the target tier (REAL-PILOT requires "Paper-Live Valid" + WINNER-CANDIDATE achieved on scorecard)
2. Its scorecard final_score has been at the required threshold for the **minimum hold period** of the target tier (21d for WINNER-CANDIDATE, 30d for SCALE-1, 60d for SCALE-2, 90d for OFFENSIVE)
3. The promotion is logged in `capital_promotion_ledger.jsonl` with evidence + approver
4. For real-money promotions: readiness gate signed off, no waivers active that block the promotion specifically

## Demotion rules (auto, no ceremony)

A strategy moves DOWN one or more tiers IMMEDIATELY when:
- DEGRADED operational_maturity for 21+ consecutive days → demote one tier
- 2+ execution-path bugs in 30 days → demote to QUARANTINE
- Scorecard final_score drops below 50 for 14 consecutive days → demote to KEEP-PAPER
- Real-money strategy hits **strategy-level kill trigger** (per `project_kill_pause_engine.md`) → demote to paper, investigation required before promotion attempt
- Reconciliation drift unresolved within 24h → halt strategy, demote to QUARANTINE

Demotion is **automatic and logged**. Re-promotion requires a fresh evidence cycle.

## Anti-promotion guards

These conditions **block** promotion regardless of score:
- Cluster exposure cap would be breached by promoting (per `project_cluster_exposure_model.md`)
- Real-money readiness gate has any unresolved BLOCKS-FLEET liability
- Strategy is in QUARANTINE
- vix_short asking to go real with < 90 days paper proof (per `project_pre_freeze_coverage_gaps_20260426.md` — special handling)
- Total fleet drawdown > -10% in last 14 days (no promotions during stress)

## Allocation mechanism

Allocation is **manual until 90 days real-money evidence**, per user decision 2026-04-26:
- Bot **recommends** allocation changes via scorecard output
- User **approves** before any change takes effect
- After 90 days clean real evidence: semi-auto within tier caps (bot can rotate within Tier 4 strategies, can't promote up tiers)
- **Never fully auto** under current scope

## What this policy does NOT do

- **Does not generate orders.** This is a tier+rules document, not execution code.
- **Does not auto-promote without user sign-off.**
- **Does not override the strategy freeze.** Adding new strategies to the allocator pool requires they exist before 5/31.
- **Does not relax the readiness gate.** Tier 3 entry requires gate clearance regardless of scorecard.

## How to apply this memory

**Why:** without this, "good strategy" silently became "deserves capital." The locked tier table and concentration caps prevent that drift.

**How to apply:**
- Every capital decision references this policy + the strategy's scorecard + the ladder stage.
- Promotion conversations should produce a `capital_promotion_ledger.jsonl` entry, not a verbal commitment.
- If a tier rule conflicts with a strategy's situation: rule wins. Adjust the rule explicitly via memory amendment if it's wrong; don't bend it case-by-case.
