---
name: Current System State 2026-03-31
description: Complete state after major build session — real execution layer, promotion pipeline, kill switch, all gaps closed
type: project
---

## System State (2026-03-31)

### Fleet Status
- 14/14 runners online (8 FX + 6 futures)
- FX: clientId 1, Futures: clientId 2
- All paper/watcher stage, 0 real-money
- 21 total trades across fleet

### Major Builds Completed This Session

**1. Real Order Execution Layer (NEW)**
- runner_unified.py now has full IBKR order execution path
- Gated behind `execution_mode == "real"` — paper mode unchanged
- Entry: MarketOrder submission, fill callback, order timeout (60s)
- Exit: MarketOrder to close, cancel orphaned stop/target
- Bracket orders: StopOrder + LimitOrder placed after entry fill
- Trailing stop sync: local stop changes pushed to broker
- Fill routing: ib.execDetailsEvent routes to correct InstrumentRunner
- New State fields: entry_pending, exit_pending, order IDs, fill prices

**2. Emergency Kill Switch (NEW)**
- File-based: create `KILL_SWITCH` in repo root = emergency shutdown
- Cancels all open orders, closes all positions, Discord alert, disconnect
- Also: `PAUSE_ENTRIES` file blocks new entries without closing positions

**3. Complete Promotion Pipeline (NEW/EXPANDED)**
- Threshold increased from 30 to 60 valid trades
- 4 new promotion gates: regime_diversity, give_back (35%), consecutive_losses (8+), profit_factor (>=1.10)
- Outlier dependency tightened: 30% → 25%
- demotion_check.py: PROD demotion (expectancy, DD, consecutive weeks, single-day loss)
- Quarantine resolver: REINSTATE/DEMOTE_TO_QA/KILL with 14-day timeout
- Risk sizing ladder: 0.5% → 0.75% → 1.0% → 1.5% → 2.0% → 3.0% cap
- DEMOTE_TO_QA bug fixed (was killing instead of returning to paper)
- survived_disconnect changed from required to advisory

**4. Paper Sizing Fix (CRITICAL BUG FIX)**
- _get_account_equity() now returns model_start_equity_usd ($10K) for paper/watcher
- Was using live broker equity, causing RISK_BLOCKED_SIZE_ZERO on all paper runners

**5. Dashboard Fixes**
- Balance history chart: JS scoping bug fixed (balanceHistory moved to script-level)
- Trade timestamp normalization: fallback through ts/exit_ts/close_ts/entry_ts
- P&L chart denominator guard (single-point safe)
- Daily performance journal added (per-market breakdown, USD conversion)
- 3 orphaned API endpoints removed (divergence_status, kill_discipline, promotion_gate)
- Dead JS fetch blocks removed (divergence guard, kill discipline, promotion gate)
- Journal filter event listeners null-guarded

**6. Ops Fixes**
- Discord alerts singleton via ProcessLock (no more stacking)
- premarket_check.ps1 reads from deployment_registry (not hardcoded)
- autostart_runner.ps1 checks TWS before starting
- run_cohort_report.ps1 tracks critical failures, sends Discord on error
- Incident file dedup (one per symbol+type per session)
- Periodic broker reconciliation re-enabled (5-minute, warn-only)
- 617 stale incident files cleaned up
- 5 duplicate watchdog processes killed, one managed watchdog running

**7. Documentation Updates**
- ARGUS_MAP.md: Complete rewrite, IBKR primary, encoding fixed
- FILE_TREE.txt: Full update with all argus_flow/ files
- PROMOTION_PIPELINE.md: Complete stage lifecycle doctrine
- requirements.txt: Pinned 22 dependencies

### Start Commands
FX: `C:\Argus\.venv\Scripts\python.exe -m argus_flow.runner_unified --configs argus_flow/configs/cadjpy_t4_paper_v1.json argus_flow/configs/usdjpy_ny_paper_v1.json argus_flow/configs/audjpy_t4_paper_v1.json argus_flow/configs/audusd_ny_paper_v1.json argus_flow/configs/eurjpy_t4_paper_v1.json argus_flow/configs/eurusd_t4_paper_v1.json argus_flow/configs/gbpjpy_t4_paper_v1.json argus_flow/configs/gbpusd_range_paper_v1.json`

Futures: `C:\Argus\.venv\Scripts\python.exe -m argus_flow.runner_unified --client-id 2 --configs argus_flow/configs/mes_range_paper_v1.json argus_flow/configs/mnq_range_paper_v1.json argus_flow/configs/mym_range_paper_v1.json argus_flow/configs/m2k_range_paper_v1.json argus_flow/configs/mgc_range_paper_v1.json argus_flow/configs/mcl_range_paper_v1.json`

Dashboard: `C:\Argus\.venv\Scripts\python.exe ops/dashboard.py`

Kill switch: `echo > C:\Argus\repo\KILL_SWITCH`
Pause entries: `echo > C:\Argus\repo\PAUSE_ENTRIES`
