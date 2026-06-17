---
name: 2026-05-20 event dispatcher (Phase 3 of activity-multiplication)
description: Shipped helio/event_dispatcher.py — fires JSONL trace events to ib_insync-shaped event slots in controlled order/timing, with chaos transformations (swap/drop/duplicate/delay_ms) for probing callback-ordering bugs. RecordingSubscriber test helper + make_target convenience. Phase 2 mutates state; Phase 3 drives callbacks. 19 new tests, 148/148 green across all three phases of the rollout.
type: project
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
Phase 2's TraceFakeIB lets tests assert final state but doesn't fire
events to subscribers. That's the gap for testing handler-order
sensitivity — bugs where event A arrives before B vs. after B
produces different outcomes. Phase 3 closes that gap.

## What shipped

### helio/event_dispatcher.py

**`EventDispatcher`** — given a target object with ib_insync-style
event slots (`orderStatusEvent`, `execDetailsEvent`, etc.), fires
each event in a JSONL trace to the matching slot. Reconstructs minimal
`Trade` / `Fill` / `Position` SimpleNamespace objects from the trace's
event data so subscribers receive what looks like real ib_insync
objects.

Three timing modes:
- `instant` — back-to-back, no waits (default; fast test execution)
- `accelerated` — wait `monotonic_ms` gaps / speedup
- `realtime` — wait actual `monotonic_ms` gaps from the original trace

**`ChaosTransform`** dataclass + `apply_chaos(events, transforms)`
function. Four transform kinds:
- `swap` — swap two event positions (test the "did execDetails arrive
  before orderStatus?" race)
- `drop` — remove an event (test "what if the fill callback was lost?")
- `duplicate` — fire the same event twice (test "is the handler
  idempotent?")
- `delay_ms` — add wait before firing (in realtime/accelerated mode)

`apply_chaos` does NOT mutate its input. Index targets are validated
defensively — out-of-range indices are no-ops, not crashes.

**`RecordingSubscriber`** — drop-in test helper that logs every event
it receives with args. Tests assert against `.calls` to verify event
order, ordering, and any dropped events.

**`make_target()` + `subscribe_recording(target, sub)`** — convenience
for tests: build a stand-in target with empty event slots, wire a
RecordingSubscriber to all of them, ready for dispatch.

## Tests

19 in `argus_flow/tests/test_event_dispatcher.py`:

  - dispatcher fires all events in order
  - exec_details fires with correct (order_id, shares, price) args
  - returns DispatchRecord for each event including dropped/failed ones
  - unknown event kind / missing slot do not raise
  - chaos swap reorders firing
  - chaos drop removes event from fire stream
  - chaos duplicate fires event twice
  - chaos delay actually sleeps in realtime mode
  - apply_chaos does not mutate input
  - unknown chaos kind raises ValueError
  - out-of-range index is a no-op
  - instant mode skips recorded gaps
  - accelerated mode compresses time (10x = ~50ms for 500ms gap)
  - cascade fixture fires full 9-event sequence to subscriber
  - cascade fixture with swap changes callback order
  - cascade fixture with drop removes execDetails fill
  - event slot supports unsubscribe
  - event slot supports multiple subscribers

**148/148 green** across all three phases + regression neighbors
(paper_stress, stress_injector, exit_broker_truth_guard,
audit_fixes_20260520, pead, real_money_boundary,
killed_strategy_invariant, static_safety_invariants, sunset_roster).

## How Phase 2 and Phase 3 compose

Phase 2 (TraceFakeIB) and Phase 3 (EventDispatcher) handle different
sides of the same trace:

**Phase 2 — state-machine regression**
```python
fake_ib = TraceFakeIB()
for evt in trace:
    fake_ib.apply(evt)
    # call runner methods that READ from ib, assert state
```

**Phase 3 — callback-ordering regression**
```python
target = make_target()
subscribe(target, runner_handler)
EventDispatcher(target).dispatch(trace, chaos=[ChaosTransform("swap", 5, 6)])
# assert handler did the right thing with the chaos-mutated ordering
```

Both consume the same JSONL fixtures from the recorder. Use whichever
matches the bug class.

## Limitations (deliberate, not bugs)

- **Synchronous dispatch.** Events fire on the dispatcher's thread,
  not via a real asyncio loop. Catches state-machine and ordering
  bugs but not asyncio-scheduler interleavings. Adequate for >95%
  of bugs we've seen; rebuilding the asyncio loop wasn't worth the
  scope.

- **No real runner driving.** Tests use stub targets (`make_target()`).
  Driving the actual InstrumentRunner against the dispatcher requires
  per-handler mocking of I/O sinks (canonical_fills writes, state
  persistence, broker calls). That's per-handler scaffolding, not
  generic infrastructure — best added as the operator hits real bugs
  that need it.

- **Capture-replay parity not implemented.** A "run the trace through
  the dispatcher, capture what the subscriber writes, diff against
  the original trace" check would be the Codex X1 live-vs-replay
  parity item. Possible follow-up; not blocking 5/31.

## The four-piece rollout

Three phases shipped today across the activity-multiplication plan:

| Piece | Phase | What it does |
|---|---|---|
| paper_stress | 1 | Multiply real-strategy fires via threshold knob |
| stress_injector | 1 | Fire synthetic brackets on demand for IBKR API stress |
| event_recorder | 2 | Capture every broker event to JSONL during paper trading |
| trace_replay (TraceFakeIB + analyzer) | 2 | Load + replay traces against runner methods, detect race patterns |
| event_dispatcher | 3 | Fire events to subscribers in controlled order, inject chaos |

Together: more activity → more events captured → every incident a
candidate regression test (state OR ordering).

## Activation notes (operator)

For Phase 3 there's no operator activation step — it's a test-time
tool used when writing regression tests against captured traces. The
recorder (Phase 2) is the operator-facing knob; Phase 3 is for the
developer writing the test.

When a new race condition surfaces:
1. Recorder has captured it (Phase 2): trace file in
   `argus_flow/logs/traces/`
2. Find the suspect events in the trace (the 2-3 events around the
   bug timing)
3. Build a stub subscriber that mimics the runner handler under test
4. Dispatch the trace with `ChaosTransform("swap", idx_a, idx_b)`
5. If subscriber behaves correctly under both original and swapped
   ordering → handler is order-tolerant
6. If it diverges → bug, fix, regression test pins the fix
