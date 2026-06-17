---
name: Fix-now queue completed (2026-05-18)
description: 7-item Codex audit fix-now queue shipped — GLD cap, phantom filter, FX boundary, 2 static-invariant test suites, ENTRY canonical writes, FX TIF=GTC. Plus 8 pre-existing real_money_boundary test failures fixed in passing.
type: project
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
7 fix-now items + 1 collateral pre-existing test repair, all shipped 2026-05-18.

**Why:** Codex second-sweep audit (2026-05-18) plus my 6-agent audit identified
7 items as fix-now (not fix-later). User said "lets get it all done" → cleared
the queue in one session before the 5/31 strategy freeze.

**How to apply:** When user asks "what landed on 2026-05-18?" or about any of
the items below, this entry is the index. The pull-request-equivalent diff
list:

1. **GLD cap revert** — `argus_flow/configs/fleet_sizing.json` v9 → v10. Reverted
   `forge_gld_pm_long: 2.2 → 0.4`. The Saturday 2.2× fix was silently overridden
   by `cluster_exposure.PER_STRATEGY_NOTIONAL_CAP_X = 0.4`; aligned to smallest
   binding cap rather than chase 4-layer reconciliation.

2. **Phantom quarantine helper** — `helio/roi_filter.py` + `argus_flow/tests/test_roi_filter.py`
   (11 tests). Centralized filter for ROI consumers. PHANTOM_TRADES explicit
   list, KILLED_STRATEGY_CUTOFFS dict, threshold-phantom (>$1K), backfill
   null-anchor rows. Routes: filter_fills(), filtered_canonical_fills(),
   assert_min_entries().

3. **Argus FX real_money boundary bypass closed** — `argus_flow/runner_unified.py`
   `_submit_real_entry` got Guard 0 (enforce_real_money_boundary) before the
   existing halt/IdealPro/cluster guards. Fails closed on any boundary error.
   Also flipped port default 7496 → 7497 in 5 files (runner_unified, download_ibkr_bars,
   emergency_close, position_monitor, helio/runner) — caught by static test below.

4. **Static safety invariants** — `argus_flow/tests/test_static_safety_invariants.py`
   (4 tests, all green): no bare 7496 default outside real-money modules,
   every submit_bracket call passes strategy_label, no direct ib.placeOrder
   outside the allowed executor/runner/operator allowlist, every Future()
   construction uses qualify_front_month_future (warning-level). Caught the
   5 real 7496 defaults bug in passing; allowlisted operator one-shots
   (close_orphan_*, flatten_*) and tech-debt forge runners (spy_trend_follower,
   tom_international, etc.) explicitly.

5. **Killed-strategy invariant** — `argus_flow/tests/test_killed_strategy_invariant.py`
   (4 tests, all green): every strategy in KILLED_STRATEGY_CUTOFFS has
   factor=0.0 in allocation_factors.json, no_restart=True in fleet_monitor
   SYSTEMS, NOT in real_money_allowlist, cutoff is ISO YYYY-MM-DD. Caught
   `forge_nq_london_close` missing `no_restart: True` in fleet_monitor.py
   (killed 5/13 but fleet_monitor would still auto-restart it) — patched.

6. **ENTRY writes for canonical_fills.jsonl** — `helio/ibkr_execution.submit_bracket`
   now writes a side="ENTRY" canonical row right after entry-fill confirmation
   (covers every forge runner that uses the central executor). Argus
   `runner_unified._on_fill` got an equivalent ENTRY write inside its real-fill
   path. Codex observation: 329 EXIT / 0 ENTRY → fleet ledger had no entry
   anchor for orphan reconciliation. Both writes are try/except-wrapped to
   preserve the "canonical log cannot break a live trade" invariant.

7. **FX TIF=DAY → GTC** — `argus_flow/runner_unified._build_exit_order`
   upgrades DAY → GTC for CASH instruments. Speculative Codex hypothesis on
   the recurring argus_gbpusd EXIT FAILED cascade: a DAY-flagged FX LMT can
   silently expire around the broker's daily roll. Retry escalation already
   ended in GTC; this collapses the retry chain. New test
   test_fx_day_is_upgraded_to_gtc; existing test relaxed.

**Collateral repair (8 pre-existing failing tests):** `test_real_money_boundary.py`
had 8 tests that didn't monkeypatch the module-level `REAL_MONEY_ENABLED`
constant (added later as belt-and-suspenders alongside allowlist.global_enabled).
Added a `real_money_module_enabled` fixture; all 27 now green.

**Test scoreboard at session end:**
- Static safety invariants: 4/4
- Killed-strategy invariant: 4/4
- ROI filter: 11/11
- Exit order builder: 8/8
- Canonical fills: 22/22
- Argus dual-write: 8/8
- Forge dual-writes: 7/7
- Gld_pm_long dual-write: 5/5
- Real-money boundary: 27/27
- Total in touched files: **96/96 green**

**Files changed (summary):**
- argus_flow/configs/fleet_sizing.json (GLD 2.2→0.4)
- argus_flow/runner_unified.py (boundary guard, port default, ENTRY write,
  FX TIF upgrade)
- argus_flow/ops/download_ibkr_bars.py (port default 7496→7497)
- argus_flow/ops/emergency_close.py (port default)
- argus_flow/ops/position_monitor.py (port default)
- helio/runner.py (port default)
- helio/ibkr_execution.py (ENTRY canonical write after fill)
- helio/fleet_monitor.py (forge_nq_london_close no_restart=True)
- helio/roi_filter.py (new — phantom quarantine helper)
- argus_flow/tests/test_roi_filter.py (new)
- argus_flow/tests/test_static_safety_invariants.py (new)
- argus_flow/tests/test_killed_strategy_invariant.py (new)
- argus_flow/tests/test_exit_order_builder.py (updated assertion)
- argus_flow/tests/test_real_money_boundary.py (fixture for 8 tests)
