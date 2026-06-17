---
name: 2026-05-20 trace inspector CLI + invariants (Phase 4 of activity-multiplication)
description: Shipped helio/trace_invariants.py (4 cross-event invariants — ORDER_LIFECYCLE_VALID, NO_FILL_WITHOUT_ORDER, NO_CANCEL_AFTER_FILL, NO_DUPLICATE_EXEC_ID) + 3 new find_anomalies patterns (ORPHAN_POSITION_CHANGE, RAPID_REVERSAL, STATUS_REGRESSION) + ops/trace_inspect.py operator CLI (summary stats, anomalies, violations, exit code 0/1/2). Cascade fixture flags 3 invariant violations + 1 anomaly when run through the CLI. 21 new tests; 169/169 green across all 4 phases.
type: project
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
Phases 1-3 built the data-collection plumbing (multiply activity,
inject synthetic signals, record events, replay state, dispatch
callbacks). Phase 4 is the **operator-facing analysis layer** — point
the CLI at any captured trace and get actionable findings in one
command.

## What shipped

### helio/trace_invariants.py — hard invariants

Four cross-event rules that MUST be true for a well-behaved trace.
A violation = real bug, not heuristic.

  1. **ORDER_LIFECYCLE_VALID** — orderStatus transitions follow the
     state machine (PendingSubmit → Submitted → Filled/Cancelled).
     Regressions and skips are violations.

  2. **NO_FILL_WITHOUT_ORDER** — every execDetails for an order_id
     must be preceded somewhere in the trace by a newOrder or
     orderStatus for that order_id.

  3. **NO_CANCEL_AFTER_FILL** — once Filled, no subsequent Cancelled
     or PendingCancel. This is the broker-side signature of the 5/19
     cascade race (IBKR reports Cancelled on an already-filled order
     during the cancel-sleep window).

  4. **NO_DUPLICATE_EXEC_ID** — each execution has a unique exec_id.
     Duplicates = double-counted fills (or broker data corruption).

`verify_invariants(trace)` returns `list[InvariantViolation]`. Empty
list = pass. Each violation has invariant name, trace index, order_id,
and human-readable summary.

### helio/trace_replay.py — extended anomaly patterns

Added three heuristic patterns alongside the existing FILL_DURING_CANCEL
and DOUBLE_FILL:

  - **ORPHAN_POSITION_CHANGE** — position changes between updates with
    no execDetails in between. Catches missed fill callbacks AND broker
    corruption like the 5/19 negative-cost garbage updates.
  - **RAPID_REVERSAL** — position flips long↔short in a single update.
    Catches the 5/19 cascade-race aftermath where positions reversed
    instead of closing.
  - **STATUS_REGRESSION** — orderStatus goes backwards in lifecycle
    (e.g. Filled → Submitted). Catches stale callback ordering.

Anomalies are SIGNALS TO INVESTIGATE; invariants are HARD RULES.

### ops/trace_inspect.py — operator CLI

  ```powershell
  python -m ops.trace_inspect argus_flow/logs/traces/argus_2026-05-21.jsonl
  python -m ops.trace_inspect <path> --json
  ```

Output (human mode): trace metadata, event counts by kind, invariant
violations with location, anomaly patterns with location. Exit codes:
  - 0 = clean (no invariant violations; anomalies are non-blocking)
  - 1 = invariant violations present
  - 2 = trace file not found

JSON mode emits a structured report for piping into automation.

## Tests

21 in `argus_flow/tests/test_trace_invariants.py`:

  - 3 ORDER_LIFECYCLE_VALID (clean / regression / repeat-status)
  - 2 NO_FILL_WITHOUT_ORDER (pass / orphan)
  - 2 NO_CANCEL_AFTER_FILL (cascade fixture / clean cancellation)
  - 2 NO_DUPLICATE_EXEC_ID (duplicate detected / null exec_id allowed)
  - 1 STATUS_REGRESSION
  - 2 ORPHAN_POSITION_CHANGE (detected / not-triggered)
  - 2 RAPID_REVERSAL (long→short / short→long)
  - 7 CLI: inspect returns structured report, clean trace empty,
    exits 1 on violation, exits 0 on clean, exits 2 on missing file,
    JSON mode valid, empty trace handled

**169/169 green** across all four rollout phases + regression neighbors
(paper_stress, stress_injector, exit_broker_truth_guard,
audit_fixes_20260520, pead, real_money_boundary,
killed_strategy_invariant, static_safety_invariants, sunset_roster,
golden_trace, event_dispatcher, trace_invariants).

## Smoke validation against cascade fixture

```text
$ python -m ops.trace_inspect argus_flow/tests/fixtures/cascade_race_20260519.jsonl

trace: argus_flow/tests/fixtures/cascade_race_20260519.jsonl
events: 9
window: 2026-05-19T15:00:00 -> 2026-05-19T15:02:32

by kind:
  orderStatus             4
  position                2
  connected               1
  newOrder                1
  execDetails             1

INVARIANT VIOLATIONS (3):
  [ORDER_LIFECYCLE_VALID] at idx 7 order_id=7421: invalid transition 'Filled' -> 'PendingCancel' (prev at idx 5)
  [NO_CANCEL_AFTER_FILL]  at idx 7 order_id=7421: PendingCancel after Filled at idx 5. Broker reporting cancel on already-filled order = cascade-race signature.
  [NO_CANCEL_AFTER_FILL]  at idx 8 order_id=7421: Cancelled after Filled at idx 5. Same.

anomalies / heuristic patterns (1):
  [STATUS_REGRESSION]     at idx 7: order_id 7421 status went backwards 'Filled' (idx 5) -> 'PendingCancel' (idx 7)
```

The CLI correctly catches the 5/19 cascade-race signature without any
operator code. Pointed at a real captured trace, this is the diagnostic
loop closed.

## The four-phase summary

| Phase | Tool | What it does |
|---|---|---|
| 1 | helio/paper_stress.py | Multiply real-strategy fires (config knob × runner restart) |
| 1 | ops/stress_injector.py | Synthetic brackets + chaos modes on test instruments |
| 1.5 | PC2 runbook (memory) | Independent paper node for data collection |
| 2 | helio/event_recorder.py | Capture broker events to JSONL during paper trading |
| 2 | helio/trace_replay.py | TraceFakeIB + find_anomalies for state regression |
| 3 | helio/event_dispatcher.py | Fire events to subscribers + ChaosTransform for ordering bugs |
| 4 | helio/trace_invariants.py | 4 hard invariants — ORDER_LIFECYCLE / NO_FILL_WITHOUT_ORDER / NO_CANCEL_AFTER_FILL / NO_DUPLICATE_EXEC_ID |
| 4 | ops/trace_inspect.py | Operator CLI: point at trace, get findings, exit code |

**Total today (across 4 phases):**
  - 6 new helio modules (paper_stress, event_recorder, trace_replay [extended], event_dispatcher, trace_invariants) plus the ones already shipped
  - 2 new ops tools (stress_injector, trace_inspect)
  - 1 fixture (cascade_race_20260519.jsonl)
  - 1 runner edit (GOLDEN_TRACE_PATH env var wiring)
  - 2 memory runbook docs (paper stress rollout + secondary laptop)
  - 169/169 green, ~86 new tests

## What's still deferred

- **Capture-replay parity** (Codex X1) — run a recorded trace through
  replay, capture the artifacts that would have been written, diff
  against original. The plumbing pieces are now all built; this would
  be a small composition layer. Not blocking 5/31.
- **Per-handler regression tests** — drive the actual `_on_fill`,
  `_check_order_timeouts`, etc. with the dispatcher. Each handler
  needs its own I/O-sink mocking; per-bug work, not generic infra.
- **Asyncio-loop simulation** — would catch scheduler-interleaving
  bugs. Cost is high vs. expected bug yield; defer.

## How to use this in the actual reset window

11 days until 5/31. Recommended sequence:

  1. Operator activates Phase 1 paper_stress on argus FX (config edit
     + restart). Strategies fire 3-5× more often.
  2. Operator sets GOLDEN_TRACE_PATH for the runner. Every trade
     captured to JSONL.
  3. At end of each day, run `python -m ops.trace_inspect` on the
     captured trace. Catch any invariant violations BEFORE they
     compound into incidents.
  4. Any violation → reverse-engineer with test_audit_fixes pattern,
     add to fixture set, ship a fix.
  5. Bugs found feed back into permanent regression tests.

Phase 4 closes the feedback loop. The bot can now self-report
suspected issues from its own captured traces, end-to-end, with one
command.
