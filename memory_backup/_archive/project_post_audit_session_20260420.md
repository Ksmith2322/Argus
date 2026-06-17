---
name: Post-audit session — 2026-04-20 PM
description: After the multi-agent audit debate, knocked out 8 action items. 3 design proposals pending user green-light. Apollo forward_returns backfill was stalled 4 days because ArgusCohortReport scheduled task failing.
type: project
originSessionId: 3a8e0a94-b591-4160-81af-20ab2d227878
---
Follow-on work after the 2026-04-20 scope_down runner wiring + multi-agent audit.

**🗂️ 2026-04-23 STATUS UPDATE (this section supersedes the below):** Most items
from this memo have been completed or superseded. See `project_session_20260423_wrap.md`
for the authoritative state. Key items below that are NOW DONE:
- ✅ sector_rot writer gated (2026-04-20)
- ✅ 6 orphan locks removed (2026-04-20, then +6 more 2026-04-22)
- ✅ CLAUDE.md cohort text fixed
- ✅ Registry paths (daily_report) corrected
- ✅ Apollo backfill unstalled
- ✅ `RESET_DRAWDOWN` flag removed (2026-04-22)
- ✅ gld_pm_long --loop wired (was already fixed; confirmed 2026-04-21)
- ✅ drawdown-breaker minimum-peak floor already deployed (verified; no further action)
- ✅ ArgusManagedTruth Interactive-Only bug — WORKED AROUND via new `managed_truth_loop`
  daemon (2026-04-23); the task re-registration with creds is still nice-to-have but
  no longer critical for realtime dashboard updates.

**STILL OPEN from this memo:**
- ⏳ wick_gbpusd fix-or-kill (cohort-blocked phantom-close bug + no --loop support)
- ⏳ NSSM install + watchdog→Windows service (needs admin action)
- ⏳ gld_pm_long "launcher one-shot vs --loop" — NOTE: this was already fixed on
  2026-04-21; the memo below lists it as pending but it is NOT pending anymore.


## What was fixed

| # | Item | File | Result |
|---|---|---|---|
| 1 | sector_rot writer gated | `forge/sector_rot_confidence_writer.py` | `SECTOR_ROT_WRITER_ENABLED=False` — nightly short-circuits with msg. Use `--force` for audit reruns. |
| 2 | 6 orphaned locks removed | `argus_flow/logs/_locks/` | apollo_earnings / discord_watcher / hermes_gap / runner_c7805 / titan / weekly_pair_onboarding |
| 3 | CLAUDE.md cohort text | `CLAUDE.md:15-18` | Corrected to match runtime: GBP/USD + USD/JPY at paper, CAD/JPY at watcher |
| 4 | Legacy cohort runner metadata | `argus_flow/ops/fleet_registry.py:68-84` + `argus_flow/ops/daily_report.py:23-30` | Points at actual `*_mtf_paper_v1.json` / `*_range_paper_v1.json` files |
| 5 | Apollo forward_returns unstalled | `apollo/logs/forward_returns.jsonl` | +37 records (116 → 153). Up through 2026-04-17 scan. |
| 6 | RESET_DRAWDOWN flag armed | `C:/Argus/repo/RESET_DRAWDOWN` | Auto-consumed by `can_enter` on next entry attempt — all pairs currently FLAT so it hasn't fired |

## Root causes identified

- **Apollo backfill stalled** because Windows Task Scheduler `ArgusCohortReport` failed at 2026-04-20 16:14 with error -2147020576 (ERROR_NO_SUCH_LOGON_SESSION). The task is set to "Interactive only" → doesn't run when user RDP session is disconnected. **Fragile**: re-register with "run whether user is logged on or not" + stored password for persistence.

- **CADJPY "missing" from cohort_report** is intentional — config declares `stage=watcher`, daily_report correctly filters to paper-stage only. Not a bug. CADJPY promotes to PAPER when trade-count qualifies.

- **wick_gbpusd 0 trades in 5 months**: entry logic is correctly selective (design target 13/yr). BUT has a phantom-close bug in paper-position exit handler. 2-week diagnostic fix.

- **gld_pm_long 2 trades instead of 4-5**: launcher runs at 23:00 UTC one-shot, strategy's signal hours are 18/19/20 UTC. Structurally can't fire in window. Either redeploy as `--loop` daemon covering 18-20 UTC, or kill.

- **Drawdown pause glitch**: formula `(peak-current)/|peak|` with 5% threshold bites on tiny peaks. +$5→+$2 = 60% "drawdown" despite net positive. Design fix: minimum-peak floor of 10R before arming breaker.

## Pending (user green-light needed)

1. **Re-register ArgusCohortReport** — change to "run whether logged on or not" + stored creds. Without this, nightly backfill / cohort reports will keep stalling on disconnect.
2. **runner_unified drawdown-breaker minimum-peak floor** (task 21 proposal). Touches trade logic; resets cohort count.
3. **Watchdog → Windows service** (task 22 proposal). Register `ops/watchdog.ps1` as auto-start service. Reduces ~3hr manual recovery to automatic.
4. **gld_pm_long fix-or-kill decision**. Either rewire as `--loop` daemon or kill the strategy.
5. **wick_gbpusd phantom-close bug fix**. 2-week diagnostic; doesn't change entry selectivity.

## Still not close to $10K funding

Post-audit honest read: no strategy is near promotion. gdx_gld 87 "trades" are all backfill; 0 live. Best live accumulator is argus_usdjpy at 3 valid trades + 1 week observed; needs 27 more valid trades + ~6 more weeks calendar. Multi-month timeline minimum. Flipping RESEARCH_ONLY on Tori/Cue Banks isn't a shortcut — both have placeholder `--live` and no IBKR execution bridge.
