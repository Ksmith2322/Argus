# Tuesday 2026-05-26 — market open checklist

Markets reopen after Memorial Day. Use this as your run-of-show.

## 30 min before US open (~9:00 AM ET / 13:00 UTC)

1. **Verify TWS is up** and logged into the paper account (DUP472829, port 7497).
2. **Run pre-market check**:
   ```powershell
   C:\Argus\.venv\Scripts\python.exe -m ops.audit.run_pre_market_check
   ```
   Expect verdict `REVIEW` (not READY, not NOT_READY).
   - `runners on old SHA: forge_xs_momentum + forge_gld_pm_long` — see step 3
   - `stale or missing heartbeats: 3 variants` — see step 4
3. **Restart the two live runners** to pick up the latest code
   (commits 8c7ff5c and later added heartbeat git_sha for restart-verify;
   the running processes predate it):
   ```powershell
   # Stop the existing ones (watchdog will NOT restart them since fleet_monitor systems
   # don't auto-restart unless explicitly listed; check after stop)
   Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "forge.gld_pm_long" } | Stop-Process -Force
   Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "forge.xs_momentum.runner" -and $_.CommandLine -notmatch "--variant" } | Stop-Process -Force
   # Launch fresh:
   $env:IBKR_PORT = '7497'
   Start-Process -FilePath 'C:\Argus\.venv\Scripts\python.exe' -ArgumentList '-m','forge.gld_pm_long.runner','--loop' -WorkingDirectory 'C:\Argus\repo' -WindowStyle Hidden
   Start-Process -FilePath 'C:\Argus\.venv\Scripts\python.exe' -ArgumentList '-m','forge.xs_momentum.runner','--loop' -WorkingDirectory 'C:\Argus\repo' -WindowStyle Hidden
   ```
4. **Launch the 3 new variants** (paper-trading, allocation 0.5× each):
   ```powershell
   .\ops\launch_xs_momentum_variants.ps1
   ```
   Wait ~60 seconds, then check heartbeats:
   ```powershell
   Get-ChildItem forge\logs\xs_momentum_*\heartbeat.json | Select-Object FullName, LastWriteTime
   ```
   All 3 should have heartbeats <2 min old.
5. **Re-run pre-market check** — should now report `READY` (no more reasons).

## At market open (9:30 AM ET / 13:30 UTC)

- The 4 xs_momentum strategies will NOT fire today (May 26 is day 26 of month; rebalance window is day 1-7 of next month, so first action ~June 1).
- `gld_pm_long` fires its PM evaluation around 21:05 UTC / 17:05 ET / 5:05 PM ET.
- `tom_spy` is PENDING_OPT_IN — only fires if you flipped its `allocation_factor` >0 in `allocation_factors.json` (currently 0.0; opt-in deadline is TODAY).

## Decision required by 1 PM ET — `forge_tom_spy` opt-in

The TOM strategy's first trading window is May 26 through ~June 2.
If you want to test it this cycle:

1. Edit `argus_flow/configs/allocation_factors.json`, set
   `"forge_tom_spy": 0.3` (the recommended sizing).
2. Add a one-line entry to `_kill_log` documenting the decision.
3. Kill the existing `tom_spy` runner if any, launch fresh:
   ```powershell
   $env:IBKR_PORT = '7497'
   Start-Process -FilePath 'C:\Argus\.venv\Scripts\python.exe' -ArgumentList '-m','forge.tom_spy.runner','--loop' -WorkingDirectory 'C:\Argus\repo' -WindowStyle Hidden
   ```

If you'd rather wait another cycle, leave at 0.0 — `nov_spy` is single-strategy
November-only and won't fire either way until Nov 2.

## What to watch during the day

| Where | What |
|---|---|
| `http://localhost:8080/` (dashboard) | the new audit panels at the top — pre-market readiness, audit health, data feed contracts, strategy roles, alpha readiness |
| `argus_flow/logs/canonical_fills.jsonl` | first post-reset fill lands when `gld_pm_long` fires ~21:05 UTC |
| Discord | `daily_health_check` runs nightly 23:00 UTC, posts if anything not all-GREEN |

## What to NOT touch

- **Don't change `allocation_factor` for `xs_momentum` or the 3 variants mid-week** — the disciplined gate was scored at the current size; changing it invalidates the live evidence.
- **Don't raise `PER_STRATEGY_NOTIONAL_CAP_X`** — earn the higher cap through 30 days of clean live evidence first. See `ops/CAPACITY_HEADROOM_NOTES.md`.
- **Don't kill rogue processes blindly.** A second `xs_momentum` runner will refuse to start (PID lock); but if you see weirdness, check `forge/logs/xs_momentum_<variant>/runner.lock` to confirm only one is alive per variant.

## Reference docs (refreshed this session)

- `docs/PERFORMANCE_EXPECTATIONS_20260524.md` — per-strategy + fleet
  P&L bands, red-flag signals, decision triggers
- `docs/GAME_PLAN_20260526_TO_20260630.md` — week-by-week milestones,
  5-agent rotation plan, 6-hard-signals bar for 7/1 go/no-go
- `docs/UNIVERSE_SWEEP_FINDINGS_20260524.md` — full sweep results
  (3 variants survived 20y disciplined gate)
- `ops/DATA_FEED_RUNBOOK.md` — yfinance cache refresh + Task Scheduler
  setup
- `ops/CAPACITY_HEADROOM_NOTES.md` — when to raise the cluster caps
- `OPERATOR_HANDOFF.md` — full operator action queue (TWS, scheduled
  tasks, opt-in decisions)

## Status snapshot at session end

- Test suite: **2220 passing** (started 1970)
- ACTIVE strategies: **5** (started 2)
- Codex gaps: **10 closed** (started 0)
- Hardening shipped: PID-lock guard, restart-verify, evidence-quality
  gate, preflight-to-action mapping, role-aware disciplined gate,
  data-feed contracts, freshness runbook, order-lifecycle audit (X7),
  pre-market readiness check

You're cleared for tomorrow's open. The bot is in the best shape it's
been all session.
