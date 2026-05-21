---
name: Four-agent audit synthesis — 2026-04-23
description: Consolidated findings from 4 parallel audits (dormant strategies, operational gaps, pending items, 5/1 plan review). Master reference for what's missing, blocked, and next.
type: project
originSessionId: 81afc97b-10bf-49fd-ba79-e5a89a7f400b
---
Triggered by user 2026-04-22: "have a few agents verify whats missing, what
pendings, next steps, as well as review 5/1 implementation/clean up."

Four agents ran in parallel. This memo consolidates their findings.

## Source memos (detail)

- `project_dormant_strategies_inventory_20260422.md` — Tier-1 dormant research
- `project_event_trader_architecture_20260422.md` — executor layer design
- `project_hands_off_20260422.md` — 5/1 collection window + deliverables
- `project_risk_dashboard_concept_20260423.md` — health-metric dashboard future

## Highest-ROI unaddressed items (ranked)

### Tier A — Activate dormant research (fast, proven edge)
1. **fomc_drift**: PF 1.58 / 215 trades, no runner. **2hr job to wrap.**
2. **tom_international**: PF 1.31 / 1,249 trades — needs slippage model before activation (per-trade edge is tiny)
3. **form4_cluster**: RETIRED 2026-04-22 to `_research_archive/` — PF 0.35, negative

### Tier B — Infra gaps (affect operations)
1. **ArgusCohortReport re-register with credentials** — still pending user password; nightly cohort reports keep stalling on disconnect (partially mitigated by managed_truth_loop daemon as of 2026-04-23 reset)
2. **Watchdog → Windows service via NSSM** — prevents supervisor dying mid-session; install script exists at `ops/install_watchdog_service.ps1`, needs user to install NSSM + admin password
3. **wick_gbpusd fix-or-kill** — phantom-close bug + no --loop support; 5 months / 0 trades wasted

### Tier C — Dashboard gaps (visibility)
1. **5 invisible forge strategies added to fleet_monitor** ✅ DONE 2026-04-22 (multi_orb, vix_intraday, spy_mean_rev, nq_london_close, aud_asian_breakout)
2. **CADJPY walkforward ERROR** ✅ FIXED 2026-04-22 (string-direction config causes `'str' object has no attribute 'get'` — added isinstance guard)
3. **Dashboard equity curve missing 13 strategies** ✅ FIXED 2026-04-22 (spec list expanded from 8 → 21)

### Tier D — Architecture/design direction (future)
1. **event_trader executor layer** — unlocks atlas + themis + future news signals. Needs 5/1 atlas validation first.
2. **Market Health dashboard** — observation-only Tier 1, post-5/1
3. **Operational maturity report** (`ops/operational_maturity.py`) — 5/1 deliverable, per-strategy VALIDATED/EMERGING/DEGRADED verdict

## False-positive pendings (clean up from memory)

- `gld_pm_long --loop fix` — marked pending in post_audit but done (verified 2026-04-21)
- `RESET_DRAWDOWN flag cleanup` — done 2026-04-22
- `drawdown-breaker minimum-peak floor` — already deployed (verified via log "DRAWDOWN BREAKER: reset (below_min_peak_floor)")
- `Oracle Polymarket` — paused/removed, some old docs still list as LIVE

## 5/1 scope additions (from agent review)

Original 5/1 plan: operational_maturity.py, watchdog hardening, spy_mean_rev dup reap, April SPY delta.

**Must-add (per agent review):**
- Post-clamp sample-sufficiency gate (most strategies will verdict INSUFFICIENT_DATA — state explicitly)
- Signal-floor-per-session alert in watchdog (not just heartbeat presence)
- Nightly reconcile sweep on canonical_fills (dedup / drift detection)
- Baseline anchor-ts declared on SPY delta (post-reset = 2026-04-23)

**Should-add:**
- Remove-best-3 concentration column in maturity report (charter §Kill Rules)
- Kill-rule auto-flag column (not auto-execute)
- Honest-dollar view on all 5/1 numbers (post-clamp period only now)

## Dependency map (why sequence matters)

1. Reset day — 2026-04-23 ✅ COMPLETE
2. Data collection — 2026-04-23 → 2026-05-01 (9-day window)
3. Maturity report — 5/1 (needs data)
4. Atlas validation — 5/1 (needs data)
5. Strategy gating decisions — post-5/1 (needs validation)
6. Dormant strategy activation (fomc_drift) — post-5/1 (gate on validation)
7. event_trader executor — post-atlas-validation (gate on signal being predictive)
8. Health dashboard — post-event_trader (UI on proven signals)

Shortest path to 200% target: activate fomc_drift (2hr), validate the 15 existing strategies' live-vs-backtest drift (5/1 report), apply tier-based sizing growth. Leverage discussion reserved until edge is proven live.
