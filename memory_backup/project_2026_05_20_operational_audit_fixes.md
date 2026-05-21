---
name: 2026-05-20 operational audit + 9 fixes shipped
description: Second 5-agent audit (trade-flow forensics, race hunter, silent-failure hunter, cap/gate auditor, idle-strategy diagnostic) caught 15+ findings the 5/19 cleanup missed. Shipped 9 fixes covering the entry-timeout race that's been firing every FX entry, bracket half-armed cascade, Greek-family fail-open, JPY decimal in flatten_eod, broker-unreachable passthrough (regression from 5/20 morning), pending_fills wiring, orphan sanity check, and cancel-during-fill race. 150/150 tests green. Runner + auto-restart tasks down.
type: project
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
After clearing today's halt and seeing trades resume, the operator asked
for another full audit looking for bugs hurting operations. The 5-agent
operational audit caught **15+ findings** the 5/19 cleanup missed.
**9 fixes shipped same session.**

## The systemic finding (what 5/19 missed)

**The entry-timeout race has been firing every single FX entry since 5/12.**
Order submits → fills at broker → execDetails callback races the 60s
timeout → runner cancels (fails, already filled) → state cleared
INCLUDING `_pending_stop_px/_pending_target_px` → reconciliation adopts
as orphan with SYNTHETIC stops 2-5× wider than the strategy designed.

Impact: every argus FX trade since 5/12 has been running with
- 30% of intended size (NOTIONAL_CAP downsizing the 15-25× sizing formula)
- 2-5× wider stops (synthetic from instrument defaults)
- NO canonical_fills ENTRY row (gate is `order_id == s.entry_order_id`,
  but order_id was cleared)
- NO broker-side OCO bracket (synthetic stops are software-managed only)

This explains the 0-ENTRY canonical_fills problem — it wasn't a write
bug, it was a SYMPTOM of the entry race. Fix #1 below repairs both.

## Fixes shipped (all in this session)

### P0 (5 fixes, blocked correct trade execution)

**#1 — Entry-timeout broker-truth check + preserve pending stops**
`argus_flow/runner_unified.py:_check_order_timeouts` entry branch.
Mirror of the 5/19 exit-side fix. On 60s timeout, query broker FIRST.
If position exists, adopt with `_pending_stop_px/_pending_target_px`
from memory (not synthetic). Submit protective brackets at intended
levels. Only cancel-and-clear on genuine timeout (broker truly flat).

**#2 — Bracket half-armed: cancel stop on target placeOrder failure**
`argus_flow/runner_unified.py:_submit_bracket_orders`. Inner try around
target placement; on exception, explicitly cancel the already-live
stop before returning False. Without this, target throws (Error 110,
throttle, network) → stop alone live at broker → caller emergency-exits
alongside the stop → LONG→SHORT cascade like 5/19.

**#3 — Greek-family executor fail-OPEN → fail-CLOSED**
`helio/ibkr_executor.py:190` (submit_bracket) and `:277` (submit_market).
Both had `except Exception: log.warning("allowing paper path")` for
unexpected errors in `enforce_real_money_boundary`. The X5 sweep missed
this file entirely. If anyone ever flips port to 7496, this would silently
submit live-account orders on ImportError / allowlist read failure / etc.
Now `return None` + ERROR log.

**#4 — JPY decimal regression in flatten_eod_executor**
`ops/flatten_eod_executor.py:83`. Same `round(ref * buffer, 5)` bug
fixed in `_build_exit_order` on 5/19, untouched in this emergency tool.
Daily-loss circuit-breaker tool fires rarely; if it ever fires on a
JPY position (USDJPY today is LONG 60K), Warning 110 rejection. Fixed
to JPY-aware decimals + qualifyContracts (Error 321 prevention)
+ TIF=GTC (broker-day-roll prevention).

**#7 — `_broker_state_allows_exit` broker-unreachable → fail-CLOSED**
`argus_flow/runner_unified.py:_broker_state_allows_exit`. **This was a
regression I introduced on 5/20 morning.** Yesterday's helper returned
`(True, "passthrough")` on broker-unreachable, claiming downstream
fail-closed layers would catch it. But `_submit_real_exit` calls
ib.placeOrder DIRECTLY, bypassing submit_bracket/cluster_exposure
entirely. A network blip during cascade retry would produce the exact
5/19 catastrophe via network failure rather than order race. Now returns
`(False, "broker_unreachable_fail_closed")`.

### P1 (4 fixes, infrastructure hardening)

**#6 — Wire pending_fills into argus _submit_real_entry**
`argus_flow/runner_unified.py:_submit_real_entry` + `_on_fill`.
`helio/pending_fills.py` (write_pending/clear_pending API) has existed
for weeks, wired into forge's submit_bracket, but argus's
`_submit_real_entry` never imported it. `_pending_fills.jsonl` was 0
bytes. Now writes before placeOrder, clears on confirmed fill. Crash
recovery for argus FX is now real.

**#8 — adopt_orphan_position sanity checks**
`argus_flow/runner_unified.py:adopt_orphan_position`. On 5/19 IBKR sent
two corrupted position-updates (position=-1.0 avgCost=-202.13) — an
odd-lot arithmetic glitch. Runner blindly adopted both, submitted real
LimitOrder against negative prices. Now rejects:
- `broker_avg_cost <= 0`
- `abs(broker_qty) < 1` (sub-lot artifact)
- `|broker_avg_cost - last_mid| / last_mid > 20%` (price too far from market)

**#9 — Cascade re-checks broker after cancel sleep**
`argus_flow/runner_unified.py:_check_order_timeouts` 30s and 60s arms.
`_cancel_order_by_id` sleeps 2s during which the original order can
fill — function returns "CANCEL CONFIRMED" but position is actually
closed. Without this re-check, the IOC/GTC retry duplicates the exit.
Now: after cancel sleep, `_read_broker_position()`; if flat, clear
local state + return (skip retry).

## Tests

**12 new tests** in `argus_flow/tests/test_audit_fixes_20260520.py`:
- entry_timeout_has_broker_truth_check
- bracket_cancels_stop_if_target_placeOrder_fails
- ibkr_executor_real_money_fails_closed_on_unexpected
- flatten_eod_jpy_aware_decimals + flatten_eod_uses_gtc_for_fx
- broker_state_unreachable_fails_closed
- submit_real_entry_writes_pending_fills + on_fill_clears_pending_fills_on_entry
- adopt_orphan_rejects_garbage_prices
- exit_cascade_rechecks_broker_after_cancel (30s and 60s arms)
- no_allowing_trade_in_execution_modules (cross-cutting)

Plus updated `test_passes_through_when_broker_unreachable` to reflect
the new fail-closed contract (was asserting the regression behavior).

**150/150 tests green** across `test_audit_fixes_20260520`,
`test_exit_broker_truth_guard`, `test_exit_order_builder`,
`test_fx_exit_retry_cascade`, `test_emergency_close`,
`test_sunset_roster`, `test_killed_strategy_invariant`,
`test_static_safety_invariants`, `test_canonical_fills`,
`test_entry_write_smoke`, `test_real_money_boundary`,
`test_lineage_id`, `test_killed_strategy_runtime_invariant`,
`test_guards_fail_closed`, `test_gld_cap_policy`.

## Operational state

- **argus_flow.runner_unified: STOPPED** (verified)
- **ArgusPreMarketCheck: DISABLED** (the task that auto-relaunched overnight)
- **ArgusFleetStartup: DISABLED** (from yesterday)
- **ArgusWatchdog: Ready** (boot-only, won't fire without reboot)
- **watchdog.ps1: not running**
- **Open broker positions (unchanged):** CADJPY LONG 41,572 (5/15 orphan,
  needs operator flatten before 5/31), GLD 22, SPY 26, QQQ 7
- **FX runner: down** — fixes will activate on next restart

## What's still on the table

From the audit findings still not fixed:

**P2 — post-reset architectural items:**
- placeOrder → state-write race in 6 sites (pre-allocate order_id via
  ib.client.getReqId before placeOrder) — silent-failure #4
- FX sizing formula 15-25× over-sized (`fx_units_for_risk` at
  `runner_unified.py:1551`) — cap/gate big finding. Strategies allocated
  0.0 now so not urgent.
- Loosen TOTAL_NOTIONAL_CAP 1.5× → 2.0× anchor — cap/gate recommendation
- Partial-fill state ordering on bracket legs (Race #5)
- Add RAW_SIZE_RATIO sanity gate at sizing time
- Delete duplicate asset-class cap from fleet_sizing.json
- Forge stale heartbeat cleanup (multi_orb 13d, spy_mean_rev 20d, etc.)

**Operator action required:**
- Flatten CADJPY LONG 41,572 before 5/31 (5/15 orphan with synthetic stop)
- Formally kill apollo/hermes/titan (3-4 day stale heartbeats)
- Decide on the 3 stock orphans (GLD/SPY/QQQ)

## The honest read

The 5/19 audit caught 5 bugs. Today's audit caught 15+ more, INCLUDING
a regression I introduced yesterday morning (#7). Code that "passes
tests" still requires live exercise. The pattern of bugs:

1. Race conditions where local state and broker state diverge (cascade
   exit race fixed 5/19, entry race fixed today, fill-during-cancel
   fixed today, partial-fill bracket bug remaining)
2. Fail-OPEN exception handlers in execution paths (X5 swept some,
   today swept 4 more)
3. Modules that exist but aren't wired (pending_fills, originally)
4. Trusting broker-provided values without sanity checks (negative-
   price orphan adoption fixed today)

These are systemic — they exist because trading code interacts with
asynchronous broker callbacks, and tests can't reproduce the timing.
The right doctrine going forward is **live exercise during market
hours before treating any execution-path change as shipped.**

## What happens next

The bot is in safe state — runner down, watchdogs disabled, fixes
ready to activate on next start. The 9 fixes shipped today directly
address the highest-impact items from the audit. The remaining P2
items are architectural and can wait for post-5/31 reset.

**Real-money posture:** the 5/19 audit said path C (wait for clean
post-reset evidence + bootstrap CIs above 1.20). That stands. Today
reinforces it — we just spent a session fixing 9 bugs that had been
shipping green. The discipline of "code passes tests ≠ code works"
is the doctrine that should gate any 7/1+ → 10/1+ → real-money
decision.
