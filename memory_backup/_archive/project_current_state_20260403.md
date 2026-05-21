---
name: Current System State 2026-04-03
description: Greek family review complete, safety fixes deployed, dashboard upgraded, signal frequency fixes live
type: project
---

## System State (2026-04-03)

### Fleet Status
- 8 FX Argus runners online (all FLAT, outside London session)
- 6 futures runners online (session-based)
- Greek family: Helio (3 instruments), Hermes (1), Apollo (8) — watcher mode
- 15/60 valid Argus trades collected (4.2 days in)
- Signal frequency WATCH/KILL across most pairs (pre-fix data in window)

### Major Work This Session

**1. Signal Frequency Fix (3 blockers identified + fixed)**
- News filter: was blocking EUR/USD 4/5 weekdays (recurring schedule too aggressive) → emptied, now calendar-based only
- Open risk limit: 5% too tight for 8 instruments → raised to 10%
- Blocked hours: added [[15,19]] UTC to GBPUSD, EURJPY, GBPJPY, CADJPY configs
- London cluster limit: max 2 positions from {GBPUSD, EURJPY, GBPJPY, CADJPY}

**2. Economic Calendar System**
- `data/economic_calendar.json` — real event dates, not recurring
- `argus_flow/ops/fetch_economic_calendar.py` — auto-gen FOMC/ECB/BoE/BoJ/NFP dates
- `ops/run_calendar_update.ps1` — weekly scheduled task wrapper
- Added `ArgusCalendarUpdate` to `register_tasks.ps1` (Sunday 20:00)

**3. Config Hash Fix**
- daily_report.py used `read_bytes()` sha256, runner used `read_text().encode()` sha256 → mismatch on Windows CRLF
- Fixed daily_report to match runner's method
- 1 stale hash fixed (discovery_fx_universe.json)

**4. Greek Family Safety Fixes (Codex R4)**
- Process locks: all 3 family runners (Helio, Hermes, Apollo) now use ProcessLock
- Heartbeat schema: added `system`, `family`, `stage` fields to all heartbeats
- Stage enforcement: runners log stage and execution permission at startup
- Lock release on clean shutdown

**5. Dashboard: Greek Family Visualization**
- Neural Core renamed "Helio Neural Core"
- `/api/brain_state` now scans `helio/logs/` for family heartbeats
- New outer "GREEK FAMILY" ring in neural visualization (golden)
- Family nodes color-coded: Helio=gold, Hermes=red, Apollo=purple
- Cross-family connections (dashed lines to Argus pairs)
- Stats bar: added "Families" count
- Status line: "X Argus + Y Greek nodes | Z families"

**6. Greek Family Review Document**
- 4 Codex rounds + 3 Claude reviews + joint synthesis
- All converge: prove Argus first → build family infra → onboard Apollo → one at a time
- GREEK_FAMILY_REVIEW.md — complete engineering blueprint

### Ablation Test Results (EUR/USD)
- Baseline (range_pct_min=0.0012): 74 trades, 50% WR, PF 1.13, +55.5 pips, DD 92.85
- Ablation (range_pct_min=0): 92 trades, 55.4% WR, PF 1.11, +57.15 pips, DD 144.85
- Conclusion: range_pct is a risk filter (controls drawdown), not signal source

**7. 10x Modules Built & Wired**
- `helio/regime_router.py` — classifies TRENDING/RANGING/VOLATILE/BREAKOUT, deprioritizes wrong-regime families (watcher stage only)
- `helio/portfolio_guard.py` — cross-family position limits (max 6 total, 3/family, 4 directional bias, 2 correlated). Wired into all 3 runner entry paths.
- `helio/drift_detector.py` — monitors feature distribution drift vs baselines. GREEN/YELLOW/RED health. Baseline built from 24K+ signal rows.
- `helio/seed_heartbeats.py` — generates/updates heartbeat files for all 16 Greek family instruments
- All 3 modules integrated into dashboard: `/api/portfolio_guard`, `/api/drift_report`, brain_state includes both
- Dashboard "SYSTEM HEALTH" panel (top-right) shows portfolio guard + drift + family breakdown
- Stats bar: added "Port. Guard" (positions/max) and "Drift" (health color) indicators
- `ArgusDriftCheck` scheduled task added (daily 06:00), drift also runs in nightly cohort report
- `ops/run_drift_check.ps1` wrapper script created

**8. Dashboard Restart Required**
- Killed old dashboard process, restarted with updated code
- Also restarted runner_unified (was collateral from taskkill)
- 52 nodes total: 36 Argus + 8 Apollo + 7 Helio + 1 Hermes
- All 56 tests pass

### Key Pending
- Re-run `.\ops\register_tasks.ps1` from admin PowerShell to activate `ArgusDriftCheck` task
- Tomorrow's London session (8-14 UTC) — first with all fixes active
- Re-run divergence guard after 1 week to check signal frequency normalization
- Argus needs 45 more valid trades for promotion gate
