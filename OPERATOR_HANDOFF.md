# Operator Handoff — 2026-05-23 evening → 2026-05-26 market open

Tuesday 5/26 is the next trading session (Monday 5/25 is Memorial Day, US
equity markets closed). This document lists every action that requires
operator (your) hands because they involve credentials, scheduled tasks,
process kills, or shared-state mutations.

Everything else from tonight's audit is already in the codebase. The fleet
is wired to behave correctly once you do these. Items are roughly ordered
by criticality.

---

## 0. New candidate: forge_tom_spy (2026-05-24 — needs your call)

The new-strategy hunt produced its first candidate: classical turn-of-month
(TOM) effect on SPY. **Live runner is now built**; operator needs to
decide whether to (1) add to ACTIVE_ROSTER and (2) flip allocation_factor
above 0.0.

### Evidence stack

**20-year disciplined gate** (n=240):
- PF 1.90, IID CI lower 1.215 at 10bps slippage — passes 1.20 floor
- Block bootstrap (b=3,5,8): all pass
- Period-stability sub-samples fail individually (n=120 each) but point
  PFs are 1.717 / 1.721 — effect is consistent, just sample-limited
- **PARTIAL_PASS (6/9 layers)**

**Cross-decade OOS**:
- pre-2006: PF 1.59 / 2006-2016: PF 1.68 / 2016-2026: **PF 1.76**
- The "TOM was arbitraged out post-2000" literature claim is contradicted
  by SPY data

**Cohort risk audit** (`ops/reports/system_audit/cohort_risk_audit.md`):
- tom_spy ↔ xs_momentum correlation: **0.03** (essentially uncorrelated
  despite both trading SPY direction — different time-of-month exposure)
- Adding tom_spy at 0.3× to current portfolio: max DD 17.11% → **14.08%**
  (−18%), vol 10.78% → 9.11% (−15%), Sharpe 0.54 → 0.58
- Cost: 0.45%/yr CAGR reduction

### How to activate

1. Edit `argus_flow/tests/test_sunset_roster.py`: add `"forge_tom_spy"`
   to `ACTIVE_ROSTER`. The test_active_roster_is_exactly_xs_momentum_and_gld_pm_long
   pin will fail until you do this — by design, so re-activation requires
   a deliberate test edit.
2. Edit `argus_flow/configs/allocation_factors.json`: add
   `"forge_tom_spy": 0.3` to `factors` and a `_kill_log` line noting
   the recalibration source.
3. Run `python -m forge.tom_spy.runner --check` to confirm today's
   action (should print `is_tom_entry_day` and `is_tom_exit_day` flags).
4. Launch the daemon:
   ```powershell
   $env:IBKR_PORT = '7497'
   Start-Process -FilePath 'C:\Argus\.venv\Scripts\python.exe' `
     -ArgumentList '-m','forge.tom_spy.runner','--loop' `
     -WorkingDirectory 'C:\Argus\repo' -WindowStyle Hidden
   ```
5. The runner is calendar-aware; first action will be the next entry
   day (likely the 4th-to-last weekday of either May or June 2026 —
   `--check` will tell you).

### Russell reconstitution candidate — NULL RESULT

Also tested IWM around the annual late-June Russell rebalance Friday.
Across 16 years (2010-2025), no window reaches conventional significance:
- pre5_to_friday: mean +1.03%, t=+1.45 (best targeted window, ns)
- full_20day: mean +1.82%, t=+1.88 (most cumulative)

The Russell recon edge requires targeted add/delete trades (specific
predicted stocks), not a broad IWM bet. That's a much bigger data
project. Report at `ops/reports/system_audit/russell_recon_research.md`.

---

## 1. Rotate the Discord webhook (security — do this BEFORE pushing)

The webhook URL committed in `.env` line 96 (DISCORD_WEBHOOK_URL)
is public to anyone who has ever cloned the repo. It's still in git
history. The fix is rotation, not relocation.

Steps:

1. In Discord: Server Settings → Integrations → Webhooks → find the
   one pointing at the channel Argus uses → click the existing webhook →
   click **Reset URL**. Copy the new URL.
2. Create `secrets/discord.env` (`secrets/` is already in `.gitignore`)
   with the new URL:
   ```
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/<NEW-ID>/<NEW-TOKEN>
   ```
3. Remove (or comment out) line 96 of `.env`. The notify.py loader I
   shipped tonight already reads `secrets/discord.env` before falling
   back to `.env`, so nothing else needs changing.
4. (Optional) Run `python -m ops.notify --test "rotated webhook"` to
   confirm the new URL works.

If you skip rotation, the OLD URL stays live in git history forever and
anyone with read access can post to your Discord.

---

## 2. Start TWS / verify IBKR session is up

`nq_overnight` was stuck overnight in stale signal-only state because
TWS had been unreachable during the 5/22 reset window. I shipped a
self-heal that clears that stale state on the next wake **provided TWS
is up**, but if TWS itself is down on Tuesday morning, no strategy
trades.

Operator items:

- Confirm TWS is running, paper account DUP472829, port **7497**.
- IBC was installed 5/22 but the credentials in `C:\IBC\config.ini` are
  still PLACEHOLDER (`IbLoginId=PLACEHOLDER`,
  `IbPassword=PLACEHOLDER`). Either:
  - **Manual launch path**: log in to TWS yourself before 14:30 UTC
    Tuesday and leave it running. Acceptable until you fill IBC creds.
  - **Auto-launch path**: edit `C:\IBC\config.ini`, fill in the real
    paper credentials, then run `ops/start_tws_via_ibc.ps1` to verify
    IBC can log in unattended. Optionally register the
    `TWSAutoStart` scheduled task per `ops/IBC_SETUP.md`.

---

## 2.5. Register the daily auto-pause cron (new — 2026-05-24)

The auto-pause loop alerts when a strategy's live PF crosses the FAIL
band for K consecutive days. Without a daily cron, it never gets
the chance to detect anything.

From an **Admin** PowerShell:
```powershell
$action = New-ScheduledTaskAction `
    -Execute "C:\Argus\.venv\Scripts\python.exe" `
    -Argument "-m ops.auto_pause" `
    -WorkingDirectory "C:\Argus\repo"
$trigger = New-ScheduledTaskTrigger -Daily -At "10:00 PM"
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "ArgusAutoPause" `
    -Action $action -Trigger $trigger -Settings $settings -Force
```

This runs nightly at 22:00 local time (after US market close), snapshots
the cohort gate, appends to `argus_flow/logs/cohort_gate_history.jsonl`,
and writes `argus_flow/logs/auto_pause_recommendations.json`. If any
strategy crosses the 5-consecutive-FAIL-days threshold for the first
time today, it posts to Discord. Discord requires the webhook (item #1
above) to be working.

**Operator workflow when a PAUSE_RECOMMENDED alert fires**:

1. Read `argus_flow/logs/auto_pause_recommendations.json` to confirm
   the recommendation (verify it's not noise from a brief drawdown).
2. Dry-run the apply to see exactly what would change:
   ```
   python -m ops.auto_pause --apply
   ```
3. If you agree, apply for real:
   ```
   python -m ops.auto_pause --apply --confirm
   ```
4. This flips `allocation_factor=0.0` for every PAUSE_RECOMMENDED
   strategy, appends a line to `_kill_log` in allocation_factors.json,
   writes an event to `auto_pause_apply_events.jsonl`, and posts
   Discord confirmation.

The recommendation→apply split is intentional: live-PF-degradation
auto-pause has been wrong before (the 2026-05-23 silent-archive-fallback
bug made live_gate_monitor lie for 12 days). The human-in-the-loop
defends against systemic bugs in the signal we'd otherwise act on.

---

## 3. Register / verify scheduled tasks

After tonight's code changes, the dashboard + auto-watchdog should be
healthy, but two scheduled tasks need attention:

- `ArgusCohortReport`: was stuck in a stale "running" state during the
  audit. From an **Admin** PowerShell:
  ```
  Stop-ScheduledTask -TaskName "ArgusCohortReport" -ErrorAction SilentlyContinue
  Get-ScheduledTask -TaskName "ArgusCohortReport" | Select-Object TaskName, State
  ```
  Confirm State=Ready. If it's still in a wedged state, disable then
  re-enable.
- Dead `argus_flow.runner_unified` process: the FX runner was put down
  permanently 5/20 (3 argus FX pairs are slated for archive). If
  Get-Process shows it running, kill it — it's leftover from the old
  run. Use:
  ```
  Get-Process python | Where-Object { $_.CommandLine -match "runner_unified" } |
      Stop-Process
  ```
  Only do this if you confirm the CommandLine matches; do NOT kill
  every python process.

Optionally re-register the full task set via
`ops/register_tasks.ps1` (must be **Admin** PowerShell).

---

## 4. Verify nq_overnight stale state self-heals on next wake

On the first wake when TWS is reachable, the self-heal block I added
will clear the phantom `open_trade` from 5/22 16:00 UTC.

What you should see in `forge/logs/nq_overnight/runner.log`:
```
... [WARNING] nq_overnight: STALE_SIGNAL_ONLY: clearing phantom open_trade
  (entry_ts=2026-05-22 20:00:00+00:00, signal-only entry never had real
  broker position) so live signals can resume
```
After this line, `state.json` `open_trade` becomes `null` and the next
20:00 UTC signal hour will evaluate cleanly. If you don't see the
self-heal line, TWS connection failed and the runner stayed in fallback;
fix TWS first.

The same self-heal pattern was added defensively to gld_pm_long. It
doesn't have stale state today, but the next time IBKR drops mid-signal
it won't strand a phantom trade.

---

## 5. Verify xs_momentum will fire on June 1

I shipped `--loop` and `--evaluate` modes for `forge.xs_momentum`. The
rebalance gate fires only on the **first weekday wake of a new calendar
month** (with state['last_rebalance_month'] != current month). Manual
test you can run right now to confirm wiring:

```
python -m forge.xs_momentum.runner --evaluate
# Expected: {"action": "noop", "reason": "not_due (last_rebalance_month=)", ...}

python -m forge.xs_momentum.runner --check
# Expected: JSON with current top-2 picks from the broad-8 universe
```

To launch the daemon for production:
```
$env:IBKR_PORT = '7497'
Start-Process -FilePath 'C:\Argus\.venv\Scripts\python.exe' `
  -ArgumentList '-m','forge.xs_momentum.runner','--loop' `
  -WorkingDirectory 'C:\Argus\repo' -WindowStyle Hidden
```
Heartbeat at `forge/logs/xs_momentum/heartbeat.json` updates daily; the
real rebalance fires June 1 at 14:30 UTC (10:30 AM ET — first 60min of
the regular session, when the broad-8 ETFs have settled their opening
auctions).

If you want to force-rebalance immediately (e.g. to seed live evidence):
```
python -m forge.xs_momentum.runner --evaluate --force
```
This bypasses the month-boundary gate but still respects
allocation_factor (currently 1.0× for xs_momentum) and all real-money
boundary checks.

---

## 6. Verify allocation gate caught spy_trend_follower

I shipped `forge_spy_trend_follower` to read `allocation_factor` and
refuse to trade when it's 0.0 (the current setting). Sanity check:

```
python -m forge.spy_trend_follower.runner --evaluate --mode paper
# Expected log line:
# [ALLOC-GATE] action=... blocked: allocation_factor=0.0 (strategy deallocated)
```

If you want spy_trend_follower active later, edit
`argus_flow/configs/allocation_factors.json` and set its factor > 0.
The gate is read fresh on every cycle — no restart needed.

---

## 7. live_gate_monitor now tells the truth

Pre-fix: `live_gate_monitor` silently fell back to pre-5/22 archive
data when no live trades.csv existed, so `forge_gld_pm_long` appeared
to have n=16 live trades with PF 1.88. Those were April 2026 trades
from before the reset.

Post-fix: default `fallback_archive=False`, all 5 strategies correctly
report `INSUFFICIENT_N` with n=0 until real fills accumulate.

```
python -m helio.live_gate_monitor
# Expected: all 5 strategies show n=0, live_pf=n/a until trades fire
```

There is a regression test pinning this behavior:
`argus_flow/tests/test_live_gate_monitor.py::test_load_trades_silent_archive_fallback_disabled_by_default`

---

## 8. Promotion gate baseline updated with per-strategy slippage

`argus_flow/configs/promotion_gate_baseline.json` is now v2026-05-23.v2.
Every strategy now records:
- `slippage_bps_used`: what bps figure produced the published CI bounds
- `_slippage_bps_realistic`: Agent 2 5/23 recalibration

Strategies whose `_slippage_bps_realistic > slippage_bps_used` (notably
`forge_pead` at 10→40 bps and `forge_nq_overnight` at 3→7 bps) need
the gate re-run before any real-money consideration. Both are already
verdict=FAIL so this is informational, but it changes how aggressively
you should monitor for unexpected live PF — the bar is HIGHER than
the committed CI suggests for those two.

---

## What I shipped tonight (code, all already in the working tree)

| File | Change |
|---|---|
| [helio/live_gate_monitor.py](helio/live_gate_monitor.py) | silent archive fallback disabled by default; added `trade_source` field |
| [argus_flow/tests/test_live_gate_monitor.py](argus_flow/tests/test_live_gate_monitor.py) | 2 new regression tests (20 total, all green) |
| [forge/spy_trend_follower/runner.py](forge/spy_trend_follower/runner.py) | reads allocation_factor; fail-closed; refuses trade at 0.0 |
| [forge/xs_momentum/runner.py](forge/xs_momentum/runner.py) | `--evaluate` + `--loop` + `--force`; allocation gate; real-money boundary; canonical_fills writes; comment on auto_adjust=False rationale |
| [argus_flow/tests/test_xs_momentum_runner.py](argus_flow/tests/test_xs_momentum_runner.py) | 9 new control-flow tests for the rebalance gate + allocation fail-closed |
| [forge/nq_overnight/runner.py](forge/nq_overnight/runner.py) | self-heal: clears stale signal-only `open_trade` when broker confirms flat |
| [forge/gld_pm_long/runner.py](forge/gld_pm_long/runner.py) | same self-heal pattern (defensive — no stale state today) |
| [argus_flow/configs/promotion_gate_baseline.json](argus_flow/configs/promotion_gate_baseline.json) | v2 with per-strategy slippage_used + realistic; PEAD/NQ flagged for re-run |
| [helio/yfinance_data.py](helio/yfinance_data.py) | added repo-wide auto_adjust convention docblock |
| [ops/notify.py](ops/notify.py) | webhook loader now reads `secrets/discord.env` before falling back to `.env` |
| [ops/dashboard.py](ops/dashboard.py:486), [ops/dispatch_*.ps1](ops/), [ops/pull_pc2_results.ps1](ops/pull_pc2_results.ps1), [ops/check_pc2.ps1](ops/check_pc2.ps1), [ops/refresh_candles.ps1](ops/refresh_candles.ps1), [ops/status_all.ps1](ops/status_all.ps1), [ops/DISASTER_RECOVERY.md](ops/DISASTER_RECOVERY.md), [roadmap.txt](roadmap.txt) | PC2 IP 192.168.1.98 → 192.168.1.101 (9 live files; memory_backup snapshot intentionally left as-is) |

42 tests passing (xs_momentum runner: 9, xs_momentum pure logic: 13,
live_gate_monitor: 20).
