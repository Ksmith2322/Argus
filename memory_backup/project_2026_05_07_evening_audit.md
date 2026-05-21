---
name: 2026-05-07 evening 3-agent verification audit (post-roadmap drift check)
description: Trust-but-verify pass on the 5/7 morning audit. Found 4 surprises - shipped P1/P2 fixes the morning audit missed, two strategies in wrong mode, status panel lying, vix_intraday past 5/15 threshold. Real gaps are now Layers 6/8 + real-money boundary + zero test coverage.
type: project
originSessionId: ca6e24c7-7756-4b66-a0df-d339b1453b20
---
# 2026-05-07 evening audit — 3 parallel agents, post-roadmap drift check

Done same day as the 4-agent morning audit (`project_2026_05_07_week_audit.md`). Three agents in parallel: code verification, capital allocator + readiness gate, 5/31 path + ops health. **Goal: build on morning audit, not redo it. Catch what shifted between morning and evening.**

## The 4 surprises this audit found

### 1. Two strategies are silently in the WRONG mode (4/30 game-plan instructions never executed)
- `forge/logs/mamba/heartbeat.json:6` shows `mode: research_only` despite 4/30 plan flipping to `--live`
- `forge/logs/gdx_gld/heartbeat.json:5` shows `mode: signal_only` despite 4/30 plan flipping to `--live`
- **Implication**: any KILL verdict on these two at 5/15 is *contaminated* — they weren't actually trying to trade.
- **5/8 fix**: 30-min ops triage. Restart with correct flags.

### 2. The status panel is LYING about CADJPY
- `argus_flow/logs/runner_unified.log`: CADJPY blocked 5/7 15:17→15:57 UTC by RECON_DRIFT (broker_qty=0 vs local LONG), repeating every ~5min for 40min
- `argus_flow/logs/fleet_status.json:27-33`: same strategy reports `entries_blocked: false`
- **Stale-heartbeat class bug** — dashboard doesn't reflect runtime. 5/15 decisions made off this panel will be wrong.
- **Fix**: `entries_blocked` field needs to read from runtime state, not a separate stale source.

### 3. vix_intraday is already PAST the 5/15 threshold
- Heartbeat: `trade_count: 61` (not the n=21 the prep doc claimed)
- Projects to n≈91 by 5/15, n≈141 by 5/31 — clears 75-trade "serious answer" bar in `project_decisive_test_protocol.md`
- **5/15 ceremony for vix_intraday is PF-gate only**. Sample size is decided.

### 4. The morning 5/7 audit was stale by the time it was written
The morning audit's "still unaddressed" list — most of those root fixes shipped same-day:

| Morning audit claim | Code-truth verdict | Evidence |
|---|---|---|
| Sizing-formula bug "still broken" | **FIXED** — `safe_position_size()` helper | `helio/strategy_common.py:92-172` |
| Persistent fill queue "real fix needed" | **SHIPPED** | `helio/pending_fills.py` (full module) |
| Pre-entry orphan check "absent" | **SHIPPED** | `helio/ibkr_execution.py:349-371` |
| Margin-aware caps "needed" | **SHIPPED** | `helio/cluster_exposure.py:141-156, 451-468` |
| Per-strategy notional cap "needed" | **SHIPPED** | `helio/cluster_exposure.py:162-189, 426-443` |
| Circuit-breaker hysteresis "needed" | **SHIPPED** | `ops/daily_loss_circuit_breaker.py:45-55` |
| multi_orb window 32→24 revert | **DONE** | `forge/multi_orb/runner.py:71` value=24, version `v4_qqq_window24_revert` |
| FX cluster fix from 5/1 | **VERIFIED** | `helio/cluster_exposure.py:125, 200-207, 407-408` |
| Integer FX qty fix | **VERIFIED at all 4 sites** | `argus_flow/runner_unified.py:2291, 2380, 2717, 2738` |

**Of the 5 P1 items in the morning roadmap, 4 are code-done; only operator-action items (verify mamba launch, diagnose argus pairs) remain.**

## What's actually missing now

### Hard blockers for real-money go-live
1. **Real-money boundary plumbing is env-var-only.** No `real_money_enabled` flag, no allowlist, no `orderRef` tagging, no account-ID validation, no mismatch-detection daemon. Only `IBKR_PORT=7497` separates paper from a misrouted live order — exactly the anti-pattern `project_real_money_boundary.md` says must NOT be sufficient.
2. **Layer 6 (strategy scorecard) is policy-only.** No `helio/scorecard.py`. The decision engine in `ops/dashboard.py:4706-4858` is a different shape (action + confidence_pct), not the locked spec (final_score + factor decomposition).
3. **Layer 8 (capital allocator tier ladder) is policy-only.** `argus_flow/configs/allocation_factors.json` is a flat per-strategy multiplier, not the $50→$500→...→$100K ladder with 25%/50% concentration caps.

### Soft blockers (waiver-eligible)
- **Readiness gate scorecard: 5 GREEN / 2 YELLOW / 7 RED / 6 UNKNOWN**. ≤3 waivers allowed.
- **Zero test coverage on the 3 RM candidates.** No `test_vix_intraday*`, no `test_pnl_reconciliation*`, no `test_persistent_fill_queue*`. Root fixes have no regression guards.
- **Kill-engine threshold mismatch.** Code uses -2/-4% daily; doctrine in `project_kill_pause_engine.md` says -10/-20/-25% account panic. Different doctrines — pick one and reconcile.
- **VIX>30 force-close-all-SHORT_VOL trigger is memo-only, no code.**

### Recurring ops bleed (still happening 5/8)
- Risk oversight at **RED** (`fleet_status.json:300-302`). HALT.flag SET (-2.17% PAUSE tier).
- gbpusd orphan adoption cycled LONG↔SHORT 5× in 20 min on 5/6 (adoption masks, doesn't prevent)
- 5 TWS disconnects in 10 min on 5/7 morning (319 total in current runner_unified.log)
- ArgusWatchdog last_result=1 since 4/20 (29.9 days)
- 2/14 scheduled tasks failing

## Recommended ordering for 5/8 (Friday)
1. **30-min ops triage**: flip mamba to `--live`, gdx_gld out of `signal_only`, clear CADJPY RECON_DRIFT, restart ArgusWatchdog. Without this, 5/15 verdicts are corrupted.
2. **Fix the lying `fleet_status.json` `entries_blocked` field** — read from runtime state.
3. **Start `helio/real_money.py` module** + allowlist + `orderRef` tagging. Single biggest 5/31 blocker.
4. **Add 1+ PnL-reconciliation test** for vix_intraday (only viable RM candidate).
5. **Defer scorecard + tier-ladder to Week 4** — docs work, not code-blocking.

## Bottom-line forecast
- **5/31 freeze**: ~85% on time
- **5/31 real-money go-live**: ~15% — defer to ~6/30 is the honest path
- Real PnL last week was +$477 (within SPY noise). 6+ readiness-gate items hard-RED vs ≤3 waiver budget.
- **vix_intraday is the lone realistic RM candidate**. Even it requires PF-hold over next 30 trades + boundary module shipping.

## Cross-references
- Morning audit: `project_2026_05_07_week_audit.md` (still good for per-strategy verdicts and incident timeline; outdated for "still unaddressed" claims)
- 5/7 prep doc claimed vix_intraday n=21 — actual n=61 per heartbeat. Prep doc is stale.
- Master breakdown: `project_bot_master_breakdown_20260426.md` — Layers 6+8 still policy-only as of this audit, despite Week 3 (5/8-5/14) being implementation week per the breakdown.
