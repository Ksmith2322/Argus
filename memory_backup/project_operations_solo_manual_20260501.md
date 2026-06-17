---
name: Solo operations manual — 30-day post-go-live runbook
description: Operating procedures for running the bot solo after 5/31 freeze + real-money go-live. Daily/weekly cadence, alert escalation, when NOT to touch, common-failure-fix recipes ranked by frequency.
type: reference
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
## Design constraint — 30 minutes/day

This manual is designed for **30 min/day** of attention, not 2 hours. Locked from user feedback 4/26: "design for 30 min/day, not aspirational." If a procedure here takes more than 30 min routinely, the procedure is wrong — flag it for simplification.

The corollary: don't add manual checks unless they're load-bearing. Each new check eats minutes.

## Daily cadence (60 seconds)

Every market day, ~5 min before NY open:

1. Open dashboard at http://localhost:8080
2. Top banner GREEN? (broker connected, equity reasonable, no PAUSE_ENTRIES)
3. Skim Fleet Health — 22 systems all OK? (allow `forge_tori` to show STALE up to 4hr — that's its cycle)
4. Skim Recent Trades — anything surprising overnight? (real-money positions Mon morning especially)
5. Done. Don't tinker.

**Don't open the dashboard between cadence windows** unless an alert fires. Watching it minute-by-minute creates intervention temptation.

## Weekly cadence (10 min, Sunday evening)

Per `reference_weekly_audit_routine.md`:

1. `cd c:/Argus/repo && C:/Argus/.venv/Scripts/python.exe -m ops.full_audit --diff`
2. Scan the 6 manual checks (cohort canary, DOWN runners, REAL_ENTRY failures, scheduled-task last_result, gdx_gld disconnects, drawdown_pause)
3. Update memory if anything surprising — append to `reference_failure_modes.md`
4. Commit changes if any fixes were applied

## Alert escalation matrix

When Discord fires, classify and act:

| Discord alert | Severity | Action | Time-to-fix |
|---|---|---|---|
| Broker disconnected > 5 min | CRITICAL | Restart TWS, then `python -m argus_flow.ops.risk_oversight` | NOW |
| Drawdown pause activated | HIGH | DON'T override. Read why. Wait until next session. | 24h+ |
| Cohort report WITH FAILURES | HIGH | Check `cohort_report.log`, identify which step. Most often: managed_truth lock contention (now non-fatal per 4/25 fix). | Same day |
| Strategy DOWN | MEDIUM | Check log, restart if confused. If recurrent (3+ in week), root-cause. | Same day |
| Fleet equity drift > 1% | MEDIUM | Run reconciliation. Verify orphan positions. | Same day |
| Single REAL_ENTRY FAILED | LOW | Note in journal. If pattern emerges (5+ in day), investigate. | Within week |
| New stale-data warning | LOW | Check artifact. Often self-resolves on next cycle. | Watch for now |

## When NOT to touch

These look like bugs but are intentional:

- **Argus FRIDAY_CLOSE block at end of week** — auto-clears Sunday open. Don't override.
- **forge_tori shows STALE 4hr** — its cycle is 4hr. Per failure mode #5, threshold is misleading.
- **apollo actionable=0 with N>50 candidates** — pre-earnings setups don't fire by design. Only post-ER drift trades.
- **vix_revert silent** — needs VIX > 25. Markets calm = no trades. Working as designed.
- **fomc_drift silent except FOMC days** — by definition.

## Most common failures (ranked by frequency)

### #1 — TWS socket disconnect (multiple/week)
Symptom: a runner shows DOWN, log has `ConnectionError: Socket disconnect`.
Fix: restart that runner. If multiple at once → TWS itself glitched, restart TWS first then runners.
Don't over-engineer this. It's a known IBKR behavior.

### #2 — Cohort report lock contention (now non-fatal post-4/25 fix)
Symptom: `refresh_managed_truth ERROR: exit code 2` in cohort_report.log.
Per 4/25 fix, this is now logged as WARNING and the chain continues. Action: nothing.
If it ever escalates back to FAILURES: check for duplicate managed_truth daemons (failure mode #1).

### #3 — gdx_gld dies after 1-24 hours (recurring)
Symptom: gdx_gld DOWN in fleet_status, log shows Socket disconnect.
Fix: restart with `python -m forge.gdx_gld_runner --live` (`$env:IBKR_PORT='7497'`).
Long-term fix: add resilience to runner (deferred to post-real-money).

### #4 — RECON_DRIFT on a pair (rare but real)
Symptom: argus blocks entries on a pair, log says `local=FLAT broker=LONG qty=N`.
Fix: run `python -m ops.close_orphan_usdjpy` (template — adjust SYMBOL/QTY in script).
Wait 5 min for next reconciliation cycle to confirm clear.

### #5 — Strategy gate too tight (silent runner)
Symptom: a strategy fires 0 trades in 30+ days despite being designed for 1+/day.
Fix: refer to `project_silent_15_actions_20260426.md` for the gate-loosening pattern. ONE change at a time, observe 1-2 weeks.

### #6 — Hash mismatch on argus startup
Symptom: `FATAL: config '<name>.json' hash mismatch` in runner_unified.log.
Cause: someone edited a config without updating `argus_flow/configs/hashes.json`.
Fix: copy the actual hash from the error message into hashes.json.

### #7 — Stale lock files showing in audit
Symptom: full_audit code.json shows stale_locks > 0.
Per 4/25 fix, audit now checks PID liveness — if a lock is flagged, the owner PID is dead.
Fix: `rm` the .lock file and the .json companion.

## Network glitches

If WiFi/internet drops:
- TWS will disconnect; runners will log Socket disconnect
- Once TWS reconnects (auto-retry), restart runners that died
- broker_truth may be stale — run `python -m argus_flow.ops.risk_oversight` once to refresh

If pagers /Discord webhooks fail:
- Argus will continue trading (alerts are observability, not control plane)
- Check `argus_flow/logs/discord_failures.jsonl` for what was missed
- Restart Discord bot/webhook on your end

## Reboot recovery

Per `reference_reboot_recovery.md`:
1. Open IBKR Gateway / TWS, log in, accept disclaimer
2. Verify port 7497 listening: `Test-NetConnection -ComputerName 127.0.0.1 -Port 7497`
3. Start argus runner_unified
4. Once argus has reconciled (~2 min), all forge runners can start
5. Start dashboard
6. Start fleet_monitor

If a single runner crashes mid-day: just restart that runner. Don't restart fleet.

## PC1 hardware failure during market hours (real-money critical)

If PC1 hardware fails (BSOD, drive failure, power loss) with open positions:

1. **Manual IBKR login** from any device — phone app, web, or PC2 if available. Verify open positions.
2. **Don't immediately panic-close.** Open positions have stops at IBKR; bracket orders are server-side. They'll execute even if argus is dead. Verify stops are present in TWS first.
3. **If stops are missing OR positions look wrong:** flatten manually at market via TWS web. Take whatever P&L lands. Better than uncontrolled.
4. **Disable scheduled tasks immediately:** `schtasks /Change /DISABLE /TN ArgusCohortReport` etc. Prevents nightly chain from running on broken state.
5. **Bring PC1 back up.** Restart from clean reboot per recovery procedure above.
6. **Reconciliation BEFORE re-enabling**: run `python -m argus_flow.ops.risk_oversight` once. Verify broker_state.json shows truth. Investigate any drift.
7. **Re-enable scheduled tasks** only after reconciliation clean for 30 min.
8. **Resume runners** in order: argus first, then forge family.

Hot standby (PC2 ready to take over) is desirable but NOT mandatory before real-money go-live. The manual runbook above IS mandatory. Test it once during Week 4 drills.

## Weekend / holiday rules

- **Forex (argus, aud_asian_breakout, jpy_pm_short, wick_gbpusd):** flat by Friday close, no entries until Sunday open. Argus enforces FRIDAY_CLOSE; verify others honor weekday checks.
- **US equities (everything else except futures):** flat over weekend. Don't trade premarket Mon unless gld_pm_long etc. has explicit pre-market support (it doesn't; entries are during regular session).
- **Holidays:** US-market-closed days (NYSE calendar) — most strategies are silent. fomc_drift skips the day if FOMC postponed. tom_international skips if month-end is holiday.

## When to page yourself (real urgency)

Page yourself (in person, not via Discord) ONLY for:
- Real-money equity dropped > 5% in a session
- Multiple runners dead simultaneously and won't restart
- Broker is showing a position you don't recognize at all (not just RECON_DRIFT, but actually orphan instrument)
- Account got flagged by IBKR for any reason

Don't page for: single REAL_ENTRY FAILED, single runner DOWN, Discord alert noise.

## When in doubt: do nothing

Most "things looking weird" resolve themselves on the next cycle. The bot has been built for autonomy. Touching it constantly creates more bugs than it fixes. The weekly audit + the alert system are sufficient for normal operations.

## How to apply this memory

**Why:** when running solo for weeks, you'll hit failures the user (or claude session) didn't predict. This is the procedural defense.

**How to apply:**
- Read this end-to-end before going to real money.
- Add new failure modes to `reference_failure_modes.md` as they're discovered, then update this manual's "common failures" list quarterly.
- Don't deviate from "when not to touch" — those are hard-learned.
