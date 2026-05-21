---
name: 2026-05-31 clean dashboard + balance reset plan
description: After the 5/31 strategy freeze, reset the paper account balance, clear stale dashboard data, and start fresh with the post-fix codebase to give every surviving strategy clean post-reset trades for the 6/30 real-money go/no-go decision. Locked decisions on what gets reset, what is preserved, and the exact procedure.
type: project
originSessionId: ca6e24c7-7756-4b66-a0df-d339b1453b20
---
# 2026-05-31 clean reset — paper balance + dashboard data epoch

## Decision context

The post-4/23 paper reset epoch accumulated:
- 7 strategy fixes (sizing root, persistent fill queue, pre-entry orphan check, margin-aware caps, per-strategy caps, CB hysteresis, multi_orb window revert)
- Real-money boundary module + mismatch daemon + 21 tests
- gdx_gld silent-death root fix (Windows lock-bug + client_id rotation)
- Mamba/cuebanks reconnect + the IBKR Error 326 diagnostic
- Drift formula fix (unrealized P&L now in expected equity)
- HALT.flag / FLATTEN_EOD.flag / broker_drift split-brain reconciled into `helio/halt_state.py`
- position_monitor scope fix + artifact_divergence semantics fix
- Phantom trades worth $7,130 explicitly excluded
- multi_orb killed cleanly
- 117 blocked opportunities forward-scored, MTF_LONG_NOT_AT_SUPPORT shadow-A/B scaffold built (default-off)

Result: post-4/23 trades are a **mix of pre-fix and post-fix evidence**. By 5/31 the only strategies with edge claims (vix_intraday, gld_pm_long, nq_overnight) will still have small post-fix sample. The 5/31 → 6/30 deferred go-live path needs **30 days of clean post-fix data per surviving strategy** to defend a real-money decision.

Resetting on 5/31 gives the surviving fleet a clean June month to accumulate that evidence.

## What gets reset

### Paper account balance
- Reset broker balance to a fixed anchor (recommend **$30,000** — rounds nicely, leaves headroom for 3-5 simultaneous positions at sensible sizing)
- Done in TWS: File → Reset Paper Account
- Updates `argus_flow/configs/fleet_sizing.json` `anchor_usd` to match
- Updates `argus_flow/logs/_risk/circuit_breaker_state.json` `day_open_equity_usd` on next cycle

### Trade evidence (rolled to archive)
- `argus_flow/logs/canonical_fills.jsonl` → `argus_flow/logs/_archive/pre_reset_20260531/canonical_fills.jsonl`
- Every `forge/logs/<strategy>/trades.csv` → `forge/logs/<strategy>/trades_pre_20260531.csv`
- Every `argus_flow/logs/<pair>_trades.csv` → `argus_flow/logs/_archive/pre_reset_20260531/`
- New empty `trades.csv` files created with the canonical schema for each surviving strategy

### Per-strategy state counters (only those with COUNTER_AHEAD_WHILE_FLAT)
- Reset state.trade_count and state.trade_serial to 0 for argus pairs (CADJPY, GBPUSD, USDJPY) so artifact_divergence reports CLEAN going forward
- Backed up to `_archive/pre_reset_20260531/state_counters.json` first

### Dashboard / analytics data
- `argus_flow/logs/strategy_scorecard.csv` archived; new scorecard built from post-5/31 data only
- `argus_flow/logs/promotion_gate_report.json` cleared and regenerated
- Decision engine action history archived (so previous KILL/REDUCE recommendations don't bias the new epoch)
- `evidence_registry.json` archived; new registry begins on 6/1

### Drift / drawdown state
- `argus_flow/logs/_risk/broker_drift_state.json` reset (new since_ts, n_trades_counted=0)
- `argus_flow/logs/_risk/portfolio_risk_state.json` peak_pnl reset to current_pnl
- HALT.flag, FLATTEN_EOD.flag must NOT be present at reset start (verify before reset)

### Phantom-trade exclusion list
- Archive the existing exclusion sidecar; new fleet starts with empty phantom list

## What is PRESERVED (do NOT reset)

- **`allocation_factors.json`** — kill verdicts (multi_orb 0.0, spy_mean_rev 0.0) carry forward; the strategies stay killed unless explicitly resurrected
- **`real_money_allowlist.json`** — boundary stays default-off; resetting would not change anything but no need to touch
- **`shadow_strategies.json`** — MTF_LONG_NOT_AT_SUPPORT stays in scaffold default-off
- **All code fixes** — sizing root, persistent fill queue, halt_state, real_money module, position_monitor scope, artifact_divergence semantics, drift formula, mamba heartbeat fix, gdx_gld lock-bug fix, IBKR_PORT propagation, dashboard halt panel
- **Backtest baseline data** — backtest_trades.csv files for each strategy (mamba 25/43d, tori 665/2yr) are reference, not state
- **Killed-strategy review** — KEEP_KILLED verdicts for spy_mean_rev / multi_orb / apollo / hermes / titan / forge_rebalance carry forward
- **Configs** — `argus_flow/configs/*_paper_v1.json` per-pair configs, hashes.json registry
- **Memory files** — all `project_*.md` files in `C:\Users\ksmit\.claude\projects\c--Argus\memory\`
- **TWS client_id rotation history** — gdx_gld stays at client_id=120 (rotated from 101 on 5/8)
- **Test suite** — 39+ tests (real_money + PnL recon + halt_state + shadow_strategies); regressions caught here

## Timing

- **2026-05-31** (Sunday) — strategy freeze. After the freeze ceremony decision (KILL / KEEP-PAPER / WINNER-CANDIDATE / REAL-CANDIDATE per strategy)
- **2026-05-31 evening or 2026-06-01 early** — execute reset before NY open Monday 6/1
- **Why not earlier**: lose evidence accumulating now; 5/15 mid-cycle review still needs the data
- **Why not later**: more days lost from the 30-day clean-evidence window before 6/30 real-money decision

## Procedure (sequential)

```
1. Pre-flight (1 hour before reset)
   a. Confirm HALT.flag and FLATTEN_EOD.flag absent
   b. Confirm no open broker positions (or close them via flatten_eod_executor --force)
   c. Snapshot all state files into _archive/pre_reset_20260531/
   d. Verify all 39+ tests still pass
   e. Confirm 5/31 ceremony verdicts written to verdict_20260531.json

2. Stop the fleet (10 min)
   a. Stop managed_truth_loop process
   b. Stop all forge runners + argus_flow.runner_unified
   c. Stop dashboard
   d. Verify no python processes match argus modules

3. Reset broker (5 min — operator action in TWS)
   a. TWS → File → Reset Paper Account → confirm
   b. Verify in TWS: balance shows new anchor (e.g. $30,000), positions empty

4. Roll archive (10 min — script-driven)
   a. python -m ops.maintenance.epoch_reset --target 20260531 --dry-run    # verify what will move
   b. python -m ops.maintenance.epoch_reset --target 20260531              # actually move
   c. Spot-check: trades.csv files are empty, state files reset, archive populated

5. Update anchor (5 min)
   a. Edit argus_flow/configs/fleet_sizing.json to set anchor_usd to new value
   b. Verify hashes.json registry updated if config schema changed

6. Restart (15 min)
   a. ops/start_all_runners.ps1 (idempotent)
   b. python -m argus_flow.ops.managed_truth_loop &
   c. python ops/dashboard.py --port 8080 &
   d. Verify all 18+ runners up + heartbeats fresh

7. Post-flight verification (15 min)
   a. /api/halt_status: halted=false, all sources clear
   b. broker_drift_state.json: tripped=false, since_ts=now
   c. circuit_breaker_state.json: day_open=new_anchor, current_tier=OK
   d. canonical_fills.jsonl exists, empty (or just header)
   e. Run quick sanity audit: python -m ops.audit.argus_audit_engine

8. Document (10 min)
   a. Append entry to argus_flow/logs/capital_promotion_ledger.jsonl describing the reset
   b. Update memory: add project_2026_06_01_post_reset_baseline.md with anchor + surviving strategy list
```

## Risk and rollback

- **Worst case**: reset proceeds but a runner doesn't restart cleanly. Recovery: re-launch via start_all_runners.ps1; archive is preserved so no data loss.
- **Worst case 2**: TWS paper reset fails or balance drifts. Recovery: re-issue reset; balance check is the gate.
- **Rollback option**: archive directory `_archive/pre_reset_20260531/` contains every file moved. To rollback: stop fleet, copy files back from archive, restart.
- **Don't rollback unless**: corruption beyond what archive recovers, or strategy logic broke during the reset. Surviving strategies' code is unchanged by the reset.

## Code work needed before 5/31

The reset script `ops/maintenance/epoch_reset.py` does NOT exist yet. To build:
- 100-200 line Python module
- `--dry-run` flag mandatory (default behavior is dry-run; explicit `--execute` to actually move)
- Idempotent (safe to re-run on same target)
- All file moves under `_archive/pre_reset_<TARGET>/<original_path>` so paths are recoverable
- Logs every move with old + new paths to `_archive/pre_reset_<TARGET>/_manifest.jsonl`

Build it during Week 4 (5/24-5/30) post-readiness-gate review. Tests:
- `argus_flow/tests/test_epoch_reset.py` — verify dry-run prints intended moves; execute moves files; idempotent re-run is no-op; rollback restores original.

## Acceptance criteria

After reset, on 6/1 morning:
- broker_equity_usd ≈ new anchor ± normal session variance
- canonical_fills.jsonl row count: 0 (or just header)
- artifact_divergence_report.json: status=CLEAN for all argus pairs
- position_monitor.json: status=OK with no orphans (assuming no positions yet)
- risk_oversight: GREEN with 0 recommendations
- alert_state max_severity: INFO (worst case WARNING for known benign items like trade_drought)
- Every surviving strategy: trade_count=0 (until first 6/1 trade)

## Cross-references

- `project_strategy_freeze_20260531.md` — the freeze itself
- `project_real_money_readiness_gate_20260531.md` — gate checklist
- `project_capital_allocator_policy.md` — capital tier ladder for post-reset deployment
- `argus_flow/configs/allocation_factors.json` — kill verdicts that survive the reset
- `helio/halt_state.py` — halt-state truth that the reset must leave clean
- `ops/research/mtf_long_support_shadow_eval.py` — shadow-A/B scaffold that runs against post-reset data
- `helio/real_money_mismatch_daemon.py` — daemon that scheduled-task-installs post-reset
