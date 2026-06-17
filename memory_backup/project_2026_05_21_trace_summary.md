---
name: 2026-05-21 trace summary aggregator (operator review layer)
description: Shipped ops/trace_summary.py — point at a directory of golden-trace JSONLs, get one row per file with event count, invariant violations, top anomaly. --days windowing, --filter-symbol for per-pair view, --json for automation, exit code 0/1/2 mirrors trace_inspect. Closes the "weekly review across N traces" gap that Phase 4's per-trace inspector didn't address. 17 new tests; 222/222 green across all phases + regression neighbors.
type: project
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
Phase 4's `ops/trace_inspect.py` reports a single trace in detail.
Operators reviewing a week of trading need to scan many traces. Until
today, that meant running `trace_inspect` N times manually.

This tool aggregates. One command, one report, one exit code.

## What shipped

### ops/trace_summary.py

`summarize_file(path, symbol_filter=None)` — builds a `FileSummary`
record with: path, event_count, by_kind counts, invariant_violations,
anomalies, top_violation_kind, top_anomaly_kind, first_ts, last_ts,
status (`CLEAN` | `VIOLATIONS` | `EMPTY` | `ERROR`), error.

`summarize_directory(dir, *, days, symbol_filter)` — globs `*.jsonl`,
applies optional days-back mtime filter, returns list of summaries
in lexicographic order (date sort if files are named with ISO dates).

### CLI flags

  - `--dir <path>` — required; directory to scan
  - `--days N` — only files modified within last N days (mtime-based)
  - `--filter-symbol SYM` — keep only events whose `data.symbol`
    matches (case-insensitive). Structural events (connected,
    disconnected) are always kept so the trace shape is preserved.
  - `--json` — emit JSON for piping into automation

### Exit codes

  - 0 — all traces CLEAN
  - 1 — at least one trace has invariant violations
  - 2 — directory missing OR no .jsonl files in directory

### Output format

```
  file                         events  status       inv  anom  top-violation
  ---------------------------  ------  ----------  ----  ----  -------------------------
  cascade_race_20260519.jsonl       9  VIOLATIONS     3     1  NO_CANCEL_AFTER_FILL
  clean_day_20260520.jsonl       4521  CLEAN          0     0  -
  clean_day_20260521.jsonl       3892  CLEAN          0     2  -

totals: 3 traces, 8422 events, 3 invariant violation(s), 3 anomaly pattern(s)
worst-status: VIOLATIONS
```

Per-file row + totals + worst-status bottom line. Status order is
`ERROR > VIOLATIONS > EMPTY > CLEAN`, so the worst-status report
catches the most-serious issue across the directory.

## Tests

17 in `argus_flow/tests/test_trace_summary.py`:

  - `summarize_file` returns VIOLATIONS / CLEAN / EMPTY / ERROR
    depending on input
  - top_anomaly_kind correctly identified (STATUS_REGRESSION on
    cascade fixture)
  - symbol filter keeps matching, drops non-matching, case-insensitive
  - `summarize_directory` finds all .jsonl, skips non-.jsonl
  - days-back filter drops backdated files
  - CLI exits 1 on violations, 0 on all-clean, 2 on missing dir,
    2 on empty dir
  - --json mode produces parseable JSON
  - --filter-symbol flag plumbed through

**222/222 green** across all 6 layers (paper_stress + stress_injector
+ event_recorder + trace_replay + event_dispatcher + trace_invariants
+ trace_parity + preflight + summary) + 9 regression neighbors.

## How this fits the operator workflow

The end-of-day review now compresses to one command:

```powershell
python -m ops.trace_summary --dir argus_flow/logs/traces --days 7
```

  - If exit 0 → no further review needed; ship the week
  - If exit 1 → at least one trace has invariant violations.
    Output names the offending file + top violation kind. Drill into
    that trace with `python -m ops.trace_inspect <path>` for full
    detail.
  - If exit 2 → recorder isn't writing files (env var not set, or
    runner crashed). Fix the recording pipeline first.

Pair with `--filter-symbol`:

```powershell
# Per-pair view across the 10-day pre-reset window
python -m ops.trace_summary --dir argus_flow/logs/traces --days 10 --filter-symbol CAD
python -m ops.trace_summary --dir argus_flow/logs/traces --days 10 --filter-symbol USD
```

This is what catches a per-symbol regression — e.g., CADJPY has
violations 4 days running but USDJPY is clean → the bug is specific
to JPY pairs and not a general regression.

## Cumulative session totals

Through today (5/20 evening + 5/21 morning):

  - **8 helio modules + 5 ops tools shipped**:
    paper_stress, event_recorder, trace_replay (extended), event_dispatcher,
    trace_invariants, trace_parity, stress_injector, trace_inspect,
    trace_parity CLI, preflight_paper_stress, trace_summary
  - **1 runner edit** (GOLDEN_TRACE_PATH wiring)
  - **6 memory phase docs** (paper stress, secondary laptop, golden
    trace, event dispatcher, trace inspect, trace parity, activation
    runbook, trace summary)
  - **139 new tests**
  - **222/222 green** across the touched and neighboring suites

The full operator loop is end-to-end usable without writing Python:

```powershell
# 1. before activation
python -m ops.preflight_paper_stress

# 2. activate (edit config + restart)

# 3. daily review
python -m ops.trace_inspect argus_flow/logs/traces/<today>.jsonl

# 4. weekly review
python -m ops.trace_summary --dir argus_flow/logs/traces --days 7

# 5. before/after fix comparison
python -m ops.trace_parity --a fixture_bug.jsonl --b post_fix.jsonl
```

Nothing else is needed to make the diagnostic infrastructure usable.
Remaining work is **activation** (operator-driven) and **bug
discovery** (data-driven, follows activation).
