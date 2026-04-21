# Architecture Proposals — 2026-04-21

Not shipped this session. Design-only. Scoped for a future dedicated sprint.

Derived from the 2026-04-21 multi-agent audit. Covers the two architectural
risks flagged CRITICAL that need deliberate engineering rather than drive-by
fixes.

---

## 1. runner_unified Single-Point-of-Failure (SPOF) mitigation

### Problem

`argus_flow/runner_unified.py` runs all FX paper trading (3 pairs today,
~15-20 at scale) in one Python process with one IBKR connection. If it
dies, all pairs go dark until watchdog notices (~60s) and restarts
(~10-15s more). 2026-04-20 incident: runner died at 02:25 UTC, watchdog
was itself dead, nothing restarted for ~4 hours until manual intervention.

Today's mitigations: watchdog.ps1 (fixed 2026-04-21 to stop kill-looping)
and the new `meta_watchdog.ps1` (restarts watchdog+fleet_monitor every
15 min via SYSTEM-account scheduled task). Those close the "supervisor-dies"
hole but not the "runner-dies-mid-trade" hole.

### Options considered

**Option A — Per-pair process isolation.** One runner process per pair.
  Pros: isolated failures. Cons: breaks shared risk state (portfolio_guard,
  fleet_risk, drawdown breaker all rely on cross-pair coordination). Would
  require an IPC layer or a shared persistent state service. Significant
  rewrite.

**Option B — Hot-standby secondary process.** Two runner_unified instances,
  one active, one passive. Passive tails state+fills, ready to take over
  on primary death. Complex failover state machine. Client-id collisions
  at IBKR. Over-engineering for paper.

**Option C — Faster auto-restart with state snapshot.** Keep single-process
  design. Add a periodic "state snapshot" that captures the full runtime
  view (open positions, risk counters, recent signals) to disk every 60s.
  On restart, load the snapshot instead of rebuilding from CSV+broker.
  Cuts restart time from ~15s to ~3s.

**Option D — Thread-per-pair within one process.** Same process, separate
  threads per instrument. If one thread crashes (e.g., yfinance exception),
  others survive. Doesn't help if the whole process OOMs, but catches the
  most common "one pair's data source broke" failure.

### Recommendation

**Option C + D together, deferred to a dedicated sprint (~1-2 weeks).**
  - State snapshot: 60s → 3s recovery
  - Thread-per-pair: isolates non-fatal per-instrument errors

Do NOT do Option A — the shared-risk coordination is load-bearing and
worth preserving. Do NOT do Option B — paper doesn't warrant it.

### Acceptance criteria

1. Runner kill-test: force-kill runner_unified mid-tick, verify watchdog
   restart + state recovery within 5s, no duplicate orders.
2. Thread fault: inject a pandas exception into one pair's tick handler,
   verify other 2 pairs keep running.
3. OOM simulation: cap runner memory at 512MB, run 30min loop, verify
   watchdog restarts before any pair misses more than 2 ticks.

---

## 2. State consistency across 5 sources of truth

### Problem

A trade's lifecycle touches:
1. Per-strategy `state.json` (open position, entry price, size)
2. Per-strategy `trades.csv` (closed trade record)
3. `canonical_fills.jsonl` (fleet-wide aggregate, appended on close)
4. `broker_state.json` (IBKR snapshot — potentially stale)
5. `fleet_state.json` (merged read-model across strategies)

Crash scenarios where these diverge:

| Crash Point | state.json | trades.csv | canonical_fills | broker |
|---|---|---|---|---|
| before close-logic runs | open position | no row | no row | actual fill |
| after trades.csv write, before canonical | FLAT | row present | no row | actual fill |
| after canonical write, before state save | open position | row present | row present | actual fill |

No crash-resume logic reconciles these consistently. On restart, the runner
reads state.json (may be stale), backfills trades.csv into canonical (may
duplicate), and assumes broker is truth (but doesn't cross-check).

### Today's partial fixes

- 2026-04-21: `helio/canonical_fills.py` now raises `TimeoutError` on lock
  timeout instead of silently dropping; fallover rows land in
  `canonical_fills_failover.jsonl`.
- `argus_flow/ops/reconciliation.py` exists and writes `reconciliation_report.json`
  but only checks count-level drift, not row-level consistency.

### Proposed merge strategy

**Single authority principle:** declare canonical_fills.jsonl as the
legal record. Every other surface becomes a read-model derived from it +
live broker state.

**Recovery protocol on crash:**

1. On startup, load canonical_fills.jsonl. For each strategy, compute
   current-from-log position (sum of EXIT side fills since last FLAT row).

2. Compare to broker positions. On mismatch:
   - If broker has position we don't → treat as a fill we missed; prompt
     operator to manually record (write to canonical_fills_recovery.jsonl).
   - If we have position broker doesn't (paper mode) → we're fine, paper.
   - If we have position broker doesn't (real mode) → PAUSE_ENTRIES,
     write incident, require manual resolution.

3. Write the normalized view to `state.json` + `broker_state.json`.
   trades.csv is purely derived (can be rebuilt from canonical_fills
   at any time).

4. Add `helio.state_recovery` module with `reconcile_on_startup()` that
   every runner calls before entering its main loop.

### Acceptance criteria

1. Inject a crash between trades.csv write and canonical_fills write.
   Restart. Verify recovery protocol catches the gap and writes a
   recovery row.
2. Inject a crash before state.json save (but after canonical write).
   Restart. Verify state.json is reconstructed correctly.
3. Reconciliation-on-restart completes in <5s for a 10K-row canonical_fills.

### Scope estimate

- `helio/state_recovery.py`: ~300 LOC
- Integration hooks in each runner (runner_unified + 5 forge runners): ~20 LOC each
- Tests: `test_state_recovery_faults.py` with 6+ crash scenarios: ~400 LOC
- Total: ~5-7 engineer days

Deferred because:
- Cohort rule (don't modify trade logic while cohort running) — this is a
  trade-logic change requiring a planned cutover window
- Paper-only success metric means state drift costs data quality, not real
  money — it's fixable by reconcile-then-rebuild in the short term
- Higher-impact volume work (new strategies, skill UX) is more urgent
  right now

---

## 3. External alerting (bonus)

Today's alerting = Discord webhook. Single-channel, single-webhook, no
retry to alternate channels, no rate-limit awareness.

Proposal: add one of Pushover / Telegram / email as a secondary channel,
cascading on Discord failure. Minimal change (helio/discord_alerts.py
already has the pattern). Estimated 1-2 days.

Not shipping now because the user hasn't hit a "Discord was down and I
missed an alert" incident yet. Flag for the day it happens.

---

## Summary

| Proposal | Status | Effort | Priority |
|---|---|---|---|
| Runner SPOF: snapshot + thread-per-pair | Design complete, not coded | 1-2 weeks | HIGH |
| State consistency: canonical-as-truth + recovery | Design complete, not coded | 5-7 days | HIGH |
| External alerting fallback | Noted only | 1-2 days | MEDIUM |

All three are real risks that compound as the fleet grows. Today's fleet
(3 paper pairs, 1M anchor) can tolerate them; a 10-pair real-money fleet
cannot.
