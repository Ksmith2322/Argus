---
name: 5/7 next-session prep — assessment + cull/improve decisions
description: What to check first when the user returns 2026-05-07. Decision criteria per strategy after a week of post-restart trading data.
type: project
originSessionId: 3848919f-b95e-42f4-8fc4-1ed3ac86dd92
---
# 5/7 session prep — read this first when user returns

User's last session was 2026-04-30 evening. Plan: check periodically, deep work resumes 2026-05-07 (Wednesday). Goal of 5/7 session: assess gained info, verify trades have improved, decide kill/improve per strategy.

## 2026-05-01 reactive fixes (operator returned briefly during the session)

Three production bugs caught during live trading, all silent until trade-attempts. Each was BLOCKING argus FX entries despite "OK" status on the dashboard. **Worth re-reading on 5/7** because they expose how single-strategy validation can hide multi-layer gate failures.

### Fix 1 — Watchdog auto-respawn proved itself
ArgusWatchdog process crashed sometime overnight 4/30→5/1. The new `ops/watchdog_health_check.py` (built 4/30) detected the stale `watchdog_managed.log` and auto-respawned at 7:33 AM 5/1. Self-heal validated end-to-end in production.

### Fix 2 — Tori heartbeat + watchdog cosmetic fixes (commit 74667da)
- `forge/tori/runner.py:run_live()` was missing heartbeat write. Dashboard saw it as STALE; runner was actually fine on 4hr cycle. Added heartbeat at top of each loop iteration.
- `ops/watchdog.ps1` reported `Futures=DOWN` forever because `Is-RunnerAlive 'futures'` always returned false (futures lane was killed in 4/07 consolidation). Now reports `N/A` when `$futuresConfigs.Count == 0`.

### Fix 3 — Argus DD_RAMP stuck peak_pnl (no commit, state file edit)
`argus_flow/logs/_risk/portfolio_risk_state.json` had `peak_pnl=4.97R` from a prior session, with `current_pnl=0.36R`. dd = 92.7% → DD_RAMP sized all FX trades to 0.2x → trades fell BELOW IBKR's $25K IdealPro min → runner refused to submit. Fix: edit state file to set `peak_pnl = current_pnl`, restart argus. peak_pnl is sticky (only goes up via line 3840), reset only via:
- session boundary (UTC midnight) — but only clears `_drawdown_pause`, NOT peak_pnl
- direct state file edit (the only real reset)
- `RESET_DRAWDOWN` flag — also only clears pause, NOT peak

**Note for 5/7:** if argus stops trading again with DD_RAMP firing in logs, this is the recurrence. Inspect `portfolio_risk_state.json` peak_pnl vs current_pnl ratio.

### Fix 4 — FX cluster cap (commit 7daa29c)
After peak_pnl reset, argus exposed second blocker: `helio/cluster_exposure.py` `SINGLE_INSTRUMENT_CAP_X=0.6` was treating FX same as stocks. Full GBPUSD trade ($31K) > 0.6 × $31K = $19K cap, but IdealPro min is $25K — impossible window. Fix: separate `FX_SINGLE_INSTRUMENT_CAP_X = 5.0` + `FX_SYMBOLS` allowlist. GBPUSD now gets $158K cap (was $19K). Bumped `TOTAL_NOTIONAL_CAP_X` 5.0→8.0 for headroom.

### Fix 5 — Integer FX quantity (just shipped, committing now)
After cluster cap fix, argus tried submitting GBPUSD orders but they were CANCELLED with `IBKR Error 10318: doesn't support fractional quantity trading`. The sizing layer produced `totalQuantity=23300.509...`. IdealPro requires integer FX quantities. Fix: `int(abs(size))` cast in `argus_flow/runner_unified.py` at all 4 `MarketOrder()` call sites (entry + 3 exit paths).

**Lesson:** these three argus blockers happened in sequence. Each fix uncovered the next. Production validation requires actual trade-attempts to surface gate-stack issues.

### Today's strategy ledger (5/1)

| Strategy | Trades | PnL | Note |
|---|---:|---:|---|
| vix_intraday (17 UTC blackout) | 4 wins / 4 attempts | +$120.93 | Blackout fix is performing |
| multi_orb v3 (window=32) | 0 wins / 3 stops | -$24.50 | Wider window let entries fire but entries still bad (entry-quality finding from 4/30 confirmed) |
| Net realized | 7 trades | **+$96.43** | |

Cuebanks: 0 signals through the full Friday session despite sd_zones bug fix. **Either the fix is incomplete OR market didn't offer S/D supply zone setups today.** Investigate further on 5/7 if still 0 by Tuesday.

Argus pairs: 0 trades closed (all blocked by 3-layer gate stack until afternoon fixes). First valid attempts post-fix were 14:55 + 15:05 UTC (cancelled by fractional-qty bug). Real first paper trades will land Sunday open onwards.

### Files modified 5/1 (all committed + pushed)

| Commit | File | Fix |
|---|---|---|
| 74667da | forge/tori/runner.py | Added heartbeat in run_live() |
| 74667da | ops/watchdog.ps1 | Futures=N/A when no futures configs |
| 7daa29c | helio/cluster_exposure.py | FX cap separation |
| (pending) | argus_flow/runner_unified.py | int() cast on FX MarketOrder qty |

### Operator items completed 5/1
- USB drive plugged in (F:) — backup validated at F:\Argus_Backup
- Drill harness ran (Plan, Halt, CircuitBreaker, Flatten, Kill) — closes readiness items 11/12/14
- backup_to_usb.ps1 keyFiles updated to current architecture (commit 3d625a8)
- drill_harness.ps1 minor display fixes (commit ad297f7)

## Run these first when the user returns

```bash
# Fleet health
curl -s http://localhost:8080/api/fleet_health | python -c "import json,sys; d=json.load(sys.stdin); s=d.get('systems') or []; ok=sum(1 for r in s if isinstance(r,dict) and r.get('status')=='OK'); down=[r.get('name') for r in s if isinstance(r,dict) and r.get('status')=='DOWN']; print(f'OK={ok}/{len(s)} DOWN={down}')"

# Refresh analytics (these run hourly via managed_truth_loop but force-fresh)
cd c:/Argus/repo
python -m ops.block_outcome_v2
python -m ops.sample_size_projection
python -m ops.stop_loss_effectiveness

# 5/7 morning audit (~7 trading days since the 4/30 restart)
python -m ops.readiness_eval
```

## What should have accumulated by 5/7

**~5-7 trading days of post-restart data** (5/1 Fri, 5/2 Mon... wait, 5/3 Sat. So: 5/1 Fri, 5/4 Mon, 5/5 Tue, 5/6 Wed = 4 trading days. Adjust expectations accordingly.)

| Strategy | Signal expected? | What to check |
|---|---|---|
| **forge_cuebanks** | YES, multiple/day during NY session | Did sd_zones bug fix actually unblock signals? `forge/logs/cuebanks/signals.csv` should have rows. If still 0, deeper bug. |
| **forge_multi_orb v3** | YES, daily ORB cycles | Did window 24→32 produce different exit patterns? Compare 5 days of v3 trades vs prior 5 days of v1 |
| **forge_vix_intraday + 17 blackout** | YES, ~2/week | Are stops still firing in first bar? Did 17 UTC blackout reduce overall stop rate? |
| **argus cadjpy** | YES, ~1/day | Stage paper unblocked + RECOVERY_REQUIRED cleared. Did paper trades execute? |
| **argus gbpusd** | YES, MORE than before (hour_filter disabled) | Per config replay: 18.9 sig/day expected. Did the unblocked window produce real entries? |
| **forge_tori --live** | MAYBE 1-2 setups | 4H trendline strategy is sparse. If 0, that's expected. |
| **forge_mamba --live** | Probably 0 entries (confluence≥5 strict) | Signal-rich, entry-poor by design. Verify `--live` mode is reaching submit_signal when conditions met. |
| **forge_nq_overnight** | YES, 4-5/week | Add ~3-5 more trades. If PF still ≥2.0 at n≥7, primary RM candidate. |
| **forge_gld_pm_long** | YES, 2-3/week | Add 1-2 trades. n≈6-7 by 5/7. |
| **forge_jpy_pm_short** | YES, 2-3/week | Add 1-2 trades. n≈6-7. |
| **forge_tom_international** | T+3 exits from 4/29 = closed trades 5/2-5/5 | Will have first n=6 closed-trade dataset. Backtest PF was 1.31 over 1249 trades. |
| **forge_gdx_gld signal-only** | YES, daily eval | Will have 4-5 signal evaluations. Re-attempt --live mode after live-mode silent-death is diagnosed. |
| **vix_revert, wick_gbpusd, fomc_drift, rebalance** | NO (environmentally silent or no event) | Don't expect signals. Confirm correctly silent. |

## Decision criteria for 5/7 cull/improve

**Auto-KILL candidates if 5/7 data shows:**
- Any strategy at n≥30 with PF<0.95 (likely KILL outright)
- Any strategy still at 0 signals after 5 trading days post-fix (cuebanks, tori, mamba, argus pairs) → deeper diagnosis OR KILL
- Any strategy with WR<35% AND PF<1.0 at n≥30

**Auto-IMPROVE candidates if 5/7 data shows:**
- multi_orb v3: live PF >1.05 at n≥150 → consider lifting factor 0.5x → 0.7x
- vix_intraday with blackout: live PF >1.10 at n≥50 → consider keep-paper at 1.0x permanent
- nq_overnight: PF holds ≥2.0 at n≥10 → consider real-money 0.5x tier (with cluster-cap check vs vix_intraday)

**Schema fix candidates** if signal volume holds:
- mamba/argus pairs/gdx_gld signals.csv schemas don't have `action` column. V2 adapters cover the schema gap. If 5/7 shows real entries, decide whether to add the column natively or keep adapters.

## Operator items between now and 5/7

These were left for the user (not code-able):
1. **Plug in USB drive** — clears ArgusUSBBackup last_result=1 (item 9 readiness)
2. **Review + run scheduled task cleanup** — `python -m ops.scheduled_task_cleanup` lists 8 deprecated tasks safe to delete (cosmetic)
3. **Optional**: run `ops/drill_harness.ps1 -Drill All` to test emergency tooling (closes readiness items 11/12/14)

## What changed in tonight's session (4/30 evening) that the user wants validated

### Code/config changes that need observation:
- forge/cuebanks/runner.py: sd_zones param fix
- forge/multi_orb/runner.py: window 24→32
- forge/vix_intraday/runner.py: 17 UTC blackout
- argus_flow/runner_unified.py: orphan-detection respects forge positions
- argus_flow/configs/cadjpy_mtf_paper_v1.json: stage watcher→paper
- argus_flow/configs/gbpusd_range_paper_v1.json: hour_filter.enabled=false
- ops/start_all_runners.ps1: tori/mamba `--live`; apollo/hermes/titan/rebalance/spy_mean_rev removed
- argus_flow/configs/hashes.json: registry updated for cadjpy + gbpusd

### Observable signals success/failure of each fix:
| Fix | Success looks like | Failure looks like |
|---|---|---|
| Cuebanks sd_zones | signals.csv has rows during NY session | still 0 signals |
| Multi_orb v3 window | trades.csv has trades with config_hash matching v3 | trades use old config, OR no improvement |
| Vix 17 UTC blackout | no entries in 17:00-18:00 UTC, lower stop rate | entries still happen at 17 UTC |
| Argus orphan fix | runner_unified.log: "NON-ARGUS POSITION" (INFO not WARN), no RECOVERY_REQUIRED | orphans still flagged UNRESOLVED |
| Argus cadjpy/gbpusd configs | cadjpy_paper trades.csv has entries; gbpusd has more entries | configs not picked up (hash mismatch?) |
| Tori/Mamba --live | trades.csv has entries when conditions fire | --live not actually launching, only --loop |

## Reference paths
- Master game plan: `project_master_game_plan_20260501.md`
- Session summary: `project_2026_04_30_session_summary.md`
- All commits pushed to `Ksmith2322/Argus` branch `phase6-hardening`
- Tags: `v2026-04-30-master-game-plan`, `v2026-04-30-every-strategy-trading`

## Known still-open items (deferred to 5/7+)

1. **gdx_gld live-mode silent-death** — not diagnosed. Currently in --signal-only --loop. To investigate: launch --live with explicit stderr redirect, wait for it to die, capture exit cause.
2. **Multi-strategy entry-quality fix** — stop-loss analysis identified "bad entries" as the structural problem for multi_orb + spy_mean_rev. We addressed via window/blackout proxies but no actual entry-timing fix yet. Weekend research direction.
3. **2026-data backtest re-runs** — only ran 60d (yfinance limit on 5m data). Full 90d+ backtests for vix_intraday/multi_orb need IBKR-sourced data, not yfinance.
4. **Operator drills 11/12/14** — drill harness exists but operator hasn't run real drills yet. Items still MANUAL on readiness gate.
