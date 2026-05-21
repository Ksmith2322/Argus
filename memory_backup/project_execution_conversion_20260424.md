---
name: IBKR paper-account real-order conversion — 2026-04-24
description: Wired the active fleet to actually submit orders to the IBKR paper account (was simulating internally). 10 strategies converted, 12 need design work (placeholder live mode).
type: project
originSessionId: 81afc97b-10bf-49fd-ba79-e5a89a7f400b
---
## Why this happened

User feedback 2026-04-24: dashboard showed +$272 simulated PnL on $11,815 anchor but IBKR paper balance hadn't moved. Audit found only argus + gdx_gld had real-execution code, and even argus was gated behind `execution_mode=="real"` while configs were `stage=paper`. **Term "paper" was overloaded** — code meant "simulate internally," user meant "real orders to IBKR paper account." All 22 strategies were in internal-simulation mode.

## What got done

**Architecture:** built `helio/ibkr_execution.py` shared helper module (connect, submit_bracket, query_position, check_bracket_filled, close_position_market, make_contract). Designed for wake-and-sleep runners. Also `helio/signal_executor.py` for scanner-style runners.

**Discovery (2026-04-24 evening):** apollo/hermes/titan/ares already had IBKR execution wired via the SEPARATE `helio/ibkr_executor.py` (no 'n') Greek-family helper. They were running with `--loop` only (paper-shadow); just needed restart with `--live` flag. ares uses `--execute` and fires via nightly `run_cohort_report.ps1`.

**Sizing fix:** tightened `notional_caps_by_asset_class` in fleet_sizing.json:
- stock: 2.0 → **1.0** (was buying $23K of SPY on $11.8K anchor; now caps at 1× anchor)
- etf: 2.0 → **1.0**
- fx: 20.0 (kept — pip-stops keep risk small)
- micro_future: 5.0 (kept — futures margin is ~10% notional)
- Config bumped to 2026-04-24.v6.

**Converted (22 strategies — full fleet — submitting real orders to IBKR paper port 7497):**

| strategy | client_id | instrument | direction |
|---|---|---|---|
| argus_flow/runner_unified | 1 + 12/51/53 | FX (3 pairs) | both |
| forge/gld_pm_long | 102 | GLD ETF | long |
| forge/jpy_pm_short | 103 | USDJPY+CADJPY FX | short |
| forge/nq_overnight | 104 | MNQ futures | long |
| forge/spy_mean_rev | 105 | SPY ETF | both |
| forge/vix_intraday | 106 | UVXY stock | both |
| forge/nq_london_close | 107 | MNQ futures | short |
| forge/aud_asian_breakout | 108 | AUDUSD FX | both |
| forge/multi_orb | 109 | SPY/QQQ/IWM/GLD | both |
| forge/fomc_drift | 110 | SPY (event) | long |
| forge/tom_international | 111 | EEM/EWJ/VGK | long |
| forge/wick_gbpusd | 112 | GBPUSD FX | long |
| forge/vix_revert | 113 | SPY (VIX trigger) | long |
| forge/mamba | 114 | MYM micro futures | both |
| forge/tori | 115 | MYM micro futures | both |
| forge/cuebanks | 116 | MYM micro futures | both |
| forge/rebalance | 117 | S&P add stocks | long |
| forge/gdx_gld_runner | 101 | GDX/GLD pair | both |
| apollo | 90 | ER stocks (113 universe) | both |
| hermes | 80 | gap-fill stocks | both |
| titan | 60 | trend stocks (10+) | long |
| ares | 70 | sector-rotation ETFs | long (monthly) |

**multi_orb subtlety:** because it can hold 4 simultaneous positions, the per-position cap is divided by ticker count: `max_notional_usd("stock") / len(PARAMS["tickers"])`. So combined fleet exposure stays under 1× anchor instead of 4×.

**Argus changes:** flipped `execution_mode == "real"` gating to `in ("real", "paper")` at 4 sites (L2686, L2728, L3226, L4371). Removed two paper-skip reconciliation branches (L4431, L4500) — paper now hits the broker so drift checks should run normally.

**ares wiring:** scheduled task `ArgusCohortReport` fires nightly at 23:00 local. `run_cohort_report.ps1` calls `python -m ares.runner --execute` which submits real orders via `helio/ibkr_executor.py`. ares is monthly rotation — only meaningful trades happen at month-end.

## What's NOT converted (and why)

**Skipped — no trading code at all:**
- forge/atlas (regime classifier, no `_open` or `open_trade`)
- forge/themis (congressional tracker, scanner only)

These are CORRECTLY signal-only by design — they emit context to other strategies, not orders.

## Final session count

- **22 strategies submitting real orders** to IBKR paper account (port 7497, account DUP472829)
- **2 skipped** (atlas/themis — informational only)
- All processes restarted with `--live` or `--loop` mode as appropriate
- Notional caps tightened from 2.0× → 1.0× for stock/etf
- Two helper modules built: `helio/ibkr_execution.py` (wake-and-sleep runners) + `helio/signal_executor.py` (scanner-style runners)

## Pattern for future conversions

When converting a runner from signal-only to real:
1. Add `from helio import ibkr_execution as ibkr` + unique `IBKR_CLIENT_ID` (forge range 100-199, used: 101-109)
2. Add `_SIGNAL_ONLY_MODE` global + `--signal-only` CLI flag
3. Modify `_open()` to take `ib=None` param. If ib is provided and not signal-only:
   - `make_contract(symbol, instrument_type)` and `qualifyContracts`
   - `query_position` to guard against doubling up
   - `submit_bracket(direction, size, stop_px, target_px, price_decimals=...)`
   - Replace planned entry with REAL fill price; recompute stop/target relative
   - Store `execution_venue="ibkr_paper"`, `stop_order_id`, `target_order_id` in state
4. Modify `evaluate_once()` to:
   - Connect at start (in try block)
   - For open trades with `execution_venue="ibkr_paper"`: call `check_bracket_filled` for stop/target, fall back to time-stop via `close_position_market` if `bars_held >= hold_bars`
   - Old yfinance-replay path stays as signal-only fallback when ib is None
   - Disconnect in `finally`
5. Smoke test with `--evaluate --signal-only`, then `--evaluate` (no flag) to test IBKR connection
6. Restart `--loop` process

## How to apply this memory

**Why:** The next session that picks this up needs to know which strategies are already real vs which still need work, and the conversion pattern to replicate.

**How to apply:**
- If user asks "is X submitting real orders?": consult the table above
- If user wants to convert one of the placeholder-live strategies: budget 30-60 min per runner, follow the pattern above, test with --signal-only first
- Watch IBKR paper balance after the first weekend → first market day to confirm real orders are landing (Mon 2026-04-28 will have first real signals through the converted runners)
- If the IBKR balance doesn't move despite trades closing: check `risk_oversight_report.json broker_truth.fleet_open_risk_usd` — if always $0, orders may be erroring silently
