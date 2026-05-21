---
name: Argus Bot Master Breakdown — 2026-04-26 snapshot
description: Single-document state-of-the-bot. Architecture, current strategies, infrastructure, what's in progress, what's queued, end-state at 5/31, and realistic vs aspirational performance goals.
type: project
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
# 0. Master objective (single statement)

> **Argus is not a strategy bot. Argus is a capital allocator over competing strategy hypotheses.**
>
> **By 5/31, identify the smallest number of strategies with clean evidence, kill or quarantine the weak ones, freeze the research surface, and allow only one strategy through a tightly controlled real-money pilot.**

The wide-portfolio approach is how we DISCOVER what works. The narrow real-money pilot is how we PROVE it. Capital flows only to evidence. Loyalty is to evidence, not to instruments.

## The 10-layer architectural stack

```
1. Strategy Logic ............ generates signals (22 active + 3 incoming)
2. Execution Layer ........... helio/ibkr_execution.py + ibkr_executor.py + signal_executor.py
3. Broker Truth Layer ........ argus_flow/ops/risk_oversight.py + broker_state.json
4. Reconciliation Layer ...... canonical_reconcile.json + per-pair reconcile in argus
5. Evidence Layer ............ test_coverage_matrix + scorecard fields
6. Strategy Scorecard ........ project_strategy_scorecard.md (P1)
7. Exposure Governor ......... project_cluster_exposure_model.md (P0, Week 3)
8. Capital Allocator ......... project_capital_allocator_policy.md (P0, locked tiers)
9. Kill/Pause Engine ......... project_kill_pause_engine.md (P0, Week 3 implementation)
10. Governance Ledger ........ project_capital_promotion_ledger.md (active)
```

**Layers 1-5 partially exist.** **Layers 6-9 are policy-defined now, code-implementation in Week 3.** **Layer 10 active.** Real-money go-live blocked until layers 6-9 implementations land + drills pass.

# 1. Architecture (the layers)

```
┌────────────────────────────────────────────────────────────────┐
│  IBKR Paper Account (DUP472829, port 7497, $11,815)            │
└────────────────────────────────────────────────────────────────┘
                              ▲ ib_insync
┌────────────────────────────────────────────────────────────────┐
│  Execution helpers                                             │
│   • helio/ibkr_execution.py  (forge runners — wake-and-sleep)  │
│   • helio/ibkr_executor.py   (Greek family — apollo/hermes/    │
│                               titan/ares — separate older)     │
│   • helio/signal_executor.py (scanner-style)                   │
└────────────────────────────────────────────────────────────────┘
                              ▲
┌────────────────────────────────────────────────────────────────┐
│  Runners (3 families, 22 active strategies as of 4/26)         │
│   • Argus       (FX, single process, 3 pairs)                  │
│   • Forge       (per-strategy processes, 17 active)            │
│   • Greek       (apollo/hermes/titan/ares scanners)            │
│   • atlas + themis (signal-only, no trades)                    │
└────────────────────────────────────────────────────────────────┘
                              ▲
┌────────────────────────────────────────────────────────────────┐
│  Risk + sizing infrastructure                                  │
│   • helio/fleet_sizing.py     (broker-equity-dynamic risk)     │
│   • argus_flow/configs/fleet_sizing.json v6 (anchor caps)      │
│   • argus_flow/ops/risk_oversight.py (broker_truth)            │
│   • portfolio_risk_state.json (drawdown_pause)                 │
│   • portfolio_guard.json (concentration / family caps)         │
└────────────────────────────────────────────────────────────────┘
                              ▲
┌────────────────────────────────────────────────────────────────┐
│  Daemons + supervision                                         │
│   • argus_flow.ops.managed_truth_loop (3-min refresh)          │
│   • helio.fleet_monitor (process health)                       │
│   • Windows scheduled tasks: ArgusCohortReport, ArgusWatchdog, │
│     ArgusGitBackup, ArgusUSBBackup, ArgusGldPmLoop, etc        │
└────────────────────────────────────────────────────────────────┘
                              ▲
┌────────────────────────────────────────────────────────────────┐
│  Observability                                                 │
│   • ops/dashboard.py (port 8080, 25+ /api/* endpoints)         │
│   • argus_flow/logs/canonical_fills.jsonl (audit trail)        │
│   • argus_flow/logs/fleet_status.json (consolidated health)    │
│   • Discord webhooks (alerts, digests)                         │
│   • ops/full_audit.py (10-lens audit, dated reports)           │
└────────────────────────────────────────────────────────────────┘
```

# 2. Current strategy inventory (4/26 snapshot)

| # | strategy | family | instrument(s) | status | live_30d | direction | tier |
|---|---|---|---|---|---|---|---|
| 1 | argus_usdjpy | argus | USDJPY | active | 2 | both | A |
| 2 | argus_gbpusd | argus | GBPUSD | active (gates loosened 4/26) | 0 | both | A |
| 3 | argus_cadjpy | argus | CADJPY | watcher (gates loosened 4/26) | 0 | both | watcher |
| 4 | forge_gld_pm_long | forge | GLD | active (weekend-guard added 4/26) | 1 | long | unproven |
| 5 | forge_jpy_pm_short | forge | USDJPY+CADJPY | active | 3 | short | unproven |
| 6 | forge_nq_overnight | forge | MNQ | active | 2 | long | unproven |
| 7 | forge_spy_mean_rev | forge | SPY | active (v2 trend filter being validated) | 29 | both | unproven |
| 8 | forge_vix_intraday | forge | UVXY | active — strongest signal | 18 | both | candidate |
| 9 | forge_nq_london_close | forge | MNQ | active | 2 | short | unproven |
| 10 | forge_aud_asian_breakout | forge | AUDUSD | active (band loosened 4/26) | 0 | both | unproven |
| 11 | forge_multi_orb | forge | SPY/QQQ/IWM/GLD | active — losing | 48 | both | unproven |
| 12 | forge_fomc_drift | forge | SPY (event) | dormant until 4/29 FOMC | 0 | long | unproven |
| 13 | forge_tom_international | forge | EEM,EWJ,VGK,EFA,FXI,INDA (6 — expanded 4/26) | dormant until 4/29 | 0 | long | unproven |
| 14 | forge_wick_gbpusd | forge | GBPUSD | low-frequency by design (~13/yr) | 0 | long | unproven |
| 15 | forge_vix_revert | forge | SPY (VIX trigger) | dormant until VIX>25 | 0 | long | unproven |
| 16 | forge_mamba | forge | MYM | active (RESEARCH_ONLY removed 4/26) | 0 | both | research |
| 17 | forge_tori | forge | MYM | active (RESEARCH_ONLY removed 4/26) | 0 | both | research |
| 18 | forge_cuebanks | forge | MYM | active (RESEARCH_ONLY removed 4/26) | 0 | both | research |
| 19 | forge_rebalance | forge | S&P add stocks | dormant — no events in window | 0 | long | unproven |
| 20 | forge_gdx_gld | forge | GDX/GLD pair | flapping (TWS disconnects) | 0 | both | candidate |
| 21 | apollo | greek | ER stocks (113) | scanning, post-ER candidates rare | 0 | both | research_only |
| 22 | hermes | greek | gap-fill stocks | active (--execute fix 4/26) | 0 | both | unproven |
| 23 | titan | greek | trend stocks (10+) | active, scanner refresh tonight | 0 (live position 1) | long | unproven |
| 24 | ares | greek | sector ETFs | monthly (next rebalance day) | 0 | long | unproven |
| 25 | forge_atlas | forge | regime classifier | signal-only, no trades | n/a | n/a | n/a |
| 26 | forge_themis | forge | congressional tracker | signal-only (401 fixed 4/26) | n/a | n/a | n/a |

**Live 30d total:** 105 trades. **Strategies that have traded in 90d:** 8 of 23 (excluding atlas/themis/ares).

# 3. What's in progress (this week)

| Item | Status | Owner | Done by |
|---|---|---|---|
| Silent-15 wake-up (gates loosened, RESEARCH_ONLY removed) | DEPLOYED 4/26 | claude | 4/26 |
| USDJPY orphan position closed + RECON_DRIFT cleared | DONE 4/26 | claude | 4/26 |
| Hermes --execute flag fix | DEPLOYED 4/26 | claude | 4/26 |
| tom_international expanded 3→6 ETFs | DEPLOYED 4/26 | claude | 4/26 |
| Cohort report ps1 fix (no longer aborts on lock contention) | DEPLOYED 4/25 | claude | 4/25 |
| H1 scheduled tasks switched to S4U logon | DEPLOYED 4/26 | claude | 4/26 |
| Monday 4/27 first-real-fills validation | PENDING | user | 4/27 |
| 5/1 review ceremony | PENDING | user | 5/1 |
| **3 new strategies (bond futures, short-vol, SPY/TLT pair)** | APPROVED, build Week 2 | tbd | 5/9 |

# 4. What's queued (5/31 freeze and after)

## Pre-freeze (5/3 → 5/31)
- Build forge_zn_momentum (Week 2)
- Build forge_vix_short (Week 2)
- Build forge_spy_tlt_pair (Week 2)
- Risk infrastructure drills (Week 3): kill switch, daily-loss circuit breaker, position caps, manual override
- Real-money readiness gate sign-off prep (Week 4)
- Final cull + freeze (Week 5)

## Post-freeze (after 5/31)
- LLM regime classifier overlay (per `project_llm_overlay_post_531.md`)
- Phase 2 instrument compatibility sweep (per `project_phase2_compatibility_sweep.md`)
- Real-money pilot: ONE strategy (the 5/1 strongest WINNER), $500-2000 starting, 0.5% risk_pct
- Fleet-wide reconciliation aggregator (orphan detection generalized from per-pair to fleet)
- gdx_gld socket-resilience code (fix the chronic disconnect cycle)

## Deferred indefinitely
- Options strategies (complexity > 5-week budget)
- Crypto re-activation (intentional archive)
- Sub-minute strategies (paper unreliable, retail real-money untenable)
- Dashboard cosmetic features (lean dashboard rule)

# 5. End state at 2026-05-31

By 5/31 close, the bot should look like this:

**Fleet composition:**
- 8-12 strategies in WINNER tier (proven edge, scaled risk)
- 0-3 strategies in REWORK (on a deadline to fix one specific thing)
- 8-12 strategies KILLED (shelved, code preserved, no longer running)
- 3 new strategies (zn_momentum, vix_short, spy_tlt_pair) with 3 weeks of data each, verdicts assigned
- 2 informational classifiers (atlas, themis) untouched

**Net active runners:** ~12-15 (down from 22). Cleaner fleet.

**Infrastructure state:**
- Cohort_report runs cleanly nightly (last_result=0)
- All scheduled tasks last_result=0 for 7 consecutive days
- Zero RECON_DRIFT events in last 14 days
- Risk drills passed (kill switch, daily-loss circuit breaker, position caps)
- Real-money readiness gate: 17+ of 20 GREEN, ≤3 explicit waivers
- Solo operations manual reviewed, gaps closed

**Decision deliverables:**
- `argus_flow/logs/verdict_20260501.json` (5/1 review record)
- `argus_flow/logs/verdict_20260531.json` (5/31 final review record)
- `project_fleet_master_20260531.md` (final snapshot memory)
- Real-money go-live decision: APPROVED / DEFERRED-TO-DATE

# 6. Performance goals — realistic vs aspirational

## Realistic (what the math supports)

For a diversified retail multi-strategy paper portfolio with PF 1.2-1.5 edges and proper sizing:

- **Annual return on equity:** 8-18% gross paper, before any execution friction
- **Max drawdown:** 15-25% peak-to-trough during the year
- **Sharpe ratio:** 0.6-1.0 (anything above 1.5 in retail at this scale is suspect)
- **Win rate:** 45-55% (PF > 1 doesn't require WR > 50%; the math is in payoff ratios)
- **Best month:** ~5-8% gross
- **Worst month:** -3 to -7% gross

**At $11,815 paper anchor → ~$945-2127 paper profit/year.** That's the honest range.

## Aspirational (the user's stated target)

200-300% annual return ($23,630-35,445 profit on $11,815). This requires one of:

- **Concentration**: top 1-2 strategies take 70%+ of risk budget. Increased risk_pct (3-5% per trade) on confirmed edges. Higher peak DD (35-50%) likely.
- **Leverage**: futures-heavy (already partly there with MNQ/MYM). Going deeper on micro futures or adding ZN (which has its own leverage) compounds.
- **Compounding**: a sustained 15% monthly return → 435% annual via compounding. Requires no losing months. Extremely rare for retail.
- **Concentration + leverage**: probably the only realistic path. ~50% drawdown risk in exchange for upside tail.

The diversified-fleet path you're building IS NOT the aspirational path — diversification structurally regresses returns toward portfolio mean. To hit 200%+, you'd need to throw concentration risk at the problem post-5/31, NOT through the wide-portfolio approach. **This is a known tension in the project.** Document; don't pretend the wide-portfolio gets you there.

## Realistic milestones for the next 6 months

| Date | Milestone | Metric |
|---|---|---|
| 5/1 | First formal review | 8+ strategies firing, ≥1 WINNER candidate |
| 5/31 | Strategy freeze + real-money gate | 8-12 WINNERS, real-money go-live decision |
| 6/30 | Real-money pilot, 30 days | $500-2000 live; 1 strategy; per-trade slippage measured; no catastrophic loss |
| 7/31 | Real-money 60 days | 2-3 strategies live; first real-PnL data; refined sizing |
| 9/30 | 90-day post-go-live review | Decide whether to scale capital. If positive: $5K-$10K real. If negative: pause + diagnose. |

## Performance milestones THAT MATTER

In order of importance:

1. **Did one strategy prove edge with n ≥ 30 valid trades and PF ≥ 1.3?** — yes/no. The single-strategy proof is more important than fleet stats.
2. **Did the system survive a real adverse event?** — TWS outage, broker disconnect, drawdown circuit breaker fire. Did it behave per design, not catastrophic?
3. **Is the operator (you) able to run it solo without claude sessions?** — by 5/31 the solo manual must be sufficient; otherwise we have a different problem.
4. **Real-money slippage matches paper assumptions?** — first 30 days of real $$ tells you whether your edge survives execution friction.

If 1-4 are YES at 6/30, the bot is real. If any are NO, we're in iteration mode, not scale mode.

# 7. Known liabilities — classified by blast radius

Each liability is classified as **Blocks Fleet** (no real money until fixed), **Quarantines Strategy** (only the affected strategy can't trade real), or **Monitor Only** (track but no trading impact).

| Liability | Class | Owner | Target |
|---|---|---|---|
| Per-instrument cumulative cap not implemented | **BLOCKS FLEET** | Week 3 | 5/15 |
| Fleet-wide reconciliation aggregator not built | **BLOCKS FLEET** | Week 3 | 5/15 |
| Currency cluster cap not implemented | **BLOCKS FLEET** (if multiple FX strategies stack correlated exposure) | Week 3 | 5/15 |
| Reset-day equity baseline drift (4/23) | **BLOCKS FLEET** | Week 4 | 5/20 |
| gdx_gld chronic socket disconnects | **QUARANTINES gdx_gld** | post-real-money | 6/30 |
| 3 new strategies not yet built (zn_momentum, vix_short, spy_tlt_pair) | Build task — not a liability per se | Week 2 | 5/9 |
| LLM overlay not yet built | **MONITOR ONLY** (post-5/31 work) | post-freeze | n/a |
| gdx_gld data path "review" (was misdiagnosis) | **MONITOR ONLY** | n/a | n/a |
| H1 scheduled tasks logon mode | **DONE 4/26** (S4U applied) | resolved | resolved |

**The classification matters:** one BLOCKS FLEET liability that's not GREEN at 5/28 = real-money go-live defers (or counts against the 3-waiver cap). One QUARANTINES STRATEGY liability = that strategy is held back, fleet can still progress.

**Don't let one broken strategy freeze all progress. Don't let one missing global guardrail let real money pass through unnoticed.**

# 8. How this document gets used

**Why:** single-source state-of-the-bot. Read this first to understand current state, then drill into specific memories for procedures.

**Read this:**
- Before any major decision (5/1 review, 5/31 freeze, real-money go-live)
- When briefing a new claude session in a future month
- Quarterly to check whether stated end-state matches reality

**Update this:**
- After each weekly audit, if state-of-fleet changed materially
- After 5/1 review (update strategy table tiers + statuses)
- After 5/31 freeze (this becomes the "fleet master" document, replace project_fleet_master_*)
- After any major architectural change (new helper, new daemon, new family)

Don't let this document drift. If it gets stale, future sessions get confused about what reality looks like.
