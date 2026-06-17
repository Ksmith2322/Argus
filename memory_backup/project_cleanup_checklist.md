---
name: Code Cleanup Checklist
description: Dead code removal, config archival, dashboard simplification — ready for next session
type: project
---

## Quick Wins (30 min)
- [ ] Archive 49 killed configs to `archive/configs_killed/`
- [ ] Delete `.tmp_*` test directories (6 dirs, 965 KB)
- [ ] Delete signal backup files (`signals_v*_backup.csv` across all log dirs)
- [ ] Delete root dead code: `confluence.py`, `_audit_backtest.py`, `_exit_analysis.py`, `_strategy_test.py`

## Medium (1 hr)
- [ ] Delete or compress `archive/legacy_data/` (144 MB)
- [ ] Fix `coin_rotation` phantom import in `ops/dashboard.py` (lines 375, 1770)
- [ ] Clean `run_manifest.py` orphaned references (engine.py, adaptive_confluence.py, structure.py, regime.py, strategy_phase2.py)
- [ ] Delete empty log files (dashboard.log, futures_group_stdout.log, RESET_DRAWDOWN)

## Dashboard Simplification (1-2 hr)
- [ ] Add system tabs: Argus | Titan | Ares | Hermes (one tab per system)
- [ ] Remove all killed-system cards and references
- [ ] Per-system summary card: status light, trades today, PnL, uptime
- [ ] Fleet overview at top: total open positions, total PnL, all systems health
- [ ] Titan scanner results panel (today's signals + historical accuracy)

## Estimated Total: ~3-4 hours across 1-2 sessions

**Why:** 250 MB dead weight, phantom imports risk runtime errors, dashboard is cluttered with killed systems. Clean before adding Ares/Hermes to prevent confusion.

**How to apply:** Do cleanup BEFORE building Ares/Hermes. Clean house, then add new rooms.
