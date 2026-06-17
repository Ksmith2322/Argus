---
name: Fix-by-5/31 batch 2 completed (2026-05-18)
description: 5 of 8 fix-by-5/31 items shipped — GLD policy test, FX retry cascade tests, X5 fail-OPEN guard sweep (3 modules), killed-strategy RUNTIME invariant + unwind ticket scaffold, evidence_epoch first-class object. 130/130 tests green across both batches.
type: project
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
Second batch of Codex audit 2026-05-18 items shipped same day as fix-now.

**Why:** After clearing the 7-item fix-now queue (project_fix_now_completed_20260518.md),
the operator said "lets keep pushing and get the next batch done." Tackled
five tractable items from the fix-by-5/31 queue. Three of the remaining
items (live-vs-replay parity, order lifecycle audit, capacity stress) are
bigger and parked for the next session.

**How to apply:** When the user references "evidence_epoch", "fail-closed
guards", "killed runtime invariant", "unwind tickets", or asks "what
landed in the second batch on 5/18", this is the index.

## What shipped

1. **GLD cap policy test** — `argus_flow/tests/test_gld_cap_policy.py` (4 tests).
   Validates the 4-layer cap alignment from fix-now #1 (fleet_sizing
   per-strategy == cluster_exposure per-strategy, METALS cluster cap
   exists with sensible value, max_notional_usd routes through the
   per-strategy override). Catches anyone re-opening the 2.2× / 0.4× drift.

2. **FX exit retry cascade tests** — `argus_flow/tests/test_fx_exit_retry_cascade.py` (8 tests).
   Documents the 3-stage retry cascade in `_check_order_timeouts` and verifies
   each stage's TIF behavior: stage A (initial) → GTC LMT for CASH; stage B
   (30s retry) → GTC LMT for CASH, IOC MKT for non-FX; stage C (60s retry)
   → GTC for both. Source-shape invariants guard the cascade against accidental
   refactor (3 stages must remain, FX-vs-non-FX branch must remain, 120s
   exhaustion must still write incident).

3. **X5 fail-OPEN guard sweep** — Codex audit X5 flagged guards that swallowed
   exceptions and logged "allowing trade", i.e. a bug in the guard silently
   disabled it. Fixed 5 sites:
   - `helio/cluster_exposure.py` — anchor-read exception → `ANCHOR_UNAVAILABLE`;
     anchor zero/negative → `ANCHOR_NON_POSITIVE`; margin-aware-check exception
     → `MARGIN_CHECK_ERROR` (all refuse the trade).
   - `helio/ibkr_execution.py` — pre-entry broker query exception → refuse with
     `broker_query_failed:<exc_type>`; hard size-cap exception → `size_cap_check_failed:`;
     cluster-cap exception → `cluster_cap_check_failed:`.
   - `helio/ibkr_executor.py` — fleet halt exception → refuse; cluster-cap
     exception → refuse.
   Regression test `argus_flow/tests/test_guards_fail_closed.py` (4 tests)
   includes a source-level check that "allowing trade" is no longer present
   in any guard exception path.

4. **Killed-strategy RUNTIME invariant** — Codex X6's "killed → no new entry,
   no owned exposure" was only partially covered by the static-config test.
   Now ALSO enforced at runtime:
   - `helio/ibkr_execution.submit_bracket` refuses entry when `strategy_label`
     is in `KILLED_STRATEGY_CUTOFFS` (reject_reason=`killed_strategy:<label>:<cutoff>`).
     Belt-and-suspenders against config drift.
   - `helio/killed_strategy_invariant.py` (new module) — scans broker
     positions and flags any owned by a killed strategy without a matching
     active unwind ticket. `KILLED_STRATEGY_SYMBOLS` maps each killed strategy
     to the symbols it owned.
   - `argus_flow/configs/killed_strategy_unwind_tickets.json` (new scaffold) —
     ships empty; operator adds a ticket per incident with strategy, symbol,
     max_qty, operator, signed_at, expires_at, reason.
   - `argus_flow/tests/test_killed_strategy_runtime_invariant.py` (9 tests).

5. **evidence_epoch as first-class object** — Codex X4: "evidence_epoch.json
   declares the current epoch + exclusion set. Every ROI report must name
   its epoch. Makes contamination structurally impossible to silently mix in."
   - `argus_flow/configs/evidence_epoch.json` — schema: `current_epoch_id` +
     `epochs[]` (each: id, started_at, ends_at, label, is_clean,
     contamination_notes, exclude_strategies, phantom_trades).
   - `helio/evidence_epoch.py` — `EvidenceEpoch` dataclass, `current_epoch()`,
     `get_epoch(id)`, `epoch_for_timestamp(ts)`, `stamp_report(report)`.
     Fail-closed on missing/corrupt registry (raises `EpochRegistryError`).
   - Pre-freeze epoch (`pre_freeze_20260418`) ships `is_clean=False` + the
     4 killed strategies in exclude_strategies + the 5/5 phantom recorded.
   - Post-reset epoch (`post_reset_20260601`) ships `is_clean=True`, waiting
     for `ops/maintenance/epoch_reset.py` to advance `current_epoch_id` at
     the 5/31 cutover.
   - `argus_flow/tests/test_evidence_epoch.py` (14 tests).

## Test scoreboard at session end

- GLD cap policy: 4/4
- FX exit retry cascade: 8/8
- Guards fail-closed: 4/4
- Killed-strategy runtime invariant: 9/9
- Evidence epoch: 14/14
- Batch-2 subtotal: **39/39**
- Plus all batch-1 tests: **130/130 combined across touched files**

## Still remaining (parked for next session)

From fix-by-5/31 queue (3 of 8 still open):

1. **Live-vs-replay parity report per strategy** — signal count, direction,
   fill expectancy. Significant per-strategy work; needs replay harness.
2. **Per-strategy order lifecycle audit** (Codex X7) — broker order ID →
   signal → entry submit → entry fill → child submit → child fill/cancel →
   state transition. Biggest structural change; needs lifecycle table model.
3. **Capacity / margin stress test per promoted strategy** — slippage at
   1x/2x/5x/10x, volume concentration, scaling ceiling.

Also still pending from fix-now #4:

- **Orphans** — flatten or write explicit unwind tickets for QQQ 7, SPY 26,
  GLD 22. Needs operator authorization to touch broker positions; not
  unilaterally fixable.

Codex X3 (intent-based ownership, not symbol-based) remains structural
work for the post-reset window — Codex flagged it as architectural debt
rather than a 5/31 blocker.

## Integration TODO (not blockers, but the scaffold is incomplete without)

The evidence_epoch object exists; ROI consumers do NOT yet stamp it onto
their reports. The follow-up integration:
- `ops/audit/run_strategy_roi_proof.py` — call `stamp_report()` on each
  produced report.
- `ops/audit/run_killed_strategy_review.py` — same.
- `helio/promotion_check.py` — refuse if input report lacks `epoch_id`
  or has `epoch_is_clean=False` (without explicit override).
- Dashboard /api/strategy_actions — show epoch badge per metric.

That integration sweep is what closes the "structurally impossible to
silently mix in contamination" loop. Build the scaffolding now (this
batch); wire consumers post-5/31 reset when current_epoch advances.
