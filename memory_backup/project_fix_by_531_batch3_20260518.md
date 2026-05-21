---
name: Fix-by-5/31 batch 3 completed (2026-05-18)
description: 3 more items shipped — evidence_epoch wired into 3 ROI consumers (run_strategy_roi_proof + killed_strategy_review + promotion_check), lineage_id added to canonical_fills as X3+X7 prerequisite, capacity/margin stress harness with per-strategy 1-2-5-10× scaling simulation. 160/160 tests green across batches 1-3.
type: project
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
Third batch of Codex audit items shipped same session.

**Why:** Operator continued saying "lets get the next batch done". Tackled
the integration sweep that closes the evidence_epoch loop (without it, the
scaffold from batch 2 is dead code), plus the foundational X3+X7 prerequisite
(lineage_id), plus the capacity stress harness — the last fix-by-5/31 item
that didn't need a replay rig.

**How to apply:** When the user references "stamped reports", "epoch-aware
promotion", "lineage_id", "intent-based ownership", "capacity headroom",
or "max_safe_multiplier", this entry is the index.

## What shipped

1. **evidence_epoch integration into 3 ROI consumers** —
   - `ops/audit/run_strategy_roi_proof.py`: imports `helio.evidence_epoch`,
     writes new `roi_proof_summary.json` artifact stamped via `stamp_report()`,
     markdown report includes the epoch header + contamination warning when
     `is_clean=False`.
   - `ops/audit/argus_audit_engine.killed_review_md`: prepends the epoch
     stamp + contamination warning so killed-strategy resurrection decisions
     can't be made against pre-reset data without operator noticing.
   - `helio/promotion_check.py`: paper→real promotion gate now requires
     `epoch.is_clean=True`. If `current_epoch_id` is the pre-freeze epoch
     (contaminated), `READY_FOR_REAL` is downgraded to `COLLECTING` with
     `contaminated_epoch:<id>` in blockers. Fail-closed if registry unreadable.
   - 6 integration tests in `argus_flow/tests/test_evidence_epoch_integration.py`.

2. **lineage_id on canonical_fills (Codex X3+X7 prerequisite)** —
   - `helio/domain.py`: added `lineage_id` field to `Fill`, new helper
     `make_lineage_id(strategy, session_id, entry_order_id)` →
     `"<strategy>.<session_id>.<entry_order_id>"`. Components truncated
     to 24 chars; `-` placeholder when component missing.
   - `Fill.to_canonical_row()` drops lineage_id when None (preserves dedup-key
     stability for pre-lineage rows in canonical_fills.jsonl + the backfill
     reconciler).
   - `helio/ibkr_execution.submit_bracket`: ENTRY-side dual-write stamps
     lineage with `session_id = f"c{client_id}p{pid}"` and
     `entry_order_id = entry_fill.order_id`.
   - `argus_flow/runner_unified._on_fill`: argus ENTRY-side dual-write
     stamps lineage with the same shape.
   - 9 tests in `argus_flow/tests/test_lineage_id.py` covering format,
     round-trip, dedup-stability, source-level regression of both writer
     callsites.

   This is the *foundation* for X3 (intent-based ownership) — a future
   reconciler can now ask "which strategy intended position X" rather than
   "which strategy trades symbol Y." It's also the X7 prerequisite —
   the lifecycle table can use lineage_id as the join key from signal
   through fill through child orders.

3. **Capacity / margin stress harness** — `helio/capacity_stress.py`,
   `argus_flow/tests/test_capacity_stress.py` (10 tests).
   For each strategy: simulates [1×, 2×, 5×, 10×] notional scaling against
   ALL 5 cap layers (per-strategy, single-instrument, cluster, total notional,
   maint margin). Reports per-multiplier breaches + `max_safe_multiplier`.
   Fleet-level `stress_fleet()` returns per-strategy reports + per-multiplier
   survivor sets. Designed for the post-5/31 promotion ceremony: strategies
   with high `max_safe_multiplier` have scaling headroom; those at 1× max
   are pinned.

## Test scoreboard at session end

- Evidence epoch integration: 6/6
- Lineage ID: 9/9
- Capacity stress: 10/10
- Batch-3 subtotal: **25/25**
- **160/160 combined** across batches 1-3 (fix-now + fix-by-5/31 batches 2+3).

## Outputs to find

- **Stamped ROI summary:** `ops/reports/system_audit/roi_proof_summary.json`
  carries `epoch_id`, `epoch_label`, `epoch_is_clean`. Dashboard or
  promotion tooling can read this directly to know which window the
  numbers are from.
- **Promotion-check JSON:** runner-level result dicts now include
  `epoch_id` + `epoch_is_clean` for paper-stage runners. A `COLLECTING`
  verdict with `contaminated_epoch:...` in blockers means "the math would
  promote but we won't trust pre-reset evidence."
- **Capacity stress JSON shape:** `stress_strategy()` and `stress_fleet()`
  return JSON-serializable dicts ready for dashboard ingestion. The
  per-strategy `max_safe_multiplier` is the headline number.

## Still parked (now smaller queue)

From fix-by-5/31, only the two BIG items remain:

1. **Live-vs-replay parity per strategy** — needs a replay harness.
   Significant per-strategy work. The lineage_id foundation we just shipped
   makes this tractable: a replay can now compare which signals/entries
   produced which lineage_ids vs. what live actually wrote.

2. **Per-strategy order lifecycle audit (Codex X7)** — broker order ID →
   signal → entry submit → entry fill → child submit → child fill/cancel →
   state transition. The biggest structural piece. lineage_id is the
   *key*; building the actual lifecycle table is the next pass.

From fix-now, still pending:

- **Orphans** (QQQ 7, SPY 26, GLD 22) — operator authorization required
  to flatten or write unwind tickets. Not unilaterally fixable.

## Integration TODO (lower priority follow-up)

- Dashboard `/api/strategy_actions` should show `epoch_id` badge per
  metric block, and refuse to display `READY_FOR_REAL` recommendations
  computed against a contaminated epoch.
- The capacity stress harness needs a CLI driver
  (`python -m ops.audit.run_capacity_stress`) so it can be scheduled +
  output to `ops/reports/system_audit/capacity_stress.json` for the
  weekly audit cadence. Library is done; CLI is mechanical.

## Codex audit coverage scoreboard

Fix-now queue: **5/6 done** (orphans needs operator action).
Fix-by-5/31 queue: **6/8 done** (live-vs-replay + lifecycle audit
remain — both are bigger structural pieces).
Codex extras: **X4 (epoch object) wired through. X5 (fail-OPEN) cleaned.
X6 (killed-strategy invariant) static + runtime. X3+X7 foundation
(lineage_id) shipped; full implementation parked.**
