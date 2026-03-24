# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment

- **Repo root (canonical CWD):** `C:\Argus\repo`
- **Python venv:** `C:\Argus\.venv\Scripts\python.exe`
- All commands must be run from `C:\Argus\repo` as the working directory.

## Current Architecture: IBKR FX Unified Runner

The active trading system is `argus_flow/runner_unified.py` — a single-process, single-connection runner managing 3 FX pairs via Interactive Brokers TWS.

**Active cohort (Class A):** GBP/USD, EUR/USD, EUR/JPY
**Connection:** TWS port 7496, account U24860535, Read-Only API
**Goal:** 30 valid trades per pair for promotion to micro-live

### Common Commands

**Launch unified runner (preferred — use PowerShell to avoid zombie processes):**
```powershell
Start-Process -FilePath 'C:\Argus\.venv\Scripts\python.exe' -ArgumentList '-m','argus_flow.runner_unified','--configs','argus_flow/configs/gbpusd_range_paper_v1.json','argus_flow/configs/eurusd_t4_paper_v1.json','argus_flow/configs/eurjpy_t4_paper_v1.json' -WorkingDirectory 'C:\Argus\repo' -WindowStyle Hidden
```

**Run cohort compliance report:**
```
C:\Argus\.venv\Scripts\python.exe -m argus_flow.ops.daily_report
```

**Run position monitor:**
```
C:\Argus\.venv\Scripts\python.exe -m argus_flow.ops.position_monitor
```

**Run fault injection tests:**
```
C:\Argus\.venv\Scripts\python.exe -m argus_flow.tests.test_unified_faults
```

### Key Files (IBKR FX System)

| File | Purpose |
|------|---------|
| `argus_flow/runner_unified.py` | Unified multi-instrument runner (main entry point) |
| `argus_flow/schemas.py` | Canonical signal/trade CSV column definitions |
| `argus_flow/configs/*.json` | Per-instrument strategy configs |
| `argus_flow/COHORT_SPEC.md` | Cohort governance rules |
| `argus_flow/ops/daily_report.py` | Cohort compliance report |
| `argus_flow/ops/position_monitor.py` | Broker vs runner state reconciliation |
| `ops/dashboard.py` | Web dashboard (FastAPI) |
| `ops/run_cohort_report.ps1` | Nightly Task Scheduler wrapper |

### Artifact Locations

Per-instrument logs: `argus_flow/logs/<symbol>/`
- `state.json` — position persistence
- `signals.csv` — all signal evaluations
- `trades.csv` — closed trades with validity metadata
- `heartbeat.json` — runner liveness (pid, mode, broker status)
- `incidents/*.json` — reconciliation/quarantine incident artifacts

### Cohort Rules (DO NOT VIOLATE)

1. **Do not modify trade logic** while cohort is running — resets the 30-trade count
2. **Do not modify config files** — frozen per COHORT_SPEC.md
3. Every trade carries: `experiment_valid`, `invalid_reason`, `config_hash`, `session_id`, `runtime_epoch`, `git_sha`
4. Dashboard metrics use valid trades only

## Legacy Code (Archived)

The following systems are **archived** in `archive/` — not actively used:

- **Coinbase crypto** (`feed_coinbase.py`, `feed_ws.py`) — killed due to 60bps fees
- **BTC/ETH correlation** (`btc_momentum_guard.py`, `correlation_guard.py`) — crypto multi-coin only
- **Kraken research** (`argus_flow/capture/kraken_*.py`) — BTC spot research closed
- **Coin rotation** (`ops/coin_rotation.py`, `ops/rotate_coin.py`) — crypto multi-coin only
- **Batch builders** (`ops/_build_batch*.py`) — historical backtest queue definitions

`engine.py` and `runner_live.py` still exist for the backtest system but are NOT used for live trading. The active live system is `argus_flow/runner_unified.py`.

## Backtest System (Legacy Coinbase — still functional)

**Run backtest:**
```
C:\Argus\.venv\Scripts\python.exe -m backtest.runner
```

**With full validation:**
```powershell
.\ops\run_backtest.ps1
```

### Backtest Rules
- `_ensure_bt_cfg()` forces `EXECUTION_MODE=ENGINE` (non-negotiable)
- Backtest must never touch `live_events.csv` / `live_signals.csv` (sandbox enforced)
- `ARGUS_BT_ARTIFACT_DIR` env var can override artifact dir — use `run_backtest.ps1` which cleans this

## Data

- Historical crypto candles: `data/eth_usd_1m.csv`, `data/btc_usd_1m_90d.csv`
- IBKR FX data: streamed live via TWS (no historical CSV needed)