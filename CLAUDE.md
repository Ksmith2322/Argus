# CLAUDE.md

Guidance for Claude Code working in this repository. Last refreshed 2026-05-25.

## Environment

- **Repo root (canonical CWD):** `C:\Argus\repo`
- **Python venv:** `C:\Argus\.venv\Scripts\python.exe`
- **OS:** Windows 10. Default to PowerShell; `bash` also available via the Bash tool.
- **IBKR Gateway:** port **4002** (paper account, DUP472829). Set `IBKR_PORT=4002` env var when launching runners. TWS port 7497 still works if Gateway is down, but Gateway has been the production target since the 5/25 migration (commit `4f6cb0c`).
- All commands run from `C:\Argus\repo` working directory.

## Current state (2026-05-25, allocation v26)

**Active fleet collapsed from 22 → 11 strategies summing 4.10× anchor.** Post-5/22 reset epoch (`post_reset_20260522`, is_clean=True) is the source of truth for live evidence. See `argus_flow/configs/allocation_factors.json` v26 for the authoritative allocation table; the `_kill_log` field has dated reasoning for every recent change.

**Posture:** post-sunset evidence accumulation + selective new-strategy admission. Disciplined gate (20y window + 10bps slippage + bootstrap PF CI ≥ 1.20 + H1/H2 both pass) is the bar for any new candidate. Most academic anomalies fail in modern data; the few survivors get PARTIAL_PASS / MARGINAL_PASS shipped at small allocation.

**Active runners (12 processes, all on Gateway 4002):**

  forge.gld_pm_long.runner --loop                              client_id=102, 0.5×
  forge.xs_momentum.runner --variant baseline --loop          client_id=121, 1.0×
  forge.xs_momentum.runner --variant sectors --loop           client_id=122, 0.25×
  forge.xs_momentum.runner --variant style --loop             client_id=123, 0.5×
  forge.xs_momentum.runner --variant legacy15 --loop          client_id=124, 0.25×
  forge.xs_momentum.runner --variant style_top3 --loop        client_id=125, 0.5×
  forge.xs_momentum.runner --variant legacy15_regime --loop   client_id=126, 0.25×
  forge.tail_hedge.runner --loop                              client_id=127, 0.1×
  forge.xs_momentum.runner --variant global47 --loop          client_id=128, 0.25×
  forge.tom_spy.runner --loop                                 client_id=129, 0.3×
  forge.nov_spy.runner --loop                                 client_id=130, 0.2×
  forge.xs_momentum_consensus.runner --loop                   shadow (no broker)

**Killed / sunset (28 strategies)** — all in `helio.roi_filter.KILLED_STRATEGY_CUTOFFS` so `submit_bracket` refuses entries at runtime even if a stale runner is alive. Includes the 3 argus FX pairs, the YouTube YM replicas (mamba/tori/cuebanks), the Greek scanners (apollo/hermes/titan), and 4 kill-before-deploy candidates (overnight_drift_qqq / credit_spread_regime / sell_in_may_modulated / turn_of_quarter).

**Use the canonical launcher** rather than launching individual runners:

```powershell
.\ops\start_post_reset_runners.ps1 -DryRun    # see what would launch
.\ops\start_post_reset_runners.ps1            # launch missing
.\ops\start_post_reset_runners.ps1 -RestartAll
```

After a power outage / reboot, run the §1.5 morning recovery sequence in `OPERATOR_HANDOFF.md` first — stale scheduled tasks restart KILLED-strategy zombies but do NOT start the active v26 roster.

**Sizing rules** (`argus_flow/configs/fleet_sizing.json` v7):
- stock/etf cap = **1.0× anchor**
- fx cap = 20.0× anchor (sunset strategies; no active FX)
- micro_future cap = 5.0× anchor
- Risk per trade = `risk_pct × broker_equity`; `risk_pct` comes from tier × allocation_factor

**Client ID allocation** (no collisions):
- argus: 1, 12, 51, 53 (sunset; no active)
- Greek family: 60/70/80/90 (sunset; no active)
- forge active: 102 (gld_pm_long), 121-128 (xs_momentum variants + tail_hedge), 129 (tom_spy), 130 (nov_spy)
- forge killed: 103-120 (jpy_pm_short, nq_overnight, spy_mean_rev, vix_intraday, nq_london_close, etc.)

**Execution helpers** (use these for new runners, don't reinvent):
- `helio/ibkr_execution.py` — wake-and-sleep runners (forge/*). Functions: `connect`, `submit_bracket`, `query_position`, `check_bracket_filled`, `close_position_market`, `make_contract`.
- `helio/signal_executor.py` — scanner-style runners. `SignalEntry` dataclass + `submit_signal` / `check_open_positions` state machine.
- `helio/ibkr_executor.py` — Greek family (apollo/hermes/titan/ares) — separate older helper.

## Common commands

**Dashboard:**
```bash
# Already running on port 8080. Restart if needed:
powershell.exe -NoProfile -Command "Get-NetTCPConnection -LocalPort 8080 -State Listen | ForEach-Object { Stop-Process -Id \$_.OwningProcess -Force }"
Start-Process -FilePath 'C:\Argus\.venv\Scripts\python.exe' -ArgumentList 'ops/dashboard.py','--port','8080' -WorkingDirectory 'C:\Argus\repo' -WindowStyle Hidden
```

**Launch / restart any runner** (prefer the launcher script for the full fleet; this is for one-offs):
```powershell
$env:IBKR_PORT = '4002'
Start-Process -FilePath 'C:\Argus\.venv\Scripts\python.exe' -ArgumentList '-m','<module>','--loop' -WorkingDirectory 'C:\Argus\repo' -WindowStyle Hidden
```

**Quick fleet pulse** (from any cwd):
```bash
curl -s http://localhost:8080/api/gateway_status
curl -s http://localhost:8080/api/fleet_health
curl -s http://localhost:8080/api/positions_open
```

**Use the dashboard endpoints** for state checks rather than scanning files. The dashboard reads the same artifacts but normalizes them.

**Manual risk_oversight refresh** (when broker_equity gets stuck after Gateway restart):
```bash
cd c:/Argus/repo && C:/Argus/.venv/Scripts/python.exe -m argus_flow.ops.risk_oversight
```

**Fleet snapshot** (single-shot state of the bot — read this first when sitting down):
```powershell
$env:PYTHONIOENCODING = 'utf-8'
C:\Argus\.venv\Scripts\python.exe -X utf8 -m ops.audit.run_fleet_snapshot --skip-yfinance
```

## Critical rules (DO NOT VIOLATE)

1. **Don't modify trade logic on running strategies without restart**. Each runner process caches PARAMS at boot; live changes won't take effect until restart.
2. **Don't add fallback/clamp/default values to broker equity reads.** The 2026-04-23 architecture explicitly removed all fallbacks. `get_sizing_anchor_usd()` raises `BrokerEquityUnavailableError` if broker is unreachable. This is intentional — fallbacks let bugs hide.
3. **Don't use `git push --force` or `git reset --hard`** without explicit user authorization.
4. **Don't restart the fleet for non-urgent changes** — runners hold state. Restarts cost a few minutes of missed signals.
5. **Don't claim "done" without verification.** Use the `verification-before-completion` skill from obra/superpowers (installed in personal scope). Run the verify command first, share output, then claim status.

## Memory system

A persistent memory system at `C:\Users\ksmit\.claude\projects\c--Argus\memory\` holds project context, user preferences, and operational knowledge across sessions. **Always read `MEMORY.md` first** — it's the index. Notable entries for current era:

- **`project_2026_05_24_to_25_v18_through_v26.md`** — current-era state: v26 active roster (11+1), Gateway migration, replay bridge, power-cycle recovery doc
- **`project_2026_05_23_pre_tuesday_hardening.md`** — 5/23 hardening + live_gate_monitor silent-fallback bug fix
- **`project_2026_05_22_early_reset_and_ibc.md`** — the 5/22 epoch reset cutover ($250K, 5-survivor cohort) — start of `post_reset_20260522` epoch
- **`SESSION_2026_05_24.md`** (in the repo, not memory) — commit-by-commit summary of 5/24's 35+ commits
- **`feedback_master_before_build.md`** — default posture: monitor + tighten, not build
- **`feedback_dashboard_lean.md`** — one authoritative place per data point, no duplicate panels

## Legacy code (archived, not used)

`archive/` contains old crypto-trading code (Coinbase, Kraken). `engine.py` and `runner_live.py` still exist for the backtest system. `argus_flow/runner_unified.py` (FX pairs) was sunset 5/20 — the pairs are in KILLED_STRATEGY_CUTOFFS and the runner should not be relaunched. Live system is the forge runners listed in the active fleet table above.

## Backtest system

```bash
# FX backtest (new)
python -m argus_flow.ops.fx_backtest --config argus_flow/configs/gbpusd_range_paper_v1.json --data argus_flow/data/gbpusd_1m.csv

# Per-strategy backtest (varies — check the runner's --backtest mode)
python -m forge.spy_mean_rev.runner --backtest --period 60d
```

Backtest sandbox is enforced — never touches `live_events.csv` / `live_signals.csv`. `EXECUTION_MODE=ENGINE` is non-negotiable for backtest mode.

## Data

- IBKR data: streamed live via TWS, downloadable via `argus_flow/ops/download_ibkr_bars.py`
- yfinance: used by most forge runners for periodic bar updates
- Strategy backtest results: `argus_flow/data/backtest_results/` and `forge/logs/<strategy>/backtest_trades.csv`
