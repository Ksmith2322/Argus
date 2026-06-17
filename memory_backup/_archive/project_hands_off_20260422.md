---
name: Hands-off collection window — 2026-04-22 to 2026-05-01
description: User stepping back for ~9 days to let fleet collect post-clamp trades. Return target 2026-05-01 (Fri). Minimal intervention during window.
type: project
originSessionId: 81afc97b-10bf-49fd-ba79-e5a89a7f400b
---
User set this at 2026-04-22 after post-clamp fleet is running 20+ strategies.

## The deal

- **Window**: 2026-04-22 → 2026-05-01 (Fri). ~9 calendar days including 1.5 trading weeks.
- **Purpose**: collect real post-clamp trade samples. Stop the "every audit surfaces new bugs" loop by letting the fleet run long enough to see what's real.
- **My role during window**: minimal intervention. Keep fleet alive, silent-block alerts, one-shot cleanup. No new features, no strategy tuning, no config changes unless a bug blocks trades.

**Why**: User's own diagnosis — "keep finding reasons why trades were not triggering" — drives constant auditing. Breaking that loop requires uninterrupted collection time.

## Goal framing (user-stated)

- **Ultimate target**: 200-300% total return (on $10K anchor). User knows this is aggressive.
- **Minimum bar**: outperform SPY. User framing: "SPY is the low benchmark, not high."
- **Honest math I gave**: 200% annual ≈ 9-10% MTD sustained ≈ top-1% hedge fund territory. Requires structural changes (leverage / options / concentration), not plumbing fixes.

## Aggregation fallacy — flagged for user 2026-04-22

User assumed: "15 strategies × 15-40% return each = 200%+ portfolio return." **False.** Portfolio return is weighted avg of slice returns, not sum. 15 strategies each making 20% on their $667 slice = $2,000 = **20% portfolio return on $10K, not 300%**.

**Levers that actually produce 200%**:
1. Leverage (5-10× margin) — multiplies both up and down
2. Compounding at 20-30%/yr for 6-7 years = 200-400% total
3. Concentration — 70% capital on the one strategy that actually works
4. Options / futures leverage (defined-risk)
5. Higher-frequency strategies (more capital turns per year)

**How to apply**: When user asks about progress toward 200%, reframe first: "are strategies delivering backtest-predicted 15-40% live?" If yes, the 200% conversation becomes a leverage decision. If no (paper-to-live edge decay), leverage multiplies a dying edge — wrong move. Maturity report answers this.

**Realistic ceilings on $10K paper**:
- 20-30% annualized: achievable, well-executing fleet
- 50-80% annualized: requires leverage lever, manageable downside
- 200%+ annual: requires leverage + cooperating tape, unusual
- 200%+ total multi-year: achievable via compounding (6-7 years at 20-30%)

## My queued background work (approved to do quietly)

**🗂️ 2026-04-23 STATUS UPDATE: all 3 items COMPLETED early.**

1. ✅ `ops/operational_maturity.py` built 2026-04-23 + auto-scheduled daily via managed_truth_loop. First report: 21 strategies, 25 post-reset trades, 1 EMERGING (multi_orb drift 0.71).
2. ✅ Watchdog hardening done via new `ops/silent_block_check.py` — per-strategy session-window signal floor check. Runs every 3 min via managed_truth_loop. Discord alerts wired with 60-min cooldown.
3. ✅ spy_mean_rev duplicate reaped 2026-04-22 (during earlier session). `RESET_DRAWDOWN` flag file deleted 2026-04-22.

See `project_session_20260423_wrap.md` for complete list.

## Queued but deferred

Original 5 pending items from 2026-04-20 post-audit — **not to be done this window**:
- Re-register ArgusCohortReport with -Credential
- Watchdog → Windows service (NSSM install required)
- Drawdown-breaker minimum-peak floor (turned out to already be deployed; verify only)
- wick_gbpusd fix-or-kill (phantom-close bug + no --loop)
- runner_unified drawdown-breaker floor refinement

## First message on user return (5/1 template)

- Operational maturity report (verdict per strategy)
- Post-clamp trade count totals
- Silent-block alerts (if any)
- April close vs SPY delta
