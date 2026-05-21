---
name: 2026-05-21 forge recorder wiring + per-strategy scorecard
description: Two pieces. (1) Wired golden-trace recorder into helio.ibkr_execution.connect() via new attach_via_env helper — all 17 forge strategies + Greek scanners now record automatically when GOLDEN_TRACE_PATH env var is set; argus_flow's existing wiring unchanged. (2) Shipped helio.strategy_scorecard + ops.strategy_scorecard CLI — daily GREEN/YELLOW/RED per strategy combining trace invariants + canonical_fills count + heartbeat freshness + entry/exit balance. 18 new tests; 240/240 green across the full toolkit.
type: project
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
The activity-multiplication / trace toolkit was argus-only until
today. Forge strategies (the 5 survivors plus all archived runners)
trade through helio.ibkr_execution.connect() and didn't have the
recorder wired. Operator review was per-trace; there was no fleet-
wide verdict. Both gaps closed in this batch.

## Piece 1 — forge recorder wiring

### helio/event_recorder.py — new attach_via_env helper

```python
def attach_via_env(ib, env_var="GOLDEN_TRACE_PATH"):
    """Read trace path from env var; attach recorder if set; otherwise no-op.
    Returns the EventRecorder OR None. Errors are logged and swallowed."""
```

The crucial invariant: trace recording must NEVER take down a strategy.
Failures are logged at WARNING and the strategy continues. The argus
runner has the same contract.

### helio/ibkr_execution.py — connect() now calls attach_via_env

After successful TWS connect, `connect()` calls `attach_via_env(ib)`
which is a no-op if `GOLDEN_TRACE_PATH` is unset. If set, an
EventRecorder is attached to the IB instance and writes a JSONL trace
of every broker event.

Affects every caller of `helio.ibkr_execution.connect()`:
  - 17 forge strategy runners
  - apollo / hermes / titan / ares (Greek family)
  - Any other helper that uses this connect path

### Operator activation for forge

Same env var as argus, set per-runner at launch:

```powershell
# Per-strategy launch — each gets its own trace file
$env:GOLDEN_TRACE_PATH = "argus_flow/logs/traces/forge_gld_pm_long_$(Get-Date -Format yyyy-MM-dd).jsonl"
$env:IBKR_PORT = "7497"
python -m forge.gld_pm_long.runner --loop
```

Wake-and-sleep forge runners attach a fresh recorder on each connect
cycle. Each cycle appends to the SAME trace file (the EventRecorder
opens in append mode). Each attach writes a `connected` marker so the
file has multiple boot markers per session — harmless, just diagnostic
noise. Operator can grep for `recorder_attached: True` to count
connect cycles.

## Piece 2 — strategy verdict scorecard

### helio/strategy_scorecard.py

`score_strategy(strategy, allocation, fills_path, trace_path, heartbeat_path, today, now)`
→ `StrategyScore(status, reason, metrics)`.

Four signals combined:
  1. **Trace invariants** — any invariant violation → RED
  2. **Activity vs allocation** — allocation > 0 + no fills + no fresh
     heartbeat → RED (process likely dead)
  3. **Heartbeat freshness** — > 4h old → RED
  4. **EOD entry/exit balance** — |entries − exits| > 1 → YELLOW

Verdict priority: RED checks first (any one trips), then YELLOW checks
collected, then GREEN if nothing flagged. Same inputs always yield
the same verdict — no time-of-day or random branching. Sub-second.

`score_fleet([(strat, alloc), ...], fills_path, trace_dir, heartbeat_dir)`
→ list of scores. Auto-discovers per-strategy trace files by convention
`<trace_dir>/<strategy>_<today>.jsonl` and heartbeat files by
`<heartbeat_dir>/<strategy>/heartbeat.json`. Missing files are
tolerated.

### ops/strategy_scorecard.py CLI

```powershell
# Score the whole fleet (auto-discover from allocation_factors.json)
python -m ops.strategy_scorecard

# Score one strategy on demand
python -m ops.strategy_scorecard --strategy forge_gld_pm_long --allocation 0.5

# JSON for automation
python -m ops.strategy_scorecard --json
```

Output (human mode):
```
     strategy           status   reason
  -  -----------------  -------  ----------------------------------------
  !  forge_gld_pm_long  RED      allocated but no fills today and no fresh heartbeat
  ~  forge_pead         YELLOW   no fills today (low-frequency strategy?)
  +  forge_xs_momentum  GREEN    all checks passed

totals: 1 GREEN, 1 YELLOW, 1 RED
```

`+` = GREEN, `~` = YELLOW, `!` = RED. Mark column makes scanning fast.

Exit codes:
  0 = all GREEN
  1 = at least one YELLOW or RED
  2 = allocation_factors.json missing OR no allocated strategies

### Smoke against current state

Right now allocation_factors.json has **all 27 strategies at factor=0.0**
(the pre-5/31 sunset state from 2026-05-20). So the fleet-mode CLI
returns exit 2 with "no allocations found" — correct behavior. After
the 5/31 reset, the 5 survivors get 0.5+ and the scorecard becomes
actionable.

Single-strategy mode (`--strategy forge_gld_pm_long --allocation 0.5`)
runs against current artifacts and correctly identifies:
```
forge_gld_pm_long  RED  allocated but no fills today and no fresh heartbeat — process likely dead
```
which is exactly true — the runner has been down since 5/20.

## Tests

18 in `argus_flow/tests/test_strategy_scorecard.py`:
  - 9 score_strategy unit tests (GREEN / RED-on-invariant / RED-on-silent
    / RED-on-stale-heartbeat / GREEN-on-balanced / YELLOW-imbalance /
    YELLOW-quiet / fills-filter-by-strategy / fills-filter-by-date)
  - 2 score_fleet tests (returns one per strategy / discovers traces
    by convention)
  - 4 CLI tests (single mode / single JSON / exit 2 missing allocations /
    load_allocations filters positive)
  - 3 attach_via_env / connect wiring tests (returns None when unset /
    attaches when set / source-level wiring verified)

**240/240 green** across the cumulative toolkit:
strategy_scorecard, trace_summary, preflight, trace_parity,
trace_invariants, event_dispatcher, golden_trace, paper_stress,
stress_injector, exit_broker_truth_guard, audit_fixes_20260520,
pead, real_money_boundary, killed_strategy_invariant,
static_safety_invariants, sunset_roster.

## Cumulative session totals (5/20 + 5/21)

The activity-multiplication rollout now spans:

| Piece | Layer |
|---|---|
| helio/paper_stress.py | strategy-side activity multiplier |
| ops/stress_injector.py | synthetic signal generator |
| helio/event_recorder.py + attach_via_env | broker event capture (argus + forge) |
| helio/trace_replay.py + TraceFakeIB + find_anomalies | state-machine regression |
| helio/event_dispatcher.py + ChaosTransform | callback-ordering regression |
| helio/trace_invariants.py | 4 hard invariants |
| helio/trace_parity.py | live-vs-replay parity (Codex X1) |
| helio/strategy_scorecard.py | per-strategy daily verdict |
| ops/trace_inspect.py | single-trace diagnostic |
| ops/trace_parity.py | trace comparison CLI |
| ops/preflight_paper_stress.py | pre-activation verifier |
| ops/trace_summary.py | multi-trace aggregator |
| ops/strategy_scorecard.py | fleet daily scorecard |
| argus_flow/tests/fixtures/cascade_race_20260519.jsonl | first incident fixture |
| memory: paper_stress / golden_trace / event_dispatcher / trace_inspect / trace_parity / activation_runbook / secondary_laptop / trace_summary / scorecard | 9 phase docs + 2 runbooks |

  - **157 new tests**
  - **240/240 green** across touched + neighbor suites
  - **2 module edits** to existing files (runner_unified.py wiring,
    ibkr_execution.py wiring)

## What the operator sees end of each day

```powershell
# 1. Fleet scorecard — one line per strategy
python -m ops.strategy_scorecard

# 2. If any RED: drill into the specific trace
python -m ops.trace_inspect argus_flow/logs/traces/<strategy>_<date>.jsonl

# 3. Compare yesterday vs today behavior for a strategy
python -m ops.trace_parity \
    --a argus_flow/logs/traces/<strategy>_<yesterday>.jsonl \
    --b argus_flow/logs/traces/<strategy>_<today>.jsonl

# 4. Weekly trend across all traces
python -m ops.trace_summary --dir argus_flow/logs/traces --days 7
```

Four commands, complete coverage of the day's state. Exit codes drive
automation; JSON modes feed automation tools.

The diagnostic loop is now genuinely **operator-complete**. The
remaining work is **operator-driven activation** + **bug discovery
from real captured data** — both blocked on the operator deciding to
flip the switch.
