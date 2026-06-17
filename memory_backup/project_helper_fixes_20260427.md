---
name: 2026-04-27 PDT cascade incident + helper hardening + sizing tightening
description: First-day-of-real-fills post-mortem. PDT rejection cascaded into stuck exits, bracket-double-fire SPY short, wrong-priced reconcile_flat trades. Fixed helio/ibkr_execution.py + tightened sizing + reset paper to $30K.
type: project
originSessionId: 0256622d-fcf2-4b57-9505-2a4e805eef61
---
## What happened

First day of real-paper-fills (2026-04-27, Monday after 2026-04-24 conversion). Cascade chain:

1. spy_mean_rev opened LONG 16 SPY @ 09:05 ET
2. **Bracket OCO bug**: target SLD 16 @ 714.22 (09:06) AND stop SLD 16 @ 713.52 (09:11) BOTH filled. Net: SHORT 16 SPY held by accident. Cause: `submit_bracket` placed stop+target as 3 independent orders (no `ocaGroup`).
3. **PDT rejection**: securities equity was $9,645 (under $25K Pattern Day Trader threshold). Strategy's exit attempts (SELL 16 SPY) all rejected with PDT errors. After-hours queue accumulated.
4. **Restart-survival bug**: `check_bracket_filled` used session-scoped `orderId` lookup → couldn't see fills from previous session after process restart. Strategy thought it still had original long, kept retrying exits. Multi_orb's QQQ/IWM/GLD all hit time-stop "reconcile_flat" with WRONG prices (current bar Close instead of actual broker fill price).
5. **Duplicate daemons**: cuebanks/mamba/tori had stale 4/24 `--live` processes running alongside 4/26 `--loop` processes — race conditions on state files.

## Fixes applied

**helio/ibkr_execution.py — 3 patches + 10 caller updates:**
1. `submit_bracket` now uses `ocaGroup` + `ocaType=1` for true OCO (prevents bracket double-fire).
2. `check_bracket_filled` switched from orderId lookup to **position-based detection + `reqExecutions` for actual fill price** (survives process restart).
3. New `is_market_open(contract)` instrument-aware RTH guard. STK/ETF gated to 09:30-16:00 ET weekdays. FX 24/5. FUT Sun-18:00-Fri-17:00 ET with daily 17:00-18:00 ET maintenance break. Used in `submit_bracket` and `close_position_market` — refuses to submit outside hours, preventing after-hours order queues + TWS warnings.
4. All 10 caller sites (9 forge runners + helio/signal_executor) updated to pass `entry_direction`, `entry_size`, `stop_px`, `target_px` kwargs so `check_bracket_filled` can classify exit reason as "stop"/"target" instead of "broker_exit".

**Sizing tightened (fleet_sizing.json v7):**
- stock/etf: 1.0× → **0.3×** (max 30% of equity per position)
- fx: 20.0× → **1.0×** (no leverage; 100% of equity max)
- micro_future: 5.0× → **0.5×** (max 50% of equity)
- Trade-off: FX strategies undertrade vs pip-based risk budget by ~10×. Acceptable for paper-validation; loosen only after 90 days clean live data. See feedback_paper_mirrors_real_money.md.

**Dashboard /api/positions_open direction label bug fixed:**
- Was: defaulted to "long" when strategy didn't store explicit direction (jpy_pm_short doesn't store direction).
- Now: `_derive_direction()` helper falls back to `stop_px > entry_px → short` derivation. Verified with jpy_pm_short USDJPY now showing "short".

**Account state:**
- User reset paper to $30K (above $25K PDT threshold) → cleared all phantom broker positions/orders.
- New broker_equity ~$31,815 (cash $30K + previous unrealized).
- Position before flatten: SHORT 16 SPY (the bracket-double-fire artifact). Closed via wide LMT BUY @ 715.08 outsideRth.
- Net cost of the bug: ~$22 (16 × $1.21 + commission).

**Process cleanup:**
- Killed 6 stale `--live` cuebanks/mamba/tori processes from 4/24.
- Full fleet reboot: 46 processes (23 logical runners) killed and relaunched via venv python with original command-line args preserved.

## Verification post-reboot

- Fleet: 25/25 systems OK, 0 DOWN, 0 BLOCKED.
- Broker: $31,815 equity, all_healthy=True, 0 positions, 0 orders.
- Sizing caps confirmed loaded:
  - stock/etf $9,545
  - fx $31,815
  - micro_future $15,908
- Argus pair entries cleared from RECON_DRIFT (auto-resolved when broker went flat).

## Files touched

- `helio/ibkr_execution.py` (3 patches)
- `helio/signal_executor.py` (1 caller update)
- `forge/{spy_mean_rev,jpy_pm_short,nq_overnight,multi_orb,aud_asian_breakout,gld_pm_long,wick_gbpusd,nq_london_close,vix_intraday}/runner.py` (caller updates)
- `argus_flow/configs/fleet_sizing.json` (v7)
- `ops/dashboard.py` (`_derive_direction` helper + 2 call sites)

## Open follow-ups (UPDATED 2026-04-27 evening — all closed)

- ~~FX_USD cluster cap~~ → **DONE.** `helio/cluster_exposure.py` built with 10 clusters + per-instrument cap (0.6×) + total notional cap (3×). Wired into `submit_bracket()` via optional `est_entry_px` kwarg; 9 forge runners updated to pass it. Pre-trade check rejects with `cluster_cap_breach:<cluster>` reason. New `/api/cluster_exposure` endpoint exposes utilization. Caps were tightened from spec defaults to be consistent with new fleet_sizing.json v7 (e.g. FX_USD_LONG 8× → 2×, EQUITY_BETA 1.5× → 0.9×).
- ~~autostart infrastructure~~ → **DONE.** `ops/start_all_runners.ps1` is the canonical idempotent launcher (`-DryRun` and `-RestartAll` flags). 23 runners total. Not yet registered as scheduled task — user can do that with `Register-ScheduledTask -Trigger (New-ScheduledTaskTrigger -AtLogOn)` if desired.
- ~~trades.csv wrong-priced records~~ → **DONE.** 6 rows marked `experiment_valid=false` with `invalid_reason` explaining the cause: multi_orb QQQ/IWM/GLD (reconcile_flat bug), jpy_pm_short CADJPY (simulated exit during reset), aud_asian_breakout AUDUSD (price doesn't match TWS), spy_mean_rev SPY (OCO bracket double-fire — only one leg recorded).
- **Dashboard cache lag** — DEFERRED. Cosmetic only.

## Late-evening additions (2026-04-27, after the first reboot)

- **IdealPro $25K floor on FX entries** (`helio/ibkr_execution.py` + `argus_flow/runner_unified.py`): refuses any FX entry where USD-equivalent notional < $25K — IdealPro routes those as odd lots with materially worse spreads. Helper `_fx_usd_notional()` handles USD-base, USD-quote, and cross pairs.
- **Argus runner_unified guards**: `_submit_real_entry` now checks fleet halt + IdealPro min + cluster cap (mirrors helio/ibkr_execution.py). `_submit_bracket_orders` now uses OCA group + ocaType=1 (same OCO fix as helio).
- **Greek family (apollo/hermes/titan)** `helio/ibkr_executor.py:submit_bracket()` now checks fleet halt + cluster cap. (OCO already correct via ib_insync.bracketOrder.)
- **Kill-switch built**: `HALT.flag` file + 3 endpoints (`/api/halt_status`, `POST /api/halt_fleet`, `POST /api/resume_fleet`) + visible red banner with RESUME button. Touch the flag from any terminal to halt; runners refuse new entries while flag exists; existing positions can still exit.
- **ArgusFleetStartup scheduled task registered** (LogonTrigger) — runs `ops/start_all_runners.ps1` at logon, idempotent across the 23 logical runners.
- **`reference_failure_modes.md`** updated with failure modes #6 (PDT cascade), #7 (OCO double-fire), #8 (check_bracket_filled restart-survival).

## Known follow-up: futures sizing under new caps

- **nq_overnight observation 2026-04-27 evening**: cluster cap rejects every entry with `cluster_cap_breach:SINGLE_INSTRUMENT:MNQ`. 1 MNQ contract ≈ $54K notional, SINGLE_INSTRUMENT cap is 0.6× = $19K. **MNQ literally cannot fit** under current caps. Same applies to any single full-sized futures contract.
- Affected strategies: nq_overnight, nq_london_close (MNQ), mamba/tori/cuebanks (MYM ≈ $40K/contract).
- **Decision needed (5/1 review or sooner)**: futures are typically risk-managed by margin, not notional. Real-money $30K supports 1 MNQ comfortably (~$5.4K margin = 18% equity). Options:
  - (A) Raise `micro_future` asset class cap to 2.0× and add a futures exception to SINGLE_INSTRUMENT cap — operationally fixes the issue.
  - (B) Defer until 5/1 review and accept these strategies don't trade in the meantime.
- Recommend (A) — current state is over-tightened for the futures asset class.

## New dashboard features (2026-04-27 evening)

- **Market clock widget** (top of page): shows US Stocks / FX / CME Futures status with live countdowns to next open/close. `_market_clock_snapshot()` + `/api/market_clock`. Hardcoded session windows (no holiday awareness — broker rejects on holidays).
- **`/api/cluster_exposure` endpoint**: per-cluster + per-symbol utilization vs caps. No UI panel yet (defer until exposure is non-zero often enough to be useful).
- **FLEET TOTAL row alignment fix**: was missing the BT PF column → all totals shifted one cell left of headers. Fixed.

## How to apply this memory

- If a similar cascade happens (PDT, margin cushion, bracket double-fire): check this memo for the failure chain.
- If proposing changes to `helio/ibkr_execution.py`: it's been hardened; new patches should preserve OCO + restart-survival + RTH guard semantics.
- If user asks "what changed on 4/27?": this is the comprehensive list.
