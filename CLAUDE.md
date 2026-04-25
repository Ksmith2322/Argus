# CLAUDE.md

Guidance for Claude Code working in this repository. Last refreshed 2026-04-24.

## Environment

- **Repo root (canonical CWD):** `C:\Argus\repo`
- **Python venv:** `C:\Argus\.venv\Scripts\python.exe`
- **OS:** Windows 10. Use `bash` shell within Claude Code (Unix-style paths in commands work).
- **IBKR TWS:** port **7497** (paper account, DUP472829). Set `IBKR_PORT=7497` env var when launching runners.
- All commands run from `C:\Argus\repo` working directory.

## Current state (2026-04-24)

**22 strategies submitting real orders to IBKR paper account.** Conversion completed 2026-04-24 — `project_execution_conversion_20260424.md` has the full table of strategies, client IDs, and instrument types.

**Posture:** monitoring + tighten. User feedback memory says: *"master what we have before building more."* Default behavior is to fix what breaks, not propose new features. See `feedback_master_before_build.md`.

**Three families running concurrently:**
- **Argus** (`argus_flow/runner_unified.py`) — 3 FX pairs (USD/JPY, GBP/USD, CAD/JPY) on TWS via ib_insync. Single-process multi-instrument runner.
- **Forge** (`forge/<strategy>/runner.py`) — 17 strategies. Each is its own runner process with `--loop` or `--live` mode. Examples: `gld_pm_long`, `multi_orb`, `spy_mean_rev`, `vix_intraday`, `nq_overnight`, `nq_london_close`, `aud_asian_breakout`, `wick_gbpusd`, `mamba`, `tori`, `cuebanks`, `fomc_drift`, `tom_international`, `vix_revert`, `rebalance`, `gdx_gld_runner`, plus `atlas`/`themis` as regime classifiers (no trades).
- **Greek** (`apollo/runner.py`, `hermes/runner.py`, `titan/runner.py`, `ares/runner.py`) — scanner-style runners. Each uses `--live` flag (or `--execute` for ares) to enable IBKR submission via `helio/ibkr_executor.py`.

**Sizing rules** (`argus_flow/configs/fleet_sizing.json` v6):
- stock/etf cap = **1.0× anchor** (was 2.0× before 2026-04-24)
- fx cap = 20.0× anchor (FX leverage acceptable; pip stops keep risk small)
- micro_future cap = 5.0× anchor
- Risk per trade = `risk_pct × broker_equity` where risk_pct comes from tier (unproven 0.5% → exceptional 3%)

**Client ID allocation** (no collisions):
- argus: 1 + per-pair (12, 51, 53)
- Greek family: 60 (titan), 70 (ares), 80 (hermes), 90 (apollo)
- forge: 101–117 (gdx_gld=101, gld_pm_long=102, ..., rebalance=117)

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

**Launch / restart any runner** (the IBKR_PORT env var matters):
```bash
$env:IBKR_PORT = '7497'
Start-Process -FilePath 'C:\Argus\.venv\Scripts\python.exe' -ArgumentList '-m','<module>','--loop' -WorkingDirectory 'C:\Argus\repo' -WindowStyle Hidden
```

**Quick fleet pulse** (from any cwd):
```bash
curl -s http://localhost:8080/api/gateway_status
curl -s http://localhost:8080/api/fleet_health
curl -s http://localhost:8080/api/positions_open
```

**Use the dashboard endpoints** for state checks rather than scanning files. The dashboard reads the same artifacts but normalizes them.

**Manual risk_oversight refresh** (when broker_equity gets stuck after TWS restart):
```bash
cd c:/Argus/repo && C:/Argus/.venv/Scripts/python.exe -m argus_flow.ops.risk_oversight
```

## Critical rules (DO NOT VIOLATE)

1. **Don't modify trade logic on running strategies without restart**. Each runner process caches PARAMS at boot; live changes won't take effect until restart.
2. **Don't add fallback/clamp/default values to broker equity reads.** The 2026-04-23 architecture explicitly removed all fallbacks. `get_sizing_anchor_usd()` raises `BrokerEquityUnavailableError` if broker is unreachable. This is intentional — fallbacks let bugs hide.
3. **Don't use `git push --force` or `git reset --hard`** without explicit user authorization.
4. **Don't restart the fleet for non-urgent changes** — runners hold state. Restarts cost a few minutes of missed signals.
5. **Don't claim "done" without verification.** Use the `verification-before-completion` skill from obra/superpowers (installed in personal scope). Run the verify command first, share output, then claim status.

## Memory system

A persistent memory system at `C:\Users\ksmit\.claude\projects\c--Argus\memory\` holds project context, user preferences, and operational knowledge across sessions. **Always read `MEMORY.md` first** — it's the index. Notable entries:

- **`project_monday_validation_20260427.md`** — read on return after 2026-04-24, has the validation sequence for first real fills
- **`project_execution_conversion_20260424.md`** — full table of 22 converted strategies + client IDs
- **`feedback_master_before_build.md`** — default posture: monitor + tighten, not build
- **`feedback_dashboard_lean.md`** — one authoritative place per data point, no duplicate panels
- **`reference_installed_skills.md`** — what skills are installed (personal + project), what was rejected and why
- **`reference_wifi_schedule.md`** — how to change the WiFi auto-on/off schedule

## Legacy code (archived, not used)

`archive/` contains old crypto-trading code (Coinbase, Kraken). `engine.py` and `runner_live.py` still exist for the backtest system but the live system is `argus_flow/runner_unified.py` + the runners listed above.

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
