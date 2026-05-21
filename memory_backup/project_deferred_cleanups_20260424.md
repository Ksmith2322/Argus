---
name: deferred cleanups — surface missing backtest data + sizing-cascade hardening
description: Tightenings flagged during 2026-04-24 ops but deferred per "master what we have" directive. Not urgent. Pick up when monitoring yields time.
type: project
originSessionId: 81afc97b-10bf-49fd-ba79-e5a89a7f400b
---
## Deferred tightenings (not urgent, revisit later)

### 1. Surface existing backtest data to Strategy Performance table

**Problem:** 8 strategies show `—` for Backtest PF / BT Trades / BT WR columns, suggesting they run live without validation. They were actually backtested — the data just isn't in the JSON format `/api/strategy_performance` reads (`strategy_confidence/forge_<name>.json`).

**Group A — data exists in `STRATEGY_SPEC.md` markdown tables** (mechanical extract, ~15 min):
- `forge/gld_pm_long/STRATEGY_SPEC.md` — PF table (folds analysis)
- `forge/jpy_pm_short/STRATEGY_SPEC.md` — 2.5-yr backtest spanning BOJ normalization era
- `forge/nq_overnight/STRATEGY_SPEC.md` — "PF 1.20-1.29 vs GLD's 1.6-2.3"

**Group B — data referenced in memory but no current SPEC/JSON** (bigger lift, need to re-run backtests):
- `spy_mean_rev` — memory says PF 1.45 (pre-v2 trend filter). Note: v2 with trend filter needs re-backtest anyway before any honest comparison, per `project_spy_mean_rev_v2_trend_filter_20260423.md`.
- `multi_orb`
- `vix_intraday`
- `nq_london_close`
- `aud_asian_breakout`

**Why deferred:** User directive "master what we have + let the system prove itself" takes precedence. The dashboard reading `—` is cosmetic — the strategies are live and the operational_maturity verdict engine is the authoritative judge going forward, not backtest PF.

### 2. Harden broker-equity boot-time sequencing

**Problem observed 2026-04-24:** TWS restart → argus wedged → broker_equity stopped being written to `risk_oversight_report.json` → gdx_gld_runner failed to boot because `get_initial_capital_usd()` correctly raised `BrokerEquityUnavailableError`. Had to manually `python -m argus_flow.ops.risk_oversight` to refresh the report.

**Two small fixes (choose one, not both):**
- Option A: Have `managed_truth_loop` detect dead runners on each 3-min tick and auto-relaunch them after the oversight refresh. Cleanest — reuses existing daemon.
- Option B: Add a `wait_for_broker_equity(timeout=60s)` shim in `helio/fleet_sizing.py` for boot-time reads only (runtime reads should still fail fast).

**Why deferred:** The no-fallback architecture did exactly what it was designed to do — prevented a strategy from booting with stale sizing. Manual recovery via one command is acceptable. Not urgent.

## When to apply

**How to use:** When user says "anything else to tighten?" or "any stale to-dos?" — check this list first. Don't self-start; always green-light with user before executing.
