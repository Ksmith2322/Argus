---
name: 2026-05-20 paper-stress rollout (Phase 1 of activity-multiplication)
description: Shipped helio/paper_stress.py (entry-threshold multiplier for argus FX + pead) + ops/stress_injector.py (paper-only synthetic signal injector with cancel-during-fill / double-submit / disconnect chaos modes) + PC2 deployment runbook. 27 new tests, 110/110 across regression neighbors. Phase 1 of the user's "stress the system, multiply paper activity, surface bugs faster" reframe.
type: project
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
After the 5/20 operational audit shipped 9 fixes for bugs that had been
firing live since 5/12, the operator reframed the question: panel of
5 agents had said "slow down + apply rigor"; operator pushed back
that the answer is "more time, more trades, more data" and asked
specifically about simulation or additional brokers.

The operator's frame is correct for **execution-proving** (different
goal from edge-proving). Bugs surface via activity. Three days of
trading exposed 15+ bugs; static audit missed them all. Volume
multiplication is the right tool for that goal.

This session shipped Phase 1 of the activity-multiplication plan.

## What shipped

### 1. helio/paper_stress.py — entry-threshold multiplier

Single helper function `apply(base_value, multiplier, *, strategy, knob)`
that scales a strategy's entry threshold. `mult<1` = looser = more
activity. Four safety gates inside `_is_paper()`:
1. IBKR_PORT == "7497"
2. REAL_MONEY_ENABLED env unset
3. Clamps multiplier to [0.1, 10.0]
4. Logs WARNING `PAPER_STRESS_ACTIVE` on every active call

Wired into:
- `argus_flow/runner_unified.py` MTF setup block — scales both
  `min_trend_strength` and `min_confidence` from `mtf_cfg.paper_stress_multiplier`
- `forge/pead/runner.py` — scales all three pead filters
  (`surprise_pct_min`, `volume_ratio_min`, `gap_pct_min`) from
  `PARAMS.paper_stress_multiplier`

Operator activates by editing the config (e.g. add
`"paper_stress_multiplier": 0.5` inside `mtf` block of
`usdjpy_mtf_paper_v1.json`) + restarting the runner. Default 1.0 =
no behavior change. Reversible.

Tests: 13 in `argus_flow/tests/test_paper_stress.py` (refuses on live
port, refuses with REAL_MONEY_ENABLED set, clamps out-of-range
multipliers, source-level wiring smoke tests for both runners).

### 2. ops/stress_injector.py — on-demand synthetic signal injector

Standalone tool that connects to paper TWS, submits a tiny bracket on
a paper-only test instrument (EURUSD/AUDUSD, deliberately not in
the argus survivor roster), watches the full lifecycle, logs every
event to `argus_flow/logs/stress_injector.jsonl`.

Four import-time safety locks (all must pass or `RuntimeError` at
module load):
1. `IBKR_PORT == "7497"`
2. `os.environ["REAL_MONEY_ENABLED"]` unset/false
3. `os.environ["STRESS_INJECT_OK"] == "1"` (explicit operator opt-in)
4. `helio.real_money.REAL_MONEY_ENABLED` is False (module constant)

ClientId 250 dedicated; range 250-299 reserved for stress harness;
range 300-399 reserved for PC2.

Three chaos modes for race-condition exposure:
- `cancel_during_fill` — submit then cancel within 200ms (the 5/19
  cascade race condition)
- `double_submit` — submit the same order twice
- `disconnect_after_submit` — disconnect immediately after placeOrder,
  reconnect to test recovery

Race detectors built in: `ENTRY_FILLED_DURING_CANCEL` and
`BOTH_BRACKET_LEGS_FILLED`. Forces-close orphan positions on cleanup.

CLI: `--single`, `--loop N --cooldown S`, `--chaos MODE`. Refuses to
load if any safety lock fails (no `--force` override on the locks
— deliberate).

Tests: 14 in `argus_flow/tests/test_stress_injector.py` covering all
four locks individually + composite, instrument allowlist isolation
from argus survivors, clientId range non-collision, event log
roundtrip + path-correctness, chaos modes registered.

Added `ops/stress_injector.py` to `ALLOWED_DIRECT_PLACEORDER` in
`argus_flow/tests/test_static_safety_invariants.py` with rationale
comment pointing at the four locks.

### 3. PC2 deployment runbook

Full operational doc at
`project_2026_05_20_secondary_laptop_runbook.md`. Covers: IBKR paper
account #2, TWS install on PC2, clientId allocation (PC1 1-299, PC2
300-399), repo via git push/pull NOT shared mount, env setup,
daily flow, end-of-day log push, failure scenarios, bootstrapping
checklist.

PC2 is purely paper. Hard rule. Runs stress_injector continuously +
threshold-multiplied parameter variations. Results flow PC2 → git
push → PC1 → git pull.

## Test totals

- 13 new in `test_paper_stress.py`
- 14 new in `test_stress_injector.py` (12 initial + 2 chaos)
- 1 updated allowlist in `test_static_safety_invariants.py`

**110/110 green** across the rollout + regression neighbors:
test_paper_stress, test_stress_injector,
test_exit_broker_truth_guard, test_audit_fixes_20260520, test_pead,
test_real_money_boundary, test_killed_strategy_invariant,
test_static_safety_invariants, test_sunset_roster.

## What's deferred to next session

- **Golden-trace recorder + replay harness**. Bigger build — needs
  ib_insync event capture + deterministic replay loop. Will
  retroactively convert the 5/12 entry race + 5/19 cascade into
  100%-reliable regression tests. Plan: ~1 day to build the recorder,
  then retrofit existing logs into traces.
- **Chaos for the runners** (not just the injector). The injector's
  chaos modes test what happens when WE behave badly. Chaos against
  the real runners (argus + survivors) needs ib_insync monkey-patching
  during paper trading — separate phase.
- **Wiring the multiplier into more strategies**. Today wired only
  into the two strategies with natural signal-strength knobs (argus
  FX MTF + pead). Time-window strategies (gld_pm_long, nq_overnight)
  and regime-binary (spy_trend_follower) don't have a multiplier-
  shape knob — different mechanism (widen windows, loosen regime
  gates) needed and not yet built.

## The doctrine

The user reframed correctly: bugs surface via activity. The 5/20
audit caught 9 fixes because the runner had been trading for 8 days.
The audit before that caught 5 because the runner had been trading
for 19 days. The pattern repeats — code-review finds the easy bugs,
live exercise finds the rest.

Phase 1 of activity multiplication is now shipped. The operator can
now (a) lower thresholds on argus FX + pead via config edit + runner
restart, (b) fire `python -m ops.stress_injector --loop 50 --chaos
cancel_during_fill` to deliberately exercise the race-condition
codepaths.

Bug discovery rate should accelerate. Anything found feeds back into
the regression test suite. PC2 (when stood up) doubles the rate
without doubling real-money risk because PC2 is paper-only by
construction.

Real-money posture stays at "months out, post-5/31 reset + 30-day
clean evidence + bootstrap CI >1.20 lower bound". Activity
multiplication does NOT shorten the real-money timeline (different
goal). It only accelerates execution-bug discovery, which is a
prerequisite to ever going real.
