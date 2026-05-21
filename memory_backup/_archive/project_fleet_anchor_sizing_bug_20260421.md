---
name: Fleet anchor sizing bug — 2026-04-21
description: IBKR paper account's $1M virtual balance was leaking into fleet_anchored_risk_pct sizing, 100x oversizing every forge/Greek trade. Fixed with max_anchor_usd clamp.
type: project
originSessionId: 81afc97b-10bf-49fd-ba79-e5a89a7f400b
---
Discovered 2026-04-21 while investigating gld_pm_long.

## The bug

`helio/fleet_sizing.py::get_sizing_anchor_usd()` read `broker_truth.account_equity_usd` from `risk_oversight_report.json` (which comes from IBKR `net_liquidation_usd`). For paper account DUP472829, that's the $1M virtual balance IBKR ships paper accounts with.

Every strategy using `compute_risk_usd(strategy_label=...)` — all of forge + Greek family — was sizing **100x** larger than the intended $10K anchor whenever the broker was connected.

**Why:** `get_broker_equity_usd()` returned the live $1M, only falling back to `fallback_anchor_usd = 10000` when the broker read was None/≤0. There was no ceiling.

**How to apply:** When recommending anything about fleet sizing, risk, or position sizes, ALWAYS check `max_anchor_usd` in `argus_flow/configs/fleet_sizing.json`. When the user funds real capital and the account grows, they must bump this to match.

## Evidence

`forge/logs/gld_pm_long/trades.csv` — trade #3 (2026-04-20 18:30):
- 0.5% fleet_anchored_risk_pct, entry 442.32, stop 441.21 (diff 1.10)
- Expected size at $10K anchor: 90 shares, risk $100 (matches trade #2)
- Actual: **4,530 shares, risk $5,012** → anchor was ~$1,002,384
- Closed at stop −$5,011.92 on a 1-point move

## The fix (commit 2026-04-21)

1. `argus_flow/configs/fleet_sizing.json` — added `"max_anchor_usd": 10000.0` + note
2. `helio/fleet_sizing.py::get_sizing_anchor_usd()` — clamps broker equity to config.max_anchor_usd with `_log.warning("ANCHOR_CAPPED: ...")` on clamp
3. `argus_flow/tests/test_fleet_sizing.py` — added `test_anchor_caps_broker_equity_at_max_anchor_usd` and `test_anchor_below_cap_passes_through`; added `setUp(fs.invalidate_cache)` to TierEvaluation/ComputeRiskUsd/NotionalCaps/FleetMaxOpenRisk classes (fixes test isolation exposed by new tests)
4. Config version bumped to `2026-04-21.v4`

All 27 tests pass.

## Follow-ups

- **Runner cache:** Each runner process caches the anchor for 30s (`_ANCHOR_CACHE_S`). The fix only takes effect for a given runner process after the next cache expiry — so new sizing kicks in within 30s of the next signal eval. No restart strictly required, but restarting is cleanest.
- **When going live:** bump `max_anchor_usd` in the JSON to match the real funded equity ceiling. Leaving at $10K would starve sizing if the real account grows.
- **Notional caps:** `notional_caps_by_asset_class` multiplies anchor × 2/5/20. Now that anchor is clamped, these caps are meaningful again.
- **Already-closed trades:** trade #3 PnL stays in the books at −$5,012 (paper). It doesn't invalidate cohort count since forge/gld_pm_long isn't in the FX cohort, but it skews the gld_pm_long tier math. Consider flagging it as an anomaly in future tier-stat calculations.
