---
name: Paper-reset day complete — 2026-04-23
description: IBKR paper reset to $10K cash ($11,815 NetLiq incl MTD interest). All fallbacks removed. Preprod mirrors prod. 24 runners alive, sizing at $57/trade = 0.48% of $11,815. Architecture goal achieved.
type: project
originSessionId: 81afc97b-10bf-49fd-ba79-e5a89a7f400b
---
# Reset Day Complete — 2026-04-23

## What happened

User reset IBKR paper account to $10,000 cash. Accrued MTD interest from
pre-reset $1M period bumped Net Liquidation Value to **$11,815.11**. User
decided to accept the $11,815 baseline rather than re-reset (MTD interest
clear isn't selectable in IBKR).

## Architecture changes — NO FALLBACK, NO CLAMP, NO STATIC $10K

**Removed:**
- `helio/fleet_sizing.py::_FALLBACK_ANCHOR` constant (`10000.0`)
- `helio/fleet_sizing.py::_aligned_env()` (the ARGUS_DEFAULT_ACCOUNT_EQUITY_USD env sync)
- `argus_flow/configs/fleet_sizing.json::fallback_anchor_usd` key
- `argus_flow/configs/fleet_sizing.json::max_anchor_usd` key (the clamp)
- Clamp branch + stale-fallback branch from `get_sizing_anchor_usd()`
- `argus_flow/runner_unified.py` module-load fallback to `"10000"`
- `argus_flow/ops/fleet_registry.py::_MODEL_EQUITY` module-load constant
- `forge/tori/runner.py::STARTING_EQUITY = 10_000.0` module-load constant

**Added:**
- `helio.fleet_sizing.BrokerEquityUnavailableError` — raised when broker is
  unreachable AND last-known-good cache exceeds `_STALENESS_CEILING_S = 60`.
  Runners catching this should enter READ_ONLY (manage open positions,
  refuse new entries) and retry next cycle.
- Last-known-good cache pattern — 60s tolerance for TWS hiccups before raising.
- `argus_flow/ops/managed_truth_loop.py` — new persistent Python daemon that
  calls `risk_oversight.main()` every 3 min. Replaces the broken
  `ArgusManagedTruth` scheduled task (which was "Interactive Only" and
  silently failed on RDP disconnect). **This fixes the realtime dashboard
  broker-equity updates issue.**
- `deployment_registry.json` cleanup: stale numeric `model_start_equity_usd: 10000.0`
  replaced with string sentinel `"fleet_anchor"` so runners resolve live.

**Updated:**
- 4 strategy spec MD files (wick_gbpusd, jpy_pm_short, gld_pm_long, nq_overnight)
  — changed "1.0% of model equity ($10,000 default)" to "1.0% of live broker
  equity (dynamic — pulled via `helio.fleet_sizing.get_sizing_anchor_usd()` at
  eval time; no hardcoded default)".
- Config version bumped to `2026-04-23.v5`.

## Validation gates (all passed)

1. ✅ No ANCHOR_CAPPED log lines in any runner
2. ✅ `risk_oversight_report.json::broker_truth.account_equity_usd` = $11,815.11
3. ✅ Argus sizing: `equity=$11,815.11` (= broker, not clamped)
4. ✅ fleet_sizing.json has no `max_anchor_usd` or `fallback_anchor_usd` keys
5. ✅ `BrokerEquityUnavailableError` class defined
6. ✅ 24 runner processes alive
7. ✅ Heartbeats fresh (5s) on all 3 FX pairs

Expected first-trade sizing: **~$57-$59 (0.48-0.50% of $11,815)**. Confirmed in
startup log: GBPUSD risk=$57.20, USDJPY risk=$44.87.

## Archived

Pre-reset state in `argus_flow/logs/_archive/pre_reset_20260423/` (53 files:
trades.csv + state.json + canonical_fills + dated reports for each argus pair
and all apollo/hermes/titan). Forge per-strategy in `forge/logs/_archive/pre_reset_20260423/`
(35 files).

## Tests

`argus_flow/tests/test_fleet_sizing.py` — 28/28 passing. Rewrote TestSizingAnchor:
- `test_anchor_uses_broker_equity_when_available` (happy path)
- `test_anchor_returns_high_broker_equity_unclamped` (no clamp)
- `test_raises_when_broker_missing_and_no_cache` (no fallback)
- `test_raises_when_broker_zero_and_no_cache` (no fallback)
- `test_serves_last_known_good_during_brief_outage` (60s tolerance)
- `test_stale_cache_expires_then_raises` (stale cache → error)

## Why $11,815, not $10,000

IBKR paper accounts accrue simulated monthly interest on cash balance. The
$1,815 delta is MTD interest that accumulated on the pre-reset ~$1M balance
before the reset propagated. Going forward at $10K cash, monthly interest
accrues ~$30 — negligible drift.

If user later wants exact $10K baseline, re-reset paper account and interest
zeros out with the reset.

## How to apply in future conversations

- **No more fallbacks.** If a proposal includes "default equity", "$10K default",
  or any hardcoded dollar anchor, push back. Preprod mirrors prod; the only
  way to change the anchor is to change the broker account balance.
- **TWS briefly unavailable is OK** — 60s grace window via last-known-good cache.
- **TWS down for 60s+** → runners raise `BrokerEquityUnavailableError`, enter
  READ_ONLY. This is intentional.
- **Realtime dashboard updates** — managed_truth_loop daemon (PID auto-assigned,
  launched via `python -m argus_flow.ops.managed_truth_loop`) must be alive.
  If dashboard broker equity stops updating, check this process first.
- Forge runners that use `compute_risk_usd(strategy_label=...)` all now size
  against live broker equity. No kludges per-strategy.
