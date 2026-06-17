---
name: 2026-05-10 session — ROI proof engine + EXIT_FAILED root-fix + epoch_reset
description: Built the per-strategy ROI proof engine (helio/roi_proof_core, helio/spy_benchmark, ops/audit/run_strategy_roi_proof) with 54 tests, fixed EXIT_FAILED CRITICAL on FX (LimitOrder + outsideRth instead of MarketOrder, 7 tests), and shipped ops/maintenance/epoch_reset.py for the 5/31 cutover with 12 tests. First SPY-benchmarked verdict: vix_intraday FAILS_SPY_BENCHMARK (IR=-4.36).
type: project
originSessionId: ca6e24c7-7756-4b66-a0df-d339b1453b20
---
# 5/10 session — ROI proof engine + EXIT_FAILED fix + epoch_reset

## What landed (in order)

### 1. ROI proof engine (54 tests)

The capital-allocation engine the user asked for. Three new modules:

- **`helio/roi_proof_core.py`** (220 lines, 21 tests): pure-function stats. Bootstrap CI, max drawdown, Sortino, Information Ratio, trade-removed concentration stress, friction model per asset class. **Sample-size disciplined**: returns `InsufficientSample` sentinel below n=30 rather than producing over-interpretable numbers. Constants match `project_decisive_test_protocol.md`: 30 / 75 / 138.
- **`helio/spy_benchmark.py`** (200 lines, 13 tests): SPY-over-strategy-active-timestamps comparison. Per-trade alignment (entry_ts → exit_ts SPY return), partial-coverage handling, no false confidence when SPY data doesn't cover the window.
- **`ops/audit/run_strategy_roi_proof.py`** (450 lines, 14 tests): orchestrator with the verdict matrix:
  - `SCALE_CANDIDATE` (PF≥1.25, Sortino≥0.8, IR≥0.5, n≥75)
  - `PROMISING_LOW_SAMPLE` (n<75 but pre-promotion stats look OK)
  - `REPAIR_EXIT` (PF<1.10 marginal)
  - `REPAIR_SIZING` (concentration ≥50% from one trade)
  - `SHADOW_ONLY` (positive but doesn't clear floors)
  - `FAILS_SPY_BENCHMARK` (IR<-0.5 with ≥50% coverage)
  - `KILL` (PF≤0.95 + negative friction-adj expectancy)
  - `INSUFFICIENT_SAMPLE` (n<30)
  - `ALREADY_KILLED` (allocation_factor 0.0 short-circuit)

Outputs: `strategy_roi_proof.csv`, `strategy_vs_spy_benchmark.csv`, `strategy_kill_or_scale_matrix.csv`, `strategy_roi_gap_to_25_40.csv`, `roi_proof_report.md`.

**First non-trivial finding from the dry-run**: `forge_vix_intraday` is now **FAILS_SPY_BENCHMARK** (IR=-4.36, Sortino=-5.75, ann ROI=-9.23% on avg notional, n=61 with 100% SPY coverage). The audits were calling vix_intraday our "lone real-money candidate" — the SPY-aligned IR says it's significantly worse than SPY over the same windows. KILL CANDIDATE on next mid-cycle.

`gld_pm_long` (n=20) and `nq_overnight` (n=12) annualize >156% on avg notional and label `ALREADY_AT_OR_ABOVE` both 25% and 40% targets — but the engine refuses to declare victory until n≥30. Sample-size discipline working as designed.

### 2. EXIT_FAILED root-fix (7 tests)

Yesterday's argus pair incident (orders 296, 299, 303, 307 timing out → EXIT STUCK → EXIT FAILED → forced local FLAT) had a clear root cause: **MarketOrder on FX (CASH on IdealPro) can stall around session boundaries**. The `flatten_eod_executor` knew this and used wide LimitOrder + outsideRth=True for FX; the runner's inline exit retry path used MarketOrder for everything.

Fix: added `InstrumentRunner._build_exit_order(close_action, qty, ref_px, tif)` helper that branches on `secType`:
- `CASH`: `LimitOrder` with 5% adverse buffer (1.05× for BUY, 0.95× for SELL) + `outsideRth=True`
- everything else: `MarketOrder` (RTH-routing handles itself)

Replaced all 3 sites:
1. `_submit_real_exit` (initial exit submit)
2. `EXIT RETRY (IOC MKT)` after 30s timeout
3. `EXIT RETRY (GTC MKT)` after 60s

For FX retries, `tif` is downgraded to `DAY` (IdealPro rejects `IOC` on FX).

7 tests in `argus_flow/tests/test_exit_order_builder.py` cover all paths including buy-side buffer, the zero-ref-px defensive default, and tif propagation.

### 3. `ops/maintenance/epoch_reset.py` (12 tests)

The 5/31 reset script per `project_2026_05_31_clean_reset_plan.md`. Default behavior is **dry-run**; `--execute` to actually move files.

What it does:
- Archives evidence files (canonical_fills.jsonl, per-strategy trades.csv, scorecards, drift/risk reports) into `argus_flow/logs/_archive/pre_reset_<TARGET>/`
- Zeroes `trade_count` / `trade_serial` / `next_trade_num` on argus pair state files (the COUNTER_AHEAD_WHILE_FLAT cohort)
- Rewrites `canonical_fills.jsonl` as empty (header-ready)
- Writes a `_manifest.jsonl` with every move so rollback is recoverable

What it preserves: code, configs, allocation_factors.json, real_money_allowlist.json, shadow_strategies.json, memory files, kill verdicts.

Refuses to run while `HALT.flag` or `FLATTEN_EOD.flag` is present (operator must clear first). Idempotent — re-running on the same target is a no-op for already-archived files.

Test coverage: 12 tests including dry-run plan correctness, idempotence after first execute, counter-only field reset (preserves position/config_hash), HALT.flag refusal, manifest writing.

## Test totals (this session)

- **54** ROI engine tests pass (21 core + 13 benchmark + 14 orchestrator + 6 verdict-specific)
- **7** EXIT order builder tests pass
- **12** epoch_reset tests pass
- **73 new tests, all green.** No regressions in pre-existing suite.

## What's still queued (next session)

| Item | Why deferred |
|---|---|
| `run_sizing_audit.py` upgrade | Existing `strategy_scorecard.csv` already has sizing fields; per-strategy notional history wiring is real work |
| `run_exit_repair_tests.py` | Explicitly requires n≥75 closed trades per strategy. **Zero strategies** are at that bar yet. Build after 5/31 reset accumulates evidence |
| SPY/QQQ/IWM shadow research scaffold | Lower priority — research, not safety. Build after EXIT_FAILED has lived in production for a week |

## When to run the ROI engine for real

**Not yet.** Today's outputs are on contaminated data (pre-fix + post-fix mixed). The engine correctly mostly says `INSUFFICIENT_SAMPLE`. Real interpretation begins after the 5/31 reset:
- 6/15: ~10-15 trading days of clean post-reset data
- 6/30: defensible go/no-go on real-money

Until then, the engine is infrastructure. The discipline it codifies (Sortino floor, IR floor, n≥75 trust line, no-grade-without-sample) is the framework that drives the 6/30 decision.

## Doctrine alignment

This session implemented the user's "ruthless capital allocation" framework in code:
- Beat SPY (IR ≥ 0.5 floor, with FAILS_SPY_BENCHMARK as kill condition for sample-mature failures)
- Credible path to 25-40% ROI (`strategy_roi_gap_to_25_40.csv` outputs feasibility per strategy)
- No emotional middle ground (9 explicit verdict labels with reasons)
- No grade without sample (n=30 / 75 / 138 protocol enforced)
- Sample-size disciplined (Sortino + IR refuse to compute below n=30)

## Cross-references

- `project_2026_05_07_evening_session_fixes.md` — 5/7 evening: real-money boundary + halt_state + drift formula + gdx_gld silent-death root fix
- `project_2026_05_31_clean_reset_plan.md` — 5/31 procedure that epoch_reset.py implements
- `project_decisive_test_protocol.md` — 30/75/138 sample size protocol used by the engine
- `project_real_money_boundary.md` — boundary policy enforced by the verdict matrix
- `helio/roi_proof_core.py`, `helio/spy_benchmark.py`, `ops/audit/run_strategy_roi_proof.py` — engine code
- `argus_flow/runner_unified.py:2361` — `_build_exit_order` helper (the EXIT_FAILED fix)
- `ops/maintenance/epoch_reset.py` — 5/31 reset
