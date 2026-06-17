---
name: 2026-05-20 golden-trace harness (Phase 2 of activity-multiplication)
description: Shipped helio/event_recorder.py (captures every ib_insync broker event to JSONL during paper trading; opt-in via GOLDEN_TRACE_PATH env var) + helio/trace_replay.py (load_trace + TraceFakeIB mock IB + find_anomalies analyzer detecting FILL_DURING_CANCEL and DOUBLE_FILL patterns) + first regression fixture (5/19 cascade race as 9-event JSONL). 19 new tests, 129/129 green across all touched files. Phase 2 of the activity-multiplication plan; the recorder makes every future incident a candidate regression test.
type: project
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
After Phase 1 of activity-multiplication shipped the paper-stress
multiplier + stress_injector, the next gap was forensic visibility:
when a bug fires live, we reconstruct it from unstructured logs and
write a synthetic regression test that approximates the conditions.
This loses timing fidelity and is reactive.

Phase 2 closes the gap. Going forward, every paper trade can be
captured as a structured JSONL event trace, and any incident can be
converted into a deterministic regression test by replaying the trace
against a TraceFakeIB.

## What shipped

### 1. helio/event_recorder.py — IB event capture

`EventRecorder` class wraps an `ib_insync.IB` instance and subscribes
to every broker-side event:

  - `orderStatusEvent` — every order status change
  - `execDetailsEvent` — every fill with execution details
  - `errorEvent` — IBKR error codes + messages
  - `positionEvent` — position updates
  - `newOrderEvent` — orders submitted
  - `disconnectedEvent` / `connectedEvent` — connection lifecycle

Each event written as one JSONL line with absolute timestamp +
monotonic_ms offset so timing relationships are preserved. Safe
serialization handles ib_insync's NamedTuple-style objects without
crashing on unknown types.

Write failures never propagate — `log.warning` and continue. Trace
recording must never take down the runner.

`attach_recorder(ib, path)` is a one-line convenience. `.close()`
detaches all handlers cleanly.

### 2. helio/trace_replay.py — load + mock + analyze

Three pieces:

**`load_trace(path)`** — reads JSONL back into a list of dicts.

**`TraceFakeIB`** — minimal mock implementing the `ib_insync.IB`
surface that runner_unified.py actually calls:
  - `positions()` returns synthesized FakePosition list
  - `placeOrder(c, o)` returns a FakeTrade, records call
  - `cancelOrder(o)` records call, sets status to Cancelled
  - `qualifyContracts(*c)` no-op pass-through
  - `isConnected()`, `sleep()`
  - `apply(event)` mutates state to reflect one trace event

Tests use `apply()` to walk a trace forward, then call runner methods
on a SimpleNamespace stub that points `._ib` at the fake. This is
deliberately NOT a full real-time event simulator — the runner's
async/threaded behavior would make that complex. State-machine
mutation is enough for regression tests.

**`find_anomalies(trace)`** — analyzer that scans for known
anti-patterns:
  - `FILL_DURING_CANCEL`: exec fill arriving after a cancel was sent
    on the same order_id (5/19 cascade race signature)
  - `DOUBLE_FILL`: two exec details on the same order_id

Returns a list of `TraceAnomaly` records. Empty list = clean trace.
New patterns can be added as new bugs surface.

### 3. argus_flow/tests/fixtures/cascade_race_20260519.jsonl

9-event hand-crafted trace mimicking the 5/19 cadence:
1. connected
2. position LONG 41479 CAD/JPY (initial state)
3. newOrder SELL 41479
4. orderStatus Submitted
5. execDetails — 41479 @ 90.451 (FILLED)
6. orderStatus Filled
7. position 0 (broker flat)
8. orderStatus PendingCancel (the buggy redundant cancel)
9. orderStatus Cancelled (broker acks cancel on already-filled)

Hand-crafted because we didn't have the recorder when 5/19 fired live.
Future incidents will produce real traces from the recorder, and the
fixtures directory will grow organically.

### 4. Runner wiring (opt-in)

`argus_flow/runner_unified.py` checks `GOLDEN_TRACE_PATH` env var at
fleet startup. If set, attaches the recorder to the IB instance and
logs `GOLDEN_TRACE_RECORDER attached -> <path>` at WARNING level so
operators see it on every restart with recording active.

Empty env var = no overhead. Default off. Reversible.

## Activation

```powershell
$env:GOLDEN_TRACE_PATH = "argus_flow/logs/traces/argus_$(Get-Date -Format yyyy-MM-dd).jsonl"
$env:IBKR_PORT = "7497"
python -m argus_flow.runner_unified
```

Trace file grows as events arrive. One file per session per runner is
the recommended pattern. Logs/traces/ directory should be added to
.gitignore on PC1; PC2 (data-collection node) commits traces back to
the pc2-experiments branch for sharing.

## Tests

**19 new in argus_flow/tests/test_golden_trace.py:**
- recorder attaches to all 7 event slots
- recorder writes initial attached marker
- recorder captures orderStatus events
- recorder captures execDetails events
- recorder close detaches handlers
- recorder write failure does not raise
- load_trace round-trips JSONL
- FakeIB position apply / removal / direct setter
- FakeIB placeOrder returns trade with assigned id
- FakeIB cancel records call
- FakeIB exec details appends fill
- Analyzer detects DOUBLE_FILL
- Analyzer detects FILL_DURING_CANCEL
- Analyzer clean trace has no anomalies
- Cascade fixture loads and reaches flat-broker state at checkpoint
- Cascade fixture has expected event sequence (pinned)
- Runner wires GOLDEN_TRACE_PATH

**129/129 green** across new + regression neighbors (paper_stress,
stress_injector, exit_broker_truth_guard, audit_fixes_20260520,
pead, real_money_boundary, killed_strategy_invariant,
static_safety_invariants, sunset_roster).

## Coverage gaps to flag

- **Fixture is hand-crafted, not captured.** When the 5/19 bug fired
  live, we did not have the recorder. The fixture approximates the
  event sequence from log analysis. Future incidents will produce
  real recorder output that's higher fidelity.
- **TraceFakeIB covers what runner_unified uses.** New ib_insync
  methods called by future runner code will need mock implementations.
- **Analyzer detects two patterns.** Add more as new race conditions
  surface — e.g., partial-fill ordering (one of the still-deferred
  items from 5/20 audit P2 list).
- **Real-time replay not implemented.** Tests advance state via
  `apply()`; the runner's async event loop isn't re-driven. Adequate
  for state-machine regression tests; insufficient for testing
  callback-ordering bugs. Phase 3 might add this.

## The flywheel

The shipping cadence over the past 5 days:

  5/16 → CBOT routing bug (22-day silent) — found by manual ROI audit
  5/17 → 7 stuck broker positions cleared — found by inspection
  5/18 → Codex sweep + fix-now queue (7 items)
  5/19 → 5-agent audit + PEAD/xs_momentum backtests + cascade bug
  5/20 morning → 5-agent operational audit + 9 fixes
  5/20 afternoon → activity-multiplication Phase 1 (paper_stress, stress_injector)
  5/20 evening → golden-trace Phase 2 (recorder, replay, analyzer)

The pattern: audits caught structural bugs; live exercise caught
operational bugs; recorder closes the loop by making every future
live exercise a source of replayable evidence.

The remaining piece is **real-time event replay** for callback-ordering
bugs — Phase 3, not blocking 5/31 reset. The 5/31 cutover plan stands;
nothing in golden-trace changes the timeline.
