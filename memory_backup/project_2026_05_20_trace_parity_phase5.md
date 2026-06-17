---
name: 2026-05-20 trace parity (Phase 5, Codex X1 closure)
description: Shipped helio/trace_parity.py (compare_traces field-by-field with configurable ignore lists, subscriber_to_trace helper for roundtrip self-tests) + ops/trace_parity.py CLI. Closes the Codex X1 "live-vs-replay parity" deferred audit item from 5/18. Self-parity test confirms recorder+dispatcher+reconstruction roundtrip is lossless modulo timing fields. 20 new tests; 189/189 green across all 5 rollout phases.
type: project
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
The Codex X1 finding from the 5/18 audit ("live-vs-replay parity") was
deferred because the plumbing didn't exist yet. Phases 2-4 of the
activity-multiplication rollout built it. Phase 5 is the small
composition layer that closes the loop.

## What shipped

### helio/trace_parity.py — field-level trace comparison

`compare_traces(a, b, *, ignore_fields)` walks two JSONL traces
in lock-step and returns differences:

  - **MISSING_IN_B** — event present in A at index i, B is shorter
  - **EXTRA_IN_B** — event present in B at index i, A is shorter
  - **EVENT_KIND_MISMATCH** — same index, different event kind
  - **VALUE_MISMATCH** — same kind, different field value

Field comparison is configurable via `ignore_fields`. Default ignores
`ts` and `monotonic_ms` because those will always differ between any
two real captures (wall-clock + monotonic counters reset on each
recording).

`subscriber_to_trace(sub)` converts a `RecordingSubscriber`'s call log
back to JSONL form for round-trip self-tests against the original.

### ops/trace_parity.py — operator CLI

  ```powershell
  python -m ops.trace_parity --a live.jsonl --b replay.jsonl
  python -m ops.trace_parity --a t1.jsonl --b t2.jsonl --json
  python -m ops.trace_parity --a t1.jsonl --b t2.jsonl \
      --ignore-fields ts,monotonic_ms,perm_id
  ```

Exit 0 = clean, 1 = divergences, 2 = missing file. Mirrors the
trace_inspect contract.

## Three uses

  1. **Self-test the harness.** Capture a live trace T1. Dispatch T1
     through the Phase 3 harness to a RecordingSubscriber. Convert
     subscriber → trace via subscriber_to_trace. compare_traces should
     report empty. Any diff = recorder, dispatcher, or reconstruction
     bug. Verified for the 5/19 cascade fixture.

  2. **Code-change validation.** Run trace T1 against commit A and
     commit B; diff the outputs. Differences = behavioral changes
     between commits, surfaced deterministically. Useful for git
     bisection of execution-path regressions.

  3. **Cross-node comparison.** PC1 and PC2 captured the same scenario
     (after PC2 is stood up). Compare the two traces. Divergences =
     environment-specific bugs (timing differences, broker version,
     network ordering).

## Tests

20 in `argus_flow/tests/test_trace_parity.py`:

  - 12 compare_traces unit tests (identity / ts ignored / value
    mismatch / event-kind mismatch / missing in B / extra in B /
    ignore field override / nested value / two diffs / a-only field /
    b-only field / monotonic_ms ignored)
  - 2 self-parity smoke tests: cascade fixture roundtrips clean; with
    chaos swap, divergence is detected
  - 1 subscriber_to_trace coverage of all 7 event kinds
  - 5 CLI: exit 0 on match / exit 1 on diff / exit 2 on missing file /
    ignore-fields override flag / --json mode valid

**189/189 green** across all 5 phases + 9 regression neighbors.

## CLI smoke

Identical-files smoke:
```text
$ python -m ops.trace_parity --a cascade_race_20260519.jsonl --b copy.jsonl
A: argus_flow/tests/fixtures/cascade_race_20260519.jsonl
B: /tmp/copy.jsonl
ignore_fields: ts,monotonic_ms

PARITY: clean — traces match
```

## The five-phase summary

| Phase | Tool | Operator interaction |
|---|---|---|
| 1 | paper_stress (`mtf.paper_stress_multiplier: 0.5` config edit) | Edit config + restart runner |
| 1 | ops/stress_injector (`python -m ops.stress_injector --loop 10`) | Run command, watch logs |
| 2 | event_recorder (`GOLDEN_TRACE_PATH` env var) | Set env var, start runner |
| 2 | trace_replay / find_anomalies | Test-time, used by ops/trace_inspect |
| 3 | event_dispatcher + ChaosTransform | Test-time, dev tool |
| 4 | trace_invariants + 3 new anomaly patterns | Used by ops/trace_inspect |
| 4 | ops/trace_inspect (`python -m ops.trace_inspect <trace>`) | One-shot diagnostic |
| 5 | trace_parity (`python -m ops.trace_parity --a x --b y`) | Compare any two traces |

**Total today (5 phases):**
  - 7 new helio modules (paper_stress, event_recorder, trace_replay [extended ×2], event_dispatcher, trace_invariants, trace_parity)
  - 3 new ops tools (stress_injector, trace_inspect, trace_parity)
  - 1 fixture (cascade_race_20260519.jsonl)
  - 1 runner edit (GOLDEN_TRACE_PATH env var wiring)
  - 2 memory runbooks (paper stress rollout + secondary laptop)
  - **5 memory phase docs**
  - **189 tests** across all touched files

## Codex audit scoreboard update

Codex X1 (live-vs-replay parity): **CLOSED**. Plumbing in place,
self-parity test green, CLI usable from the shell.

Remaining Codex items still in the deferred list:
  - X7 (order lifecycle audit) — partial via trace_invariants
    NO_FILL_WITHOUT_ORDER + NO_CANCEL_AFTER_FILL; full audit would
    require driving a real runner through the dispatcher with all I/O
    sinks mocked
  - Per-handler regression tests — per-bug work, not generic infra
  - Asyncio-loop simulation — high cost vs. yield, defer

## Operator workflow now end-to-end

The complete loop, from "operator launches the bot" to "operator
catches and ships a fix for a captured race":

```powershell
# Boot the runner with recording + paper stress on
$env:IBKR_PORT = "7497"
$env:GOLDEN_TRACE_PATH = "argus_flow/logs/traces/$(Get-Date -Format yyyy-MM-dd).jsonl"
# (also: edit argus_flow/configs/*.json to set paper_stress_multiplier: 0.5)
python -m argus_flow.runner_unified

# Optional: extra synthetic activity via the stress injector
$env:STRESS_INJECT_OK = "1"
python -m ops.stress_injector --loop 20 --cooldown 90 --chaos cancel_during_fill

# At end of day, inspect captured trace
python -m ops.trace_inspect argus_flow/logs/traces/<date>.jsonl
# → exit 1 if invariant violation; exit 0 if clean
# → human or --json output

# If a bug was caught and a fix shipped, replay the original trace
# through the new code to verify behavior changed:
python -m ops.trace_parity --a fixture_bug.jsonl --b post_fix.jsonl

# Self-parity sanity check after touching the harness itself:
# (any divergence here = bug in recorder/dispatcher/reconstruct, not
# in the bot's actual execution code)
```

The user's original goal — "stress the system, multiply activity,
surface bugs faster" — is now backed by an end-to-end operator-facing
loop. The 5/31 reset window can be used to gather real captured traces
from PC1 + PC2 (once stood up), feed them through the inspector +
parity tools, and ship fixes against permanent regression fixtures.

Nothing else in this rollout requires further building right now. The
remaining work is **operational activation** — actually pointing the
operator at the tools and running them against the live paper account.
