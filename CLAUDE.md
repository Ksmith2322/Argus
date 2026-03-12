# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment

- **Repo root (canonical CWD):** `C:\Argus\repo`
- **Python venv:** `C:\Argus\.venv\Scripts\python.exe`
- All commands must be run from `C:\Argus\repo` as the working directory.

## Common Commands

**Run backtest (single run):**
```
C:\Argus\.venv\Scripts\python.exe -m backtest.runner
```

**Run backtest with full validation + determinism check (preferred):**
```powershell
.\ops\run_backtest.ps1
```

**Run Gate A Phase 8 acceptance harness:**
```powershell
.\ops\test_gate_a_phase8.ps1
```

**Run live loop:**
```
C:\Argus\.venv\Scripts\python.exe .\runner_live.py
```

**Debug single engine step:**
```
C:\Argus\.venv\Scripts\python.exe .\_debug_step.py
```

**Download candles data:**
```
C:\Argus\.venv\Scripts\python.exe -m backtest.download_candles
```

## Architecture

### High-Level Call Graph

**Backtest flow:**
`backtest/runner.py` → `config.load_config()` → `state.BotState.from_config(cfg)` → `backtest/loader.py` (CSV) → `backtest/feed.py` (tick stream) → `engine.step()` per tick → `io_logs` (sandboxed artifact writes) → `backtest/results.py` (metrics + invariants)

**Live flow:**
`runner_live.py` → `feed_coinbase.py` (Coinbase HTTP) → `engine.step()` → `io_logs` (live artifact writes) → `notify.py` (Discord hooks)

### Ownership Boundaries (critical — do not violate)

| Module | Owns |
|--------|------|
| `config.py` | All config defaults + env overrides. Single call to `load_config()` at startup. |
| `state.py` (`BotState`) | All sub-engine instances (strategy, confluence, regime, structure, liquidity, risk, ledger) and all caches (`last_st_*`, cooldown epoch, etc.) |
| `engine.py` (`step()`) | Decision orchestration, TF snapshot assembly, event emission. Does NOT own execution or exchange state. |
| `decisions.py` | `DecisionSnapshot` and `EngineEvent` schemas — the stable field contract for signals and events. |
| `io_logs.py` | **Only allowed** CSV/artifact writer. Owns schema headers, row mapping, path resolution, sandbox enforcement, pause/kill switch checks. |
| `backtest/results.py` | All attempt accounting, invariant checks, summary metrics. |

### Subsystem modules (market logic — all owned by BotState)
`strategy_phase2.py`, `confluence.py`, `adaptive_confluence.py`, `regime.py`, `structure.py`, `liquidity.py`, `session.py`, `risk.py`, `ledger.py`, `indicators.py`, `candles.py`

### Artifact Contract

All artifacts land in `C:\Argus\repo\ops\logs\` with run-scoped names:
- `bt_events_<run_id>.csv`, `bt_signals_<run_id>.csv`, `equity_<run_id>.csv`, `trades_<run_id>.csv`, `bt_summary_<run_id>.json`
- `bt_summary_latest.json` — pointer overwritten each run (must match newest `run_id`)

Live artifacts (append-only, never touched by backtest):
- `live_events.csv`, `live_signals.csv`

### Backtest EXECUTION_MODE Rule (non-negotiable)

`_ensure_bt_cfg()` in `backtest/runner.py` forces `cfg["EXECUTION_MODE"] = "ENGINE"` regardless of `.env`. This is intentional and must not be removed. In ADAPTER mode the engine emits intents but never calls `ledger.buy()` — the fill-back loop lives in `runner_live.py`, which doesn't run during backtest. Without this override, the ledger never updates and the engine re-enters on every eligible tick.

Live mode uses `EXECUTION_MODE=ADAPTER` (set in `.env`). Backtest always uses ENGINE.

### Sandbox Rule (non-negotiable)

Backtest must never write to `live_events.csv` or `live_signals.csv`. Enforced by setting env vars before importing `io_logs`:
```python
LIVE_EVENTS_CSV  = .../bt_sandbox_live_events.csv
LIVE_SIGNALS_CSV = .../bt_sandbox_live_signals.csv
ARGUS_DISABLE_LIVE_ARTIFACTS = "1"
```
`backtest/runner.py` sets these at module load time before any other imports. Do not reorder those early-init blocks.

### Attempt Accounting Invariants

These must hold after every backtest run (enforced by `backtest/results.py`):
- `entry_attempts == entry_filled + entry_blocked_total`
- `entry_attempt_gap == 0`
- `attempt_invariants_ok == True`
- `ENTRY_ATTEMPT` and `ENTRY_METRICS` events are debug-only — must not be counted as attempt outcomes

### Phase 8 Execution Layer

Live loop uses `EXECUTION_MODE=ADAPTER` + `PaperAdapter`. Key artifacts written to `ops\logs\`:
- `fills.csv`, `orders.csv`, `order_events.csv`, `positions.csv`, `account.csv` — adapter execution truth
- `recovery_<run_id>.json` — startup reconciliation report (fields: `ok`, `bot_state`, `source_of_truth`, `position_qty`)
- `state/runtime_state_ETH_USD.json` — crash-safe runtime snapshot (written on every loop + shutdown)

Recovery precedence: `fills.csv` → `positions.csv` → snapshot. Canonical truth always wins over snapshot.

Shell env hygiene: if `ARGUS_BT_ARTIFACT_DIR` is set in the shell from a prior run, backtest artifacts go to the wrong directory. Use `.\ops\run_backtest.ps1` (which calls `Reset-ArgusEnv`) for canonical validation, or `unset ARGUS_BT_ARTIFACT_DIR` before direct invocation.

### Key Config Env Vars

Config is loaded from `.env` in repo root (or path in `DOTENV_PATH`). Mode flag `ARGUS_MODE` controls backtest vs live behavior. `BACKTEST_LIMIT` controls how many candles to run (default `720`). `EXECUTION_MODE=ADAPTER` enables Phase 8 paper adapter for live; backtest ignores this.

## Hazard Files — Do Not Use

These are legacy entry points that reference a defunct `nova_scripts` package:
- `main.py`
- `run_main.py`
- `backtest/run_backtest_LEGACY_DO_NOT_USE.py`

Also avoid `logger.py` — it is a legacy duplicate IO path superseded by `io_logs.py`.

## Data

Historical candle CSV: `C:\Argus\repo\data\eth_usd_1m.csv`
Overridable via `BACKTEST_CSV` env var.
