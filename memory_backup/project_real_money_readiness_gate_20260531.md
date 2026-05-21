---
name: Real-money readiness gate — 20-point checklist (5/31)
description: The hard line before real money goes in. Every item must be GREEN or have an explicit waiver. Sign-off ceremony is Week 4 prep + Week 5 final.
type: project
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
## Use this on 2026-05-23 (prep) and 2026-05-28 (final)

The user signs off each item. Anything not GREEN blocks real-money go-live.

## The 20 points

### Capital + account
- [ ] **1. Account at IBKR paper holds expected balance** — ~$11,815 + interest accrued. Verified via `broker_truth.account_equity_usd` matches IBKR statement.
- [ ] **2. Real-money account funded.** Separate from paper. Initial allocation decided ($500-$2000 starting).
- [ ] **3. IBKR paper and real share the same login** OR real-money creds are documented in password manager (NOT in repo).

### Reconciliation cleanliness
- [ ] **4. Zero RECON_DRIFT events in last 14 days.** Per `argus_flow/logs/canonical_reconcile.json`. The 4/26 USDJPY orphan was the last one — must stay clean for 14d.
- [ ] **5. Zero artifact_divergence flags.** Per `argus_flow/logs/artifact_divergence_report.json` showing OK or DEGRADED only with documented cause.
- [ ] **6. canonical_fills.jsonl reconciles to per-strategy CSVs.** All 22 strategies show delta_n=0 in `data.json` from full_audit.

### Operational stability
- [ ] **7. ArgusCohortReport last_result=0 for 7 consecutive nights.** No "WITH FAILURES" entries in `cohort_report.log` for the week prior.
- [ ] **8. ArgusWatchdog last_result=0.** Watchdog is supervising correctly.
- [ ] **9. All scheduled tasks last_result=0.** Per `tasks.json` from full_audit.
- [ ] **10. No DOWN runners in fleet_status.** All 22 strategies show OK or by-design quiet.

### Risk infrastructure
- [ ] **11. KILL_SWITCH drill passed Week 3.** All 22 runners halt entries within 60s.
- [ ] **12. Daily-loss circuit breaker drill passed.** drawdown_pause fires at -2% on injection.
- [ ] **13. Per-instrument cumulative cap implemented + tested.** Two strategies on same ticker = second rejected.
- [ ] **14. Manual override (KILL + FLATTEN_EOD) drill passed.**
- [ ] **15. Real-money-specific rules from `project_risk_guardrails_real_money_20260501.md` are coded.** Halved risk_pct, daily trade cap, no promotion ladder for 30d, etc.

### Documentation
- [ ] **16. `reference_reboot_recovery.md` is current.** Steps work as written, confirmed via dry-run.
- [ ] **17. `project_operations_solo_manual_20260501.md` reviewed end-to-end.** No "I don't know what to do here" moments.
- [ ] **18. `reference_failure_modes.md` extended to 15+ modes.** New ones added Week 4 from operational experience.

### Decision discipline
- [ ] **19. 5/1 verdict record exists at `argus_flow/logs/verdict_20260501.json`.** Every strategy has a verdict.
- [ ] **20. Final cull executed.** Strategies marked KILL on 5/1 are stopped + dispositions updated. Fleet count smaller than 4/26.

## Sign-off

```
Signed: ksmith2322 ___________
Date:   2026-05-__
Time:   __:__ UTC

Real-money go-live: APPROVED / DEFERRED-TO-2026-MM-DD / DECLINED
Initial real-money allocation: $______
First strategy to go real (must be a 5/1 WINNER): ____________
```

## Waivers

If an item can't be GREEN by 5/28, document the waiver explicitly:

```
Item: 13 — per-instrument cumulative cap
Waiver: Implementation deferred to 2026-06-15. Mitigated by manual review of daily exposure for first 30d.
Sign-off: ksmith2322, 2026-05-28
```

Three waivers max — beyond that, defer go-live entirely.

## Scope of "real money go-live"

When the gate passes, this is what activates:

- ONE strategy moves from paper → real first (the strongest WINNER from 5/1, ideally with 30+ live paper trades + PF ≥ 1.3)
- Real-money allocation: $500-2000 starting
- Real-money risk_pct = 0.5% (half of paper tier A)
- Other 21 strategies stay on paper account
- Daily review of every fill for 5 days
- 30-day "watch closely" period before adding a second strategy

This is NOT "flip the whole fleet to real" — that's a later milestone (~Q3 2026).

## How to apply this memory

**Why:** the gate is the hard line. Real money + bugs = lost real money. The 20 items are exactly the things that protect against that.

**How to apply:**
- Don't skip items. Each one was learned the hard way somewhere.
- Waivers are tracked — if more than 3, the gate has failed and go-live defers.
- After successful go-live, add new items to this list as new failure modes are discovered. The gate evolves.
