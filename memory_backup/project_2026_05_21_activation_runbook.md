---
name: Activation runbook — paper-stress + golden-trace on argus FX
description: Step-by-step operator sequence to flip the switch on the 5-phase rollout. Pre-flight verification, the 4 commands that activate the bot with paper_stress multiplier + GOLDEN_TRACE_PATH recording, first-hour verification, and rollback. Use this when ready to start collecting real data ahead of the 5/31 reset.
type: reference
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
The Phase 1-5 toolkit (paper_stress + stress_injector + event_recorder
+ trace_replay + event_dispatcher + trace_invariants + trace_parity)
is built and tested but **not yet activated**. The runner has been
DOWN since 2026-05-20 evening. This runbook walks the operator through
turning it on with everything wired.

## Pre-conditions

  - argus_flow.runner_unified is currently STOPPED (verify via Task
    Manager — no python process with `runner_unified` argv)
  - ArgusFleetStartup task DISABLED (from 2026-05-20 cleanup)
  - ArgusPreMarketCheck task DISABLED (from 2026-05-20 cleanup)
  - watchdog.ps1 NOT running
  - TWS is up on port 7497 with DUP472829 logged in
  - CADJPY LONG 41,572 orphan still at broker (carry-over from 5/15;
    runner will re-adopt with the broker-truth fix from 5/20 P0 #1)

The 5/31 reset is **10 days out**. Activate now, collect 10 days of
clean post-fix evidence, ship any new fixes that surface, then reset
the equity baseline on 5/31 evening per the existing reset runbook.

## Step 1 — Pre-flight verification

From repo root:

```powershell
$env:IBKR_PORT = "7497"
$env:REAL_MONEY_ENABLED = ""
$env:GOLDEN_TRACE_PATH = "argus_flow/logs/traces/argus_$(Get-Date -Format yyyy-MM-dd).jsonl"

cd C:\Argus\repo
C:\Argus\.venv\Scripts\python.exe -m ops.preflight_paper_stress
```

Expected output ends with `READY — safe to activate.` and exit code 0.

If anything reports `FAIL`, **stop and fix it before continuing**.
Likely failures:
  - `IBKR_PORT == '7497'` fails → the env var wasn't set in this shell
  - `helio.real_money.REAL_MONEY_ENABLED is False` fails → someone
    flipped the module constant; check `git diff helio/real_money.py`
  - `harness end-to-end` fails → import or write-path problem; review
    the detail field

## Step 2 — Decide stress multiplier per pair

The `paper_stress_multiplier` knob lowers entry thresholds to multiply
activity. Default = 1.0 = unchanged. To activate, edit each config
JSON in `argus_flow/configs/`:

```json
{
  ...
  "mtf": {
    "enabled": true,
    ...
    "min_trend_strength": 3.0,
    "min_confidence": 0.5,
    "paper_stress_multiplier": 0.5
  },
  ...
}
```

**Conservative starting point: 0.5** (half-threshold = expect 2-4× more
fires). This is the recommended first activation.

If 0.5 produces too few extra fires after 2-3 days of trading, drop
to 0.3 (the lowest the helper accepts is 0.1 hard floor). DO NOT go
below 0.3 in the first cycle — the strategies were never validated at
that signal strength and may produce garbage.

For the 3 argus FX configs:
  - `usdjpy_mtf_paper_v1.json` — recommended start: 0.5
  - `gbpusd_range_paper_v1.json` — recommended start: 0.5
  - `cadjpy_mtf_paper_v1.json` — already loose (0.3 trend / 0.3 conf);
    multiplier stacks on top → use 0.7 to avoid overdoing it

Re-run preflight after editing to verify configs parse and multipliers
are loaded correctly:

```powershell
C:\Argus\.venv\Scripts\python.exe -m ops.preflight_paper_stress
```

Look for the multiplier value in the config check detail lines.

## Step 3 — Launch the runner

```powershell
$env:IBKR_PORT = "7497"
$env:GOLDEN_TRACE_PATH = "argus_flow/logs/traces/argus_$(Get-Date -Format yyyy-MM-dd).jsonl"
$env:REAL_MONEY_ENABLED = ""

cd C:\Argus\repo
Start-Process -FilePath "C:\Argus\.venv\Scripts\python.exe" `
              -ArgumentList "-m","argus_flow.runner_unified" `
              -WorkingDirectory "C:\Argus\repo" `
              -WindowStyle Hidden
```

Then watch the runner's log for the first 30-60 seconds:

```powershell
Get-Content argus_flow/logs/runner_unified.log -Tail 50 -Wait
```

Expected log lines (in order):

  1. `MTF Trend Strategy enabled (4H/1H/5M, min_conf=0.25)` — note
     **0.25** not 0.5 if multiplier is 0.5 (because `0.5 × 0.5 = 0.25`).
     A multiplier of 0.5 against `min_confidence=0.5` gives effective 0.25.

  2. **`PAPER_STRESS_ACTIVE argus_USDJPY:min_trend_strength base=3.0 mult=0.5 effective=1.5`**
     This is the critical line — confirms the multiplier is active. If
     you don't see this, the multiplier isn't being read.

  3. **`PAPER_STRESS_ACTIVE argus_USDJPY:min_confidence base=0.5 mult=0.5 effective=0.25`**

  4. (similar lines for GBPUSD, CADJPY pairs)

  5. **`GOLDEN_TRACE_RECORDER attached -> argus_flow/logs/traces/argus_2026-05-21.jsonl`**
     This is the critical recording-on confirmation.

If you see the runner come up WITHOUT these lines, kill it and
re-verify. Multiplier or trace path was not picked up.

## Step 4 — First-hour verification

After 60 minutes of trading, verify the loop is producing data:

```powershell
# Check that the trace file is growing
Get-Item argus_flow/logs/traces/argus_$(Get-Date -Format yyyy-MM-dd).jsonl |
  Select-Object Length, LastWriteTime

# Inspect the trace for violations
C:\Argus\.venv\Scripts\python.exe -m ops.trace_inspect `
    argus_flow/logs/traces/argus_$(Get-Date -Format yyyy-MM-dd).jsonl
```

Expected:
  - Trace file size > 1KB (the connected marker alone is ~100B; if you
    haven't grown beyond that, the recorder isn't capturing)
  - Inspect output shows event counts by kind, and `INVARIANT
    VIOLATIONS: none` (or 0 violations from the cascade pattern — the
    5/20 fixes should prevent the violation from recurring)
  - Anomalies may show heuristic patterns (RAPID_REVERSAL might fire
    on normal position changes during the day; that's information not
    necessarily a bug)

If invariant violations DO appear:
  1. STOP the runner
  2. Save the trace file with a timestamp suffix (e.g. add a date)
  3. Open an investigation — the violation summary names the bug
     pattern + order_id + index in trace
  4. Either ship a fix + restart, OR fall back to multiplier 1.0
     (= unchanged behavior) and continue while debugging

## Optional — Stress injector parallel

The injector runs separately, fires synthetic brackets on EURUSD
(NOT in survivor roster), exercises broker-side execution path
behavior. Useful for getting "extra" trades to learn from.

```powershell
$env:STRESS_INJECT_OK = "1"
$env:IBKR_PORT = "7497"
C:\Argus\.venv\Scripts\python.exe -m ops.stress_injector `
    --loop 10 --cooldown 90 --chaos cancel_during_fill
```

Logs to `argus_flow/logs/stress_injector.jsonl` (different file from
the runner's trace). Doesn't conflict with the runner because it uses
a different clientId (250) and a different test instrument.

## Daily monitoring during the 10-day window

End of each US trading day:

```powershell
# 1. Inspect the day's trace
C:\Argus\.venv\Scripts\python.exe -m ops.trace_inspect `
    argus_flow/logs/traces/argus_$(Get-Date -Format yyyy-MM-dd).jsonl

# 2. Compare against previous day's trace for behavior changes
C:\Argus\.venv\Scripts\python.exe -m ops.trace_parity `
    --a argus_flow/logs/traces/argus_$((Get-Date).AddDays(-1).ToString('yyyy-MM-dd')).jsonl `
    --b argus_flow/logs/traces/argus_$(Get-Date -Format yyyy-MM-dd).jsonl
# (Cross-day parity is informational; events SHOULD differ because
# trades are different. Use this when comparing pre/post fix on
# replays of the SAME captured trace.)
```

Track activity volume: expect roughly 2-4× the pre-multiplier
baseline. If activity is unchanged, the multiplier didn't take effect
— recheck logs for `PAPER_STRESS_ACTIVE` warnings.

## Rollback

If anything goes wrong, roll back is one-step:

```powershell
# Stop the runner
Get-Process -Name python | Where-Object {
  $_.CommandLine -like "*runner_unified*"
} | Stop-Process -Force

# Optional: revert config edits to set multiplier back to 1.0
# (or just delete the paper_stress_multiplier field — default is 1.0)

# Restart WITHOUT the trace recorder env var
$env:GOLDEN_TRACE_PATH = $null
Start-Process -FilePath "C:\Argus\.venv\Scripts\python.exe" `
              -ArgumentList "-m","argus_flow.runner_unified" `
              -WorkingDirectory "C:\Argus\repo" `
              -WindowStyle Hidden
```

Multiplier and recorder are fully independent. Either can be turned
off without affecting the other.

## Common pitfalls

  - **Forgot to set env vars in the new shell session** — they don't
    persist. The Start-Process command launches a new process that
    INHERITS the current shell's env, so set env vars in the SAME
    shell before launching.
  - **GOLDEN_TRACE_PATH parent doesn't exist** — preflight catches
    this. The recorder calls `mkdir(parents=True, exist_ok=True)` so
    it'll create the dir, but a typo in the path gives you a trace
    file in a surprising location.
  - **Multiplier looks active in logs but no extra trades fire** —
    the strategy may genuinely have no qualifying signals during the
    observation window. Cross-check by looking at `argus_flow/logs/
    signal_log.csv` for evaluation counts vs. fire counts. If
    evaluations are happening but no fires, threshold is still too
    high → lower multiplier further.
  - **Trace inspect reports violations from the FIRST event** — the
    initial `connected` marker is sometimes flagged by the lifecycle
    invariant on the first run. Re-inspect after 100+ events; spurious
    boot-time noise should not recur.

## Decision tree for the 5/31 cutover

Whatever happens during the 10-day window dictates the 5/31 plan:

  - **Zero violations, normal activity 2-4× baseline** → proceed with
    5/31 reset as planned. argus FX retires; survivor cohort takes
    over post-reset.
  - **Violations caught, fixed, regression tests added** → proceed
    with 5/31 reset; the value of this exercise is exactly this loop.
  - **Multiplier produces too-noisy trades / strategy degrades** →
    revert to 1.0 multiplier; still ship the trace recorder + tooling
    as permanent infra; 5/31 reset proceeds.
  - **Critical new bug surfaces that needs more than 10 days to fix**
     → defer 5/31 reset until fix lands. Document the deferral
     decision in memory.

The doctrine remains: **activity = bugs found = fewer bugs at real
money time**. The 10-day window is for finding them, not for proving
edge.
