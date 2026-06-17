---
name: 2026-05-12 Capital Ladder + Kills Session
description: Built end-to-end capital ladder gate, fixed 4 correctness bugs, killed forge_vix_intraday, finalized forge_spy_mean_rev kill, patched watchdog pause-on-KILL_SWITCH. Sets the path to mechanical SMOKE_5K eligibility by ~5/26.
type: project
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
## What was built / fixed (committed 2026-05-12)

**Capital ladder gate (the SHIPPED feature):**
- `helio/capital_ladder.py` — evaluates 6 stages (RESEARCH_FREEZE → LIVE_100K) against ROI proof + ops report + correlation report + manual evidence. Returns highest passed stage. Fails closed to $0 on missing evidence.
- `helio/ops_reliability.py` — reduces noisy runtime logs (phantom annotations, canonical/reconciliation reports, broker_drift_state, risk_oversight, watchdog log) into the small set of fields the ladder gates on. Streak math via JSONL history. 36h staleness check for critical sources, 6h for watchdog log.
- `argus_flow/configs/capital_ladder.json` — 6-stage policy, strategy→cluster map, ops gates per stage.
- `argus_flow/configs/phantom_trade_resolutions.json` — dated resolutions for multi_orb + nq_london_close phantoms (annotations stay immutable; resolutions mean those rows aren't blocking).
- `helio/real_money.py` — wired to ladder via `_capital_ladder_approved_capital_usd()`. Raises `capital_ladder_blocked` when approved <= 0; `capital_ladder_oversize` per-order; `capital_ladder_gross_oversize` for aggregate (sums allowlisted strategies' open notional from heartbeats).
- `argus_flow/ops/managed_truth_loop.py` — daily refresh now runs `helio.ops_reliability` + `helio.capital_ladder` so streak math advances and the ladder report stays fresh.
- `ops/run_capital_ladder_gate.ps1` — operator wrapper. `-RequireLiveCapital` exits non-zero if approved=0.
- `docs/BOT_TO_100K_ROADMAP_2026.md` — the spec the config implements.

**Low-sample IR (SMOKE_N=10):**
- `helio/roi_proof_core.py` — added `SMOKE_N=10` constant + `min_n` parameter to `information_ratio` and `sortino_ratio`. CONTINUATION_N=30 unchanged (strict path stays sacred).
- `ops/audit/run_strategy_roi_proof.py` — writes both `information_ratio` (strict) and `information_ratio_low_sample` columns.
- `helio/capital_ladder.py` — `_effective_live_ir()` falls back to low-sample IR ONLY when stage criteria sets `allow_low_sample_ir: true`. LIVE_10K+ stays strict.
- SMOKE_5K config aligned with roadmap doc (removed off-spec `max_paper_live_ir_delta` + `min_capacity_test_usd`; spy_coverage relaxed to 0.5).

**4 correctness bugs fixed:**
1. `ops/broker_drift_aggregator.py:_realized_pnl_since` — was called but never defined; would crash `main()` on every run. Now a thin wrapper around `_expected_equity_from_fills(0.0, since_ts)`.
2. `helio/ops_reliability.py:_detect_kill_switch_tested` — substring-matched "PASS" anywhere in evidence text, would accept a WARN drill as passing because "PASS" appears in the descriptive note. Now reads structured `pass`/`status` fields via `_drill_record_passed`.
3. UTF-8 BOM in PowerShell drill output broke `json.loads`. Detector now strips BOM before parsing.
4. `ops/drill_harness.ps1` heartbeat check looked at non-existent `runner_unified_heartbeat.json`. Argus uses per-pair heartbeats. Fixed to check usdjpy/gbpusd/cadjpy heartbeats, drill passes if any one is fresh.

**Watchdog `KILL_SWITCH` behavior:**
- `ops/watchdog_managed.ps1` used to `exit 0` when KILL_SWITCH appeared. Every kill drill killed the watchdog and `watchdog_health_check`'s 10min-stale-threshold + 30min-cooldown left ~10min monitoring blindness.
- Now PAUSES the loop on KILL_SWITCH (logs the pause once, alerts yellow), CONTINUES the loop without restart attempts. Resumes when flag clears (logs + green alert). Zero monitoring gap.

## Strategy kills (3 finalized 5/12)

`argus_flow/configs/allocation_factors.json v3_2026-05-12`:
- forge_multi_orb: 0.0 (killed 5/7)
- forge_spy_mean_rev: 0.0 (killed 4/30, rework path CLOSED 5/12 — future SPY mean-rev = v2 from scratch, not v1 rework)
- forge_vix_intraday: 0.0 (killed 5/12 — n=61, drift -175%, IR=-4.36 vs SPY)

Defense in depth applied to all three:
- `argus_flow/configs/allocation_factors.json` factor=0.0
- `ops/start_all_runners.ps1` entry commented
- `helio/fleet_monitor.py` `no_restart: True` (works even if `--no-restart` CLI flag removed)

vix_intraday open position: 283 UVXY shares (~$10.5K notional, +$401 unrealized at kill time). **Initially assumed the TWS-side OCO bracket would manage exit — that assumption was WRONG.** When checked next session, no open orders existed on UVXY; the bracket had been cancelled by the emergency-shutdown path (or never persistent). Position was NAKED for several hours. Manually flattened 2026-05-12 15:02 UTC at $37.68 = +$515 realized. The broker drift detector correctly tripped at -4.56% during the gap because canonical_fills couldn't see the position. **Lesson: kills with open positions require an explicit flatten step, not reliance on OCO bracket survival.** Use `ops/flatten_orphan.py` (or inline if client_id 200 is busy).

## Operator actions completed in-session

- Restarted ArgusWatchdog scheduled task (was dead since 4/20)
- Restarted managed_truth_loop with new daily wiring (PIDs 17216 + 46360 as of 07:29 UTC; will be different on future restarts)
- TWS disclaimer accepted by user (operator step — required every time TWS restarts in paper mode)
- Kill drill ran successfully — `kill_switch_drill.json` shows `pass: true, status: PASS`

## Ladder state at session end (2026-05-12 13:15 UTC)

```
approved_capital_usd: 0 (RESEARCH_FREEZE)
remaining blockers (ALL time-only):
  ops.clean_ops_days 0 < 14            -> 5/26
  ops.broker_reconcile_passed_days 1 < 7 -> 5/19
  ops.tws_cascades_30d 16 > 0           -> 5/14-5/20 (events roll off rolling window)
  ops.open_p0_incidents 1 > 0           -> clears with tws_cascades
```

**Earliest mechanical SMOKE_5K eligibility: 2026-05-26** assuming nothing breaks the streaks.

## Tests added (10 new, 54 total in new modules passing)

- `argus_flow/tests/test_capital_ladder.py` — 7 tests (boundaries, missing evidence, smoke vs strict IR fallback)
- `argus_flow/tests/test_ops_reliability.py` — 13 tests (phantom counter, BOM strip, watchdog staleness, drill_record_passed structured vs plain log)
- `argus_flow/tests/test_reconciliation_logical_dedup.py` — 6 tests (live-vs-backfill twin handling, NQ→MNQ symbol normalization)
- `argus_flow/tests/test_real_money_boundary.py` — added gross-notional + ladder-cap tests

## Recurring operator gotchas learned today

- **TWS disclaimer must be accepted after EVERY TWS restart in paper mode.** Symptom: all runners (different client IDs) get Error 10141 "Paper trading disclaimer must first be accepted" + "clientId X already in use" (the latter is misleading — it's the disclaimer, not the slot). Fix: bring TWS window to focus, click I Accept.
- **Hourly probe lag.** `tws_health_probe` runs hourly via managed_truth_loop. If TWS recovers between probes, dashboard banner shows stale "TWS UNREACHABLE" for up to 60min. Run `python -m ops.tws_health_probe` manually to clear.
- **Kill drill cascades the runner.** Argus runner_unified emergency-shuts-down on KILL_SWITCH; fleet auto-respawns it; new instance may hit stale TWS client_id slot (Error 326). Solution: paper-trading-only impact, but plan drills outside trading hours.

## What's left to do

**Time only (no action needed):**
- Streak math: clean_ops_days, broker_reconcile_passed_days
- 18 historical TWS cascade events rolling off 30-day window

**Deferred (post-SMOKE_5K consideration):**
- Dashboard hourly-probe lag — move to every-cycle if it becomes habit-forming annoyance

## Late-afternoon addendum: 4-layer silent-gate cascade exposed (2026-05-12 15:00-16:30 UTC)

Original diagnostic question: "why is cuebanks silent?" Memory had classified cuebanks (and mamba/tori) as **ENVIRONMENTALLY SILENT** — assumption that the market wasn't producing setups. Reality: **every single signal was being silently dropped by a 4-layer cascade of bugs that have been in place since ~5/07.**

### The cascade

1. **`forge/logging_setup.py:69` had `logger.propagate = False`.** Combined with `helio.signal_executor` using `logging.getLogger(__name__)`, every error from `submit_signal` went to a logger with no file handler attached to the runner's log. Drop reasons were invisible.

2. **`helio/ibkr_execution.submit_bracket:319` rejected uppercase direction strings.** cuebanks, mamba, tori all emit `"LONG"`/`"SHORT"` from their confluence/score code. multi_orb, fomc_drift emit lowercase. Caught: `bad direction: 'SHORT'`. **100% of cuebanks signals raised this exception.**

3. **OVERSIZED_ORDER guard used `size × price` for futures notional.** 2 MYM contracts at index 49,862 reported as $99,724 (stock-math) when actual USD notional is $49,862 (with $0.50/pt multiplier). Cuebanks/mamba/tori (and any future micro futures strategy) tripped the 50%-NetLiq guard on every order.

4. **OVERSIZED_ORDER threshold was 50% across all asset classes**, but `fleet_sizing.json` allows futures at 2.0× anchor. Even with correct multiplier math, legitimate 2-contract futures positions still got rejected. **Every futures order via `helio.signal_executor` since the 4/27 fleet_sizing v8 bump has been 100% silently rejected at this gate.**

### The fixes

1. `forge/logging_setup.py` — `setup_logging()` now attaches the runner's file handler to `helio.signal_executor` and `helio.ibkr_execution` loggers in the same process.
2. `helio/ibkr_execution.submit_bracket` — lowercases the direction before validation.
3. `helio/ibkr_execution._futures_multiplier` — USD-per-point lookup table for MES/MNQ/MYM/M2K/ES/NQ/YM/RTY; non-futures default to 1.0. Applied in OVERSIZED notional calc.
4. `helio/ibkr_execution._oversize_threshold_usd` — asset-class-aware: stock/etf 0.5×, fx 1.5×, futures 2.5×. Pulls from a small table; cluster_exposure does the fine-grained per-strategy cap separately.

11 new tests in `argus_flow/tests/test_ibkr_execution_oversize.py` cover all four helpers including the exact failing real-world scenario (2 MYM at $49,862 against $31,490 NetLiq).

### What the kill list looks like in retrospect

`forge_vix_intraday` killed 5/12 morning had n=61, IR=-4.36 vs SPY, ACTUAL filled trades — those were diagnosable. Compare with cuebanks: scored 7.0 / hour for an unknown number of weeks, ZERO filled trades, no diagnostic visibility. The kill decisions were based on what was *observable* via the decision engine + ROI proof. The decision engine isn't broken; it just can't surface what doesn't reach the canonical_fills log. Future kill decisions on `helio.signal_executor` runners (cuebanks/mamba/tori) should be **re-evaluated after a full clean cycle of evidence** since their pre-fix sample is artifact-zero, not edge-zero.

### Operator state at end of session

- Cuebanks runner: PID 10860, restarted with all 4 fixes loaded
- Mamba runner: PID 8756, restarted (was using stale imports — now picks up fixes)
- Tori runner: PID 39152, restarted (same)
- Cuebanks heartbeat status: `waiting` (post-16:00 ET NY close, no more signals today)
- Live verification: pending tomorrow's 9:30 ET NY open — first cycle on the new code should either fill an order or surface the next layer (whatever it is)

### Recurring operator lesson

**"Environmentally silent" can mean "silent gate at layer N."** Before declaring a strategy silent due to market conditions, verify it can fill orders end-to-end. Decision engine + ROI proof read canonical_fills; if signals never reach canonical_fills, the strategy looks silent regardless of why.
