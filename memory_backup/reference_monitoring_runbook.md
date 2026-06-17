---
name: Monitoring-Mode Runbook
description: Weekly checklist for hands-off operation. Updated 2026-04-18 for broker-equity-dynamic sizing + tier system + auto-clear + promotion-readiness pipeline.
type: reference
originSessionId: 3a7a2a39-5348-43c1-8feb-cb0d2b0662ae
---
# Monitoring Runbook — Helio Fleet (2026-04-18 refresh)

Major changes since prior version:
- Sizing is **broker-equity-dynamic** now. No $10K hardcode anywhere. `argus_flow/configs/fleet_sizing.json` is the one knob.
- **Confidence tiers** auto-set per strategy from live evidence (unproven 0.5% → exceptional 3%). See `/api/strategy_tiers`.
- **Auto-clear PAUSE_ENTRIES**: fleet_monitor removes watchdog-created gateway pauses once all 3 Argus pair heartbeats show broker_connected=true. Manual clearing should no longer be needed in the normal case.
- **Canonical promotion pipeline**: `helio.promotion_readiness` runs nightly via `ops/run_cohort_report.ps1`, writes `argus_flow/logs/promotion_readiness_report.json`, surfaces at `/api/promotion_readiness`.
- **Gateway status banner** at top of dashboard shows broker equity + per-pair state.
- **Blueprint §18 + strategy cards** govern what's on the shortlist: gdx_gld, gld_pm_long, wick_gbpusd, apollo. Everything else is research-only.

## Daily check (60 seconds)

1. Open dashboard: `http://localhost:8080`
2. **Gateway banner at top**: should be GREEN. Yellow = `PAUSE_ENTRIES` active (if persists >10 min investigate). Red = broker unhealthy or Argus pair stale.
3. Skim fleet status: every system OK.
4. Skim recent trades table: no surprise positions, no `experiment_valid: false` on a configured pair.

## Weekly check (10 minutes — Sunday recommended)

### 1. Promotion-readiness first pass
```bash
cd C:/Argus/repo && C:/Argus/.venv/Scripts/python.exe -m helio.promotion_readiness
```
Read the next-action column. Today everything says `OBSERVE_MORE` or `NO_LIVE_EVIDENCE`. Flip to:
- `REVIEW_GATE_PASSED` = 30+ valid trades, PF≥1.20 → freeze new strategy families 30 days, focus on execution quality.
- `PROMOTION_ELIGIBLE` = 60+ valid trades, PF≥1.30 → run canonical `argus_flow.ops.promotion_gate_v2`, possibly promote tier/size.
- `KILL_CANDIDATE` = 20+ trades, PF<1.0 → kill via kill discipline process.

### 2. Signal-frequency drift
```bash
cd C:/Argus/repo && C:/Argus/.venv/Scripts/python.exe -m argus_flow.ops.signal_frequency_tracker
```
Any strategy tagged `severe` (<20% of replay trade rate): investigate. Any tagged `triggers_firing_but_gates_eating_them`: gate-side fix likely.

### 3. Fleet perf summary
```bash
cd C:/Argus/repo && C:/Argus/.venv/Scripts/python.exe -m helio.fleet_perf_summary
```
Look at the `live_window` (7d default) column — that's current reality. Ignore `all_time` for gdx_gld (back-fill dominates).

### 4. Gateway + control plane
Check `argus_flow/logs/fleet_status.json`:
- `control_files.PAUSE_ENTRIES.present` should be `false`. If true for >10 min, auto-clear failed — investigate why gateway isn't reporting healthy.
- `risk_state.disagreement` should be `null`. If populated, one of {drawdown_pause, portfolio_guard} disagrees with oversight.
- `systems.argus.recent_fatal_count` should be 0. Anything non-zero means runner_unified.log has recent FATAL lines.

### 5. Apollo forward returns
```bash
head -3 C:/Argus/repo/apollo/logs/forward_returns.jsonl
wc -l C:/Argus/repo/apollo/logs/forward_returns.jsonl
```
Sample count should grow ~5-15/week during earnings season. If flatlines, apollo.ops.backfill_forward_returns not running.

## Red-alert situations (act immediately)

| Symptom | Action |
|---|---|
| Gateway banner RED for >5 min | Check IBKR Gateway is logged in. If auto-login broke, re-login manually. |
| `PAUSE_ENTRIES` stays up >10 min after gateway healthy | Auto-clear logic failed. Verify `_argus_all_brokers_healthy()` in fleet_monitor returns True for current state. |
| Fleet monitor crash-loop alert fires | A system is failing restart repeatedly. `argus_flow/logs/crash_loop_alerts.json` has the history. Root-cause before clearing. |
| Discord failures JSONL growing | Webhook URL invalid or Cloudflare-blocked. Check the webhook from browser. |
| Any strategy hits kill-rule | Kill discipline process: move to research_only, update memory, document in `project_fleet_master_<date>.md`. |
| `portfolio_guard.allowed=false` for >1 hour while oversight=GREEN | Real block. Check the `reason` field — likely max_directional_bias or concentration. |

## Shortlist strategies (per blueprint §18.6)

| # | Strategy | Card path | Current state | Next gate |
|---|---|---|---|---|
| 1 | forge_gdx_gld | `research/strategy_cards/forge_gdx_gld.md` | 0 live trades (back-fill excluded from tier stats) | Needs fill-truth pipeline + live trades to enter review |
| 2 | forge_gld_pm_long | `research/strategy_cards/forge_gld_pm_long.md` | 1 live trade (+$199) | Need 10+ more for a first review |
| 3 | forge_wick_gbpusd | `research/strategy_cards/forge_wick_gbpusd.md` | 0 live trades, sparse cadence (~13/yr) | Accept slow validation; not a near-term producer |
| 4 | apollo_earnings_drift | `research/strategy_cards/apollo_earnings_drift.md` | Research-only, 50 forward-return records | Wait for n=100, then decide on execution path |

Everything else in the 19-system fleet is research-only unless someone writes a strategy card.

## When to come back to active development

Triggers that warrant breaking monitoring mode:
- Any shortlist strategy hits review gate (30 trades, PF≥1.20) → re-evaluate promotion
- Apollo forward-return sample reaches 100 records → decide on execution path
- Any strategy hits kill rule (from its card) → execute kill discipline
- Gateway auto-clear fails more than once in a 30-day window → debug watchdog / auto-clear logic
- Fleet crash-loop alert fires → investigate immediately

## What's NOT a reason to come back

- Wanting to add a new strategy "just to see" → blocked by One-In/One-Out rule in `project_validation_charter.md`.
- Reading Discord noise and reacting to individual alerts.
- "Maybe if I lower threshold X..." without data-driven reason.
- Building new sleeves from the original blueprint §5 list — all 12 sleeves are parked until Phase 3 promotes one strategy.

## Key paths

- Repo: `C:\Argus\repo`
- Venv: `C:\Argus\.venv\Scripts\python.exe`
- Memory: `C:\Users\ksmit\.claude\projects\c--Argus\memory\`
- Blueprint (applied scope is §18): `C:\Argus\repo\PROFIT_MAX_SYSTEM_BLUEPRINT_20260418.md`
- Strategy cards: `C:\Argus\repo\research\strategy_cards\`
- Dashboard: http://localhost:8080

## Canonical report paths

- Fleet status: `argus_flow/logs/fleet_status.json`
- Fleet perf summary: `argus_flow/logs/fleet_perf_summary.json`
- Fleet perf history: `argus_flow/logs/fleet_perf_history.jsonl`
- Promotion gate: `argus_flow/logs/promotion_gate_report.json`
- Promotion readiness: `argus_flow/logs/promotion_readiness_report.json`
- Signal frequency: `argus_flow/logs/signal_frequency_report.json`
- Signal freq history: `argus_flow/logs/signal_frequency_history.jsonl`
- Apollo forward returns: `apollo/logs/forward_returns.jsonl`
- Broker equity history: `argus_flow/logs/broker_equity_history.jsonl`
- Kill discipline: `argus_flow/logs/kill_discipline_report.json`

## Canonical API endpoints

All emit `_meta.fresh` + `_meta.mtime_s_ago`:
- `/api/fleet_health` — 19 systems + drift + control files
- `/api/gateway_status` — broker + per-pair state for top-of-page banner
- `/api/fleet_perf` — live_window + all_time PnL views
- `/api/broker_equity_curve` — true IBKR equity time-series
- `/api/promotion_gate` — canonical promotion verdicts
- `/api/promotion_readiness` — review gate + canonical gate status
- `/api/strategy_tiers` — tier + risk_pct + stats per strategy
- `/api/signal_frequency` — live-vs-replay drift (full)
- `/api/shortlist_drift` — live-vs-replay drift (filtered to shortlist)
- `/api/apollo_forward_returns` — full record + aggregate
- `/api/kill_discipline` — killed-config history
- `/api/risk_state` — 3 risk files merged + drift flag
- `/api/recent_trades` — chronological trade table with account context
- `/api/fleet_equity_curve` — trade-derived cumulative curve
- `/api/ops_summary` — one-call consolidated snapshot

## Daily scheduled tasks (Windows Task Scheduler)

| Task | Schedule | Purpose |
|---|---|---|
| ArgusCohortReport | nightly | fleet_perf + apollo backfill + signal_freq + promotion_readiness |
| ArgusWatchdog | continuous | process supervision |
| ArgusWiFiDisable | 00:00 daily | WiFi radio off at midnight |
| ArgusWiFiEnable | 07:00 daily | WiFi radio on 7am |
| ArgusUSBBackup | weekly | local backup |
| ArgusGitBackup | weekly | repo backup |
| ArgusWeeklyDigest | weekly | ops digest |
| ArgusManagedTruth | continuous | IBKR managed-truth reconciliation |
