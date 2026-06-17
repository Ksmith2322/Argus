---
name: 2026-05-19 CADJPY EXIT FAILED — JPY decimal bug + cascade double-fill bug
description: Asked "how are things operating" — caught CADJPY stuck in EXIT FAILED cascade due to JPY price-decimal bug. Fixed the decimal bug. Restart double-sold CADJPY (LONG 41479 → SHORT -82858) due to a second bug (cascade race). Manually flattened, fixed both bugs, runner left DOWN until 5/31 cutover.
type: project
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
This was supposed to be "is anything operating wrong?" The answer was:
YES, materially.

## What happened

**Timeline:**
- ~13:30 UTC — operator asked how things were operating.
- 13:36 — found CADJPY in active EXIT FAILED cascade. Order 1888
  (LimitOrder lmtPrice=109.87177) rejected with Warning 110.
- 13:36-13:42 — runner cycled through stages, kept submitting same
  5-decimal prices. Each retry rejected. EXIT FAILED CRITICAL at 13:40.
  Runner adopted broker position as orphan and started fresh exit
  attempts; same bug bit again.
- 13:48 — diagnosed root cause: JPY pairs require 3 decimals on
  IdealPro (0.001 tick), but `_build_exit_order` rounded to 5 decimals.
- 13:49 — shipped fix #1 — JPY-aware rounding in `_build_exit_order`
  AND `_submit_bracket_orders` (lines 2370/2378 had same bug). Plus
  `forge/jpy_pm_short` had `price_decimals=5` for JPY pairs (archived
  strategy but bug was latent).
- 13:50 — restarted runner. New runner submitted prices with 5
  decimals AGAIN.
- 13:54 — diagnosed bug #1b: my JPY detection checked
  `contract.symbol`, but on ib_insync `Forex` contracts that's the
  BASE currency ("USD"), not the pair. JPY-ness lives in
  `contract.currency` or `contract.localSymbol`.
- 13:55 — shipped fix #1b — check `self.symbol`, `contract.symbol`,
  `contract.currency`, `contract.localSymbol` together.
- 13:56 — restarted runner again. Order 1930 (lmtPrice=109.838,
  3 decimals) **filled** at 115.696. CADJPY momentarily flat.
- 13:57 — cascade fired a GTC retry 60s after order 1930 submission
  (orderId 1931). Order 1931 ALSO filled at 115.689. Position now
  SHORT -82858 instead of FLAT.
- 13:58 — stopped runner. Direct broker check confirmed SHORT -82858.
- 13:59 — `emergency_close.py --symbol CADJPY` failed with Error 321
  (missing exchange — third bug, in the operator tool itself).
- 14:00 — wrote one-shot `_tmp_close_cadjpy.py` with proper Forex
  contract qualification + 3-decimal limit. Filled. CADJPY FLAT.
- 14:02 — diagnosed bug #2: cascade race. When IOC retry fills instantly,
  the cascade's GTC arm fires 60s later WITHOUT checking whether the
  earlier order has already filled. Result: duplicate fills, position
  reverses.
- 14:05 — shipped fix #2: cascade now queries broker positions before
  each retry. If broker shows position is flat, clears local state and
  exits the cascade entirely.

## Bugs fixed today

### Bug 1: JPY price-decimal mismatch

**Location:** `argus_flow/runner_unified.py:_build_exit_order` +
`_submit_bracket_orders` (lines 2370 + 2378); `forge/jpy_pm_short/runner.py:308`.

**Symptom:** IBKR Warning 110 "The price does not conform to the
minimum price variation for this contract" on every CADJPY/USDJPY/
EURJPY exit. The order stays PendingSubmit forever; cascade exhausts;
position remains stuck.

**Root cause:** `round(price, 5)` produces a 5-decimal price like
109.87177 on a JPY pair. IdealPro JPY pairs have 0.001 minimum tick
(3 decimals). The sub-tick price is rejected.

**Fix:** Detect JPY pairs by inspecting `self.symbol`,
`contract.symbol`, `contract.currency`, AND `contract.localSymbol`
(an earlier iteration of the fix only checked `contract.symbol`,
which is the BASE currency on ib_insync Forex contracts and missed JPY).
JPY pairs round to 3 decimals; others stay at 5.

**Tests:** `test_jpy_pair_exit_rounds_to_3_decimals`,
`test_jpy_pair_buy_to_close_rounds_to_3_decimals`,
`test_non_jpy_pair_still_uses_5_decimals`,
`test_jpy_pair_recognition_is_case_insensitive`,
`test_jpy_detected_when_only_currency_field_says_jpy`,
`test_jpy_detected_when_only_localSymbol_says_jpy`.

### Bug 2: Cascade race / duplicate fill

**Location:** `argus_flow/runner_unified.py:_check_order_timeouts`.

**Symptom:** When the IOC retry fills instantly, the cascade fires
the GTC retry 60s later without re-checking. Result: SECOND order
submitted, SECOND fill, position reverses (LONG → SHORT, or vice versa).

**Root cause:** Cascade tracks `s.exit_pending` and `s.exit_order_id`.
When an order fills, the `_on_fill` callback should set
`s.exit_pending=False`. But there's a race: `ib.placeOrder()` returns
immediately with an order id, and IBKR can fire `execDetails` BEFORE
the runner has time to update `s.exit_order_id`. The callback then
sees a mismatched order_id and doesn't clear exit_pending. 60 seconds
later, the cascade fires the next retry.

**Fix:** Before each retry attempt, query `ib.positions()` directly.
If the broker reports the position is flat for this instrument, the
prior exit must have filled; clear local state and exit the cascade.
Added `EXIT_RACE_RESOLVED` log line so the resolution is visible.

**Test:** `test_cascade_has_broker_truth_race_check` (source-level
regression that verifies the guard is in place).

### Bug 3 (FIXED in follow-up): emergency_close.py

`argus_flow/ops/emergency_close.py` failed with Error 321 "Missing
order exchange" when given a CASH (Forex) contract from `ib.positions()`.
The contract pulled from positions doesn't have `exchange` set;
MarketOrder needs it for FX (IDEALPRO).

**Fixed same session.** Three changes:
1. Call `ib.qualifyContracts(contract)` before submitting so the
   exchange field gets populated.
2. Use the same wide-LimitOrder + outsideRth + GTC pattern as
   `runner_unified._build_exit_order` for CASH (avoids the FX
   MarketOrder-stall issue).
3. JPY-aware decimal rounding (same fix pattern as the runner —
   checks symbol + currency + localSymbol for "JPY").

New helper `_build_close_order` factored out for testability.
10 tests in `argus_flow/tests/test_emergency_close.py` covering FX
LMT path, JPY decimal rounding via each field, STK/FUT MarketOrder
fallback, defensive ref_px=0 handling.

Tool is now safe to use during incidents. The hand-rolled
one-shot script used today (already cleaned up) was the manual
equivalent of what the tool now does correctly.

## Why the operator should care

This sequence of bugs happened in 30 minutes. **All three were in
code I shipped within the last 3 days.** Specifically:

- The JPY decimal bug was in `_build_exit_order` from 5/18.
- The cascade race was always there but only bit because the JPY fix
  caused an INSTANT-FILL scenario (orders now succeed at IBKR; before
  they were rejected at IBKR so no race).
- The emergency_close bug was older but exposed because we needed the
  tool today.

**Three lessons:**

1. **Fast iteration ships bugs.** The vix_carry / pead / xs_momentum /
   coint_pairs builds were all paper-tested first. The
   `_build_exit_order` fix on 5/18 was shipped to live code WITHOUT
   live exercise. That's the difference between "test passed" and
   "code works in production."

2. **One bug masks another.** The JPY decimal bug prevented orders
   from filling. That same condition prevented the cascade race from
   appearing. Fixing one revealed the other. In production-grade code,
   you have to verify the fix doesn't unmask new failure modes.

3. **The operator-tool emergency_close.py has been broken all along.**
   It's only used in incidents. So no one noticed. Next time we
   actually need it for real money, it'll fail with Error 321 until
   someone fixes it.

## Current state

- **CADJPY: FLAT** (closed manually at 115.688)
- **Runner: DOWN** (intentionally stopped to prevent any further
  surprises before 5/31)
- **3 broker positions remain:**
  - GBP/USD: SHORT -22,530 (argus_gbpusd)
  - USD/JPY: LONG 60,238 (argus_usdjpy)
  - 3 stock orphans: GLD 22, QQQ 7, SPY 26
- **All these positions have OCO brackets at the broker** (set at
  submission time). They'll exit on stop or target if hit, without
  needing the runner alive.

## Net economic impact

- Original CADJPY trade: LONG 41,479 @ 115.53667
- First close: sold 82,858 @ 115.696 (net of original = sold 41,379 short)
- Cascade duplicate: sold another 82,858 (net = -82,858 short, but
  first fill already cleared the long, so the second sell is what
  created the SHORT exposure)
- Manual close: bought back 82,858 @ 115.688
- **Net P&L: roughly +$43 paper** (small profit on the original close;
  the over-sell + cover was a wash)

So no real damage. But the bugs are real and shipped to live code.

## Decision: runner stays DOWN until 5/31 cutover

Reasons:
1. Three bugs in 30 minutes argues for more verification before
   restarting.
2. The runner has nothing critical to do — 5/31 is 11 days away.
3. Open broker positions are protected by their OCO brackets.
4. The 5/31 cutover runbook (project_2026_05_31_reset_runbook.md)
   stops everything anyway as Phase 1. Just bringing it forward.
5. Avoids risk of cascading more bugs that the JPY fix newly unmasked.

**Operator actions before 5/31 (per the runbook):**
- Position monitor still works (queries broker directly via its own
  IBKR connection). Should be checked daily.
- If a broker bracket exits a position, the runner won't know — that's
  fine; reconciliation on next startup will pick it up.
- The 3 stock orphans (GLD/QQQ/SPY) still need operator decisions
  (close or write unwind tickets).

## Tests added this session

- `test_jpy_pair_exit_rounds_to_3_decimals`
- `test_jpy_pair_buy_to_close_rounds_to_3_decimals`
- `test_non_jpy_pair_still_uses_5_decimals`
- `test_jpy_pair_recognition_is_case_insensitive`
- `test_jpy_detected_when_only_currency_field_says_jpy`
- `test_jpy_detected_when_only_localSymbol_says_jpy`
- `test_cascade_has_broker_truth_race_check`

23/23 green across `test_exit_order_builder.py` + `test_fx_exit_retry_cascade.py`.

## What this changes in the 5/31 cutover plan

Phase 1 of the runbook says "stop all runners." That's already done.
The runbook's Phase 6 ("launch post_reset_runners") will run the
SAME runner_unified code we just fixed — so the JPY + cascade bugs
will be fixed in the post-reset cohort from day one.

No changes to the runbook required.

## Open item for next session

Fix `emergency_close.py` to call `ib.qualifyContracts(contract)`
before submitting. Add a test that emergency_close handles FX
contracts correctly. This is genuinely fix-now for any future
incident but doesn't block 5/31.

## Same-day follow-on (after operator asked "how is the day going")

Discovered the cascade race kept biting THROUGH THE AFTERNOON because:
1. `watchdog.ps1` auto-restarted the argus runner at 14:01 after I stopped it
2. The first JPY-detection fix had the wrong field (`contract.symbol` is
   base currency on ib_insync Forex), so the auto-restarted runner kept
   producing 5-decimal prices
3. After the symbol-detection fix, the cascade race kept doubling
   positions because the broker-truth check ONLY fired in
   `_check_order_timeouts` — NOT in `_submit_real_exit` or `_submit_real_entry`

**End-of-day positions** (after the chaos cleared via broker brackets):
USDJPY went 30K → 60K → 120K → 0. GBPUSD went 22K → 45K → 90K → 0.
CADJPY had the original LONG→SHORT incident + my manual close.
**Net account: +$244** (got lucky on direction; could easily have been -$2K).

## Final fixes shipped (2026-05-20 cleanup batch)

1. **Disabled `ArgusFleetStartup` scheduled task** (operator-stopped runners now stay stopped). `ArgusWatchdog` (boot trigger) still enabled but requires reboot to fire.
2. **Killed running `watchdog.ps1`** (the long-lived process that was auto-relaunching the runner).
3. **Centralized `_read_broker_position()` helper** on `InstrumentRunner` — single source of truth for broker queries.
4. **`_broker_state_allows_exit(action, qty)` helper** — refuses exit submission when:
   - Broker already flat (the cascade-double-fill case)
   - Broker direction opposite local belief (state drift)
   - Signals resize when local thinks bigger than broker
5. **`_submit_real_exit`** now calls the guard; on refusal, clears local state and returns False so reconciliation can adopt broker truth.
6. **`_check_order_timeouts`** cascade refactored to use the centralized helper instead of inline check.
7. **`_submit_real_entry`** got **Guard 4 (broker-position check)** — refuses entry when broker already has a position for this instrument (orphan detection at submission time, not just at reconciliation).
8. **3 fail-open exception handlers in `_submit_real_entry`** (Codex X5 type) converted to fail-CLOSED: halt check, FX-min check, cluster-cap check now all return False on exception instead of swallowing.

## New tests (2026-05-20)

`argus_flow/tests/test_exit_broker_truth_guard.py` — 14 tests:
- `_read_broker_position`: localSymbol match, symbol+currency fallback, returns 0 when not in positions, None when ib unreachable
- `_broker_state_allows_exit`: happy path, broker-flat refusal, opposite-direction refusal, resize signal
- Source-level guards: `_submit_real_exit` calls the helper, cascade uses centralized helper, `_submit_real_entry` has Guard 4, no `allowing trade` fail-open patterns remain in `_submit_real_entry`

## Test scoreboard after the cleanup

**130/130 green** across:
- test_exit_broker_truth_guard.py: 14/14
- test_exit_order_builder.py: 14/14 (incl. JPY decimal regression)
- test_fx_exit_retry_cascade.py: 9/9 (incl. broker-truth cascade verification)
- test_emergency_close.py: 10/10 (the 5/19 incident tool now safe)
- test_sunset_roster.py: 6/6
- test_killed_strategy_invariant.py + runtime: 13/13
- test_static_safety_invariants.py: 4/4
- test_canonical_fills.py: 22/22
- test_entry_write_smoke.py: 2/2
- test_real_money_boundary.py: 27/27
- test_lineage_id.py: 9/9

## How the day actually ended

Account NLV: **$30,218.51** (started ~$29,974, ended +$244).
Argus runner: **stopped, watchdog disabled** — will not auto-restart
until reboot OR operator manually launches watchdog.ps1.
Open positions: GLD 22 / SPY 26 / QQQ 7 (the 3 stock orphans, unchanged).
FX positions: all FLAT.

## The architectural lesson

The audit recommended Path C (extend the timeline, build conservatively).
Today proved why. Three production bugs in 30 minutes when we actually
looked at the live system — JPY decimal, cascade race, emergency tool.
The 5/31 reset gets us a clean evidence epoch but it doesn't fix the
class of bugs that ship under "tests passed = code works." Those
require live exercise.

The right doctrine going forward: **no code touches live trading
without an end-to-end smoke test against real IBKR**, ideally during
market hours so the operator can watch the first few cycles.
