# Architecture Decision: Single Control Plane

**Date:** 2026-04-12
**Status:** Decided

## Problem

Two parallel execution architectures exist:
1. **Legacy Greek** — `titan/runner.py`, `hermes/runner.py`, `apollo/runner.py` (standalone processes, `--live` flag, own heartbeats)
2. **Managed Helio** — `helio/runner.py`, `helio/runner_hermes.py`, `helio/runner_apollo.py` (shared IBKR connection, config-driven, stage-based)

Both aim to run the same strategy families (swing, hermes, apollo) but with different execution models. This created:
- Duplicate heartbeats (legacy + managed for same strategy family)
- Watcher-stage lanes in managed Helio entering synthetic trades (should be observe-only)
- Fleet monitor trying to restart managed Helio daemons that kept dying
- Stale state artifacts from managed runners polluting dashboard/divergence checks
- Confusion about which path is "real"

## Decision

**Legacy Greek is the active execution path. Managed Helio is suspended.**

### Active Fleet (executing trades)

| System | Runner | Mode | Auto-Restart | Heartbeat |
|--------|--------|------|-------------|-----------|
| Argus FX | `argus_flow/runner_unified.py` | Paper (IBKR 7497) | Yes | Yes |
| Titan | `titan/runner.py --live --loop` | Live (IBKR) | Yes | Yes |
| Hermes | `hermes/runner.py --live --loop` | Live (IBKR) | Yes | Yes |
| Apollo | `apollo/runner.py --live --loop` | Live (IBKR) | Yes | Yes |

### Reporting Only (no execution)

| System | Runner | Mode | Auto-Restart | Heartbeat |
|--------|--------|------|-------------|-----------|
| Ares | `ares/runner.py` (dry-run) | Nightly report only | No | No |

### Suspended (disabled, not running)

| System | Runner | Why Suspended |
|--------|--------|--------------|
| helio.runner (swing) | `helio/runner.py` | Watcher lanes entered synthetic trades; restart disabled |
| helio.runner_hermes | `helio/runner_hermes.py` | Same issues; restart disabled |
| helio.runner_apollo | `helio/runner_apollo.py` | Same issues; restart disabled |

### Forge (paper tracking, isolated)

| System | Runner | Mode |
|--------|--------|------|
| GDX/GLD Pairs | `forge/gdx_gld_runner.py --signal-only --loop` | Signal-only, no IBKR |
| Atlas | `forge/atlas/runner.py --loop` | Event intelligence, no trading |
| Themis | `forge/themis/runner.py --loop` | Congressional tracking, no trading |

## What Was Done

1. **Fleet monitor**: Managed Helio entries (helio_swing, helio_hermes, helio_apollo) commented out. Fleet monitor no longer attempts to restart them.

2. **Watcher observe-only**: All three managed runners (runner.py, runner_hermes.py, runner_apollo.py) now enforce true observe-only in watcher stage — signals are logged but no positions are entered, no trades are recorded, no state is modified.

3. **State cleanup**: When any managed runner exits a position to FLAT, ALL metadata is cleared (entry_price, stop_price, initial_stop, trail_stop, target_price, highest, lowest, bars_held, entry_date). Existing stale state files for watcher lanes have been cleaned.

4. **Ares demoted**: Ares remains as a nightly dry-run reporter via `run_cohort_report.ps1`. It is NOT a deployed strategy. It has no heartbeat, no auto-restart, no daemon process. Fleet risk code will read its positions file if it exists (from `--execute` mode), but by default it doesn't exist.

## Re-Enabling Managed Helio (Criteria)

The managed Helio layer can be re-enabled when:
1. Watcher observe-only enforcement is verified in production (no synthetic trades after 1 week)
2. Paper-stage lanes are proven to write clean state artifacts
3. A decision is made on whether managed Helio REPLACES legacy Greek or runs alongside it
4. Auto-restart args are corrected (managed Helio shouldn't get `--live` flag)

## Long-Term: One Architecture

The end goal is one control plane, not two. The candidates:
- **Option A: Keep legacy Greek** — simpler, proven, each system is independent. Downside: no shared IBKR connection, more processes.
- **Option B: Migrate to managed Helio** — shared connection, config-driven, stage-based promotion. Downside: more complex, the bugs we just fixed.
- **Option C: New unified runner** — single process managing all strategies through a common framework. Most complex to build, cleanest long-term.

**Decision deferred** until after burn-in review (Apr 18). Legacy Greek works. Don't break what works during burn-in.

## Ares Promotion Path

If Ares is to become a deployed strategy:
1. Add `--loop` mode with heartbeat (like Titan/Hermes/Apollo)
2. Add positions.json persistence in run mode (not just execute)
3. Add to fleet_monitor.py for auto-restart
4. Add to paper_monitor_status.py for visibility
5. Add to portfolio_guard family accounting
6. Run in paper mode for 1 month, then review

Until then, Ares = monthly dry-run report. Not counted in fleet exposure.
