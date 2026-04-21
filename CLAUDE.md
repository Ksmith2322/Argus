# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment

- **Repo root (canonical CWD):** `C:\Argus\repo`
- **Python venv:** `C:\Argus\.venv\Scripts\python.exe`
- All commands must be run from `C:\Argus\repo` as the working directory.

## Current Architecture: IBKR FX Unified Runner

The active trading system is `argus_flow/runner_unified.py` — a single-process, single-connection runner managing 3 FX pairs via Interactive Brokers TWS.

**Active cohort (Class A, stage=paper):** GBP/USD, USD/JPY
**Observer (stage=watcher, trading but not in cohort):** CAD/JPY
**Connection:** TWS port 7497 (paper), account DUP472829
**Goal:** 30 valid trades per PAPER pair for promotion to micro-live
**Note:** CADJPY promotes to PAPER when it accumulates enough live trades to qualify.

### Common Commands

**Launch entire fleet (runner + dashboard + discord):**
```powershell
.\ops\launch_fleet.ps1
```

**Launch unified runner only:**
```powershell
Start-Process -FilePath 'C:\Argus\.venv\Scripts\python.exe' -ArgumentList '-m','argus_flow.runner_unified','--configs','argus_flow/configs/gbpusd_range_paper_v1.json','argus_flow/configs/usdjpy_mtf_paper_v1.json','argus_flow/configs/cadjpy_mtf_paper_v1.json' -WorkingDirectory 'C:\Argus\repo' -WindowStyle Hidden
```

**Monitoring & reports:**
```
python -m argus_flow.ops.daily_report           # Cohort compliance
python -m argus_flow.ops.divergence_guard        # Replay-live divergence
python -m argus_flow.ops.correlation_guard       # USD/JPY correlation exposure
python -m argus_flow.ops.kill_discipline         # Kill rule enforcement
python -m argus_flow.ops.promotion_gate          # 13-check promotion criteria
python -m argus_flow.ops.artifact_divergence     # Dashboard/artifact truth check
python -m argus_flow.ops.risk_oversight          # Portfolio risk monitor
python -m argus_flow.ops.risk_oversight --watch  # Continuous monitoring (60s loop)
python -m argus_flow.ops.position_monitor        # Broker reconciliation
python -m argus_flow.ops.portfolio_pnl           # Cross-pair P&L aggregation
python -m argus_flow.ops.signal_analyzer         # Signal quality analysis
python -m argus_flow.ops.alert_escalation        # Consolidated Discord alerts
```

**Testing:**
```
python -m argus_flow.tests.test_unified_faults   # Fault injection (5 tests)
python -m argus_flow.tests.test_fx_system        # FX core components (14 tests)
python -m argus_flow.tests.chaos_test            # Adversarial data (10 suites)
```

**Backtesting & analysis:**
```
python -m argus_flow.ops.fx_backtest --config CONFIG --data DATA_CSV
python -m argus_flow.ops.fx_backtest --config CONFIG --data DATA_CSV --pyramid
python -m argus_flow.ops.download_ibkr_bars --symbol USDJPY --days 30
python -m argus_flow.ops.dual_analysis --type strategy_review --pair GBPUSD
```

**Pair management:**
```
python -m argus_flow.ops.onboard_pair --config CONFIG [--data DATA_CSV] [--auto]
python -m argus_flow.ops.generate_live_config CONFIG [--lot-size 1000] [--force]
python -m argus_flow.ops.retrain_governor_fx [--dry-run] [--min-trades 100]
```

**Experiments (Phase C rules):**
```
python -m argus_flow.ops.experiment_runner --create --name NAME --pair PAIR --param PARAM --baseline VAL --experiment VAL
python -m argus_flow.ops.experiment_runner --status
python -m argus_flow.ops.experiment_runner --evaluate --name NAME
```

**Ops scripts (PowerShell):**
```powershell
.\ops\launch_fleet.ps1           # Start runner + dashboard + discord watcher
.\ops\watchdog.ps1               # Auto-restart on crash (runs continuously)
.\ops\register_tasks.ps1         # One-click scheduled task registration (admin)
.\ops\run_cohort_report.ps1      # Nightly: daily_report + divergence + correlation + Discord
.\ops\run_weekly_digest.ps1      # Weekly FX Discord digest
```

### Key Files (IBKR FX System)

| File | Purpose |
|------|---------|
| `argus_flow/runner_unified.py` | Unified multi-instrument runner (main entry point) |
| `argus_flow/schemas.py` | Canonical signal/trade CSV column definitions |
| `argus_flow/configs/*.json` | Per-instrument strategy configs |
| `argus_flow/COHORT_SPEC.md` | Cohort governance rules |
| `argus_flow/dr/DR_RUNBOOK.md` | Disaster recovery procedures |
| `argus_flow/dr/PERFORMANCE_SUMMARY.md` | Cohort performance snapshot |
| `argus_flow/ops/daily_report.py` | Cohort compliance report |
| `argus_flow/ops/position_monitor.py` | Broker vs runner state reconciliation |
| `argus_flow/ops/divergence_guard.py` | Replay-live divergence (KILL/WATCH/PASS) |
| `argus_flow/ops/correlation_guard.py` | USD + JPY cross exposure limits |
| `argus_flow/ops/kill_discipline.py` | Automated kill rule enforcement |
| `argus_flow/ops/promotion_gate.py` | 13-check promotion criteria validator |
| `argus_flow/ops/artifact_divergence.py` | Dashboard/artifact truth checker |
| `argus_flow/ops/risk_oversight.py` | Phase 22B: portfolio risk monitor |
| `argus_flow/ops/signal_analyzer.py` | Signal quality + feature distributions |
| `argus_flow/ops/fx_backtest.py` | Offline replay backtest harness |
| `argus_flow/ops/onboard_pair.py` | Class B pair onboarding pipeline |
| `argus_flow/ops/generate_live_config.py` | Paper-to-live config generator |
| `argus_flow/ops/retrain_governor_fx.py` | FX governor model retraining |
| `argus_flow/ops/experiment_runner.py` | Phase C config A/B testing |
| `argus_flow/ops/portfolio_pnl.py` | Cross-pair P&L aggregation |
| `argus_flow/ops/alert_escalation.py` | Consolidated Discord alerting |
| `argus_flow/ops/download_ibkr_bars.py` | IBKR historical data downloader |
| `argus_flow/ops/dual_analysis.py` | Phase 22C: dual-model analysis prompts |
| `argus_flow/ops/weekly_digest.py` | Weekly Discord FX performance report |
| `argus_flow/ops/discord_alerts.py` | Trade notifications + daily summary |
| `ops/dashboard.py` | Web dashboard (FastAPI + SSE) |
| `ops/launch_fleet.ps1` | Fleet launcher (runner + dashboard + discord) |
| `ops/watchdog.ps1` | Auto-restart watchdog |
| `ops/register_tasks.ps1` | Scheduled task registration |

### Artifact Locations

Per-instrument logs: `argus_flow/logs/<symbol>/`
- `state.json` — position persistence
- `signals.csv` — all signal evaluations
- `trades.csv` — closed trades with validity metadata
- `heartbeat.json` — runner liveness (pid, mode, broker status)
- `incidents/*.json` — reconciliation/quarantine incident artifacts

Fleet-level reports: `argus_flow/logs/`
- `cohort_report_*.json` — daily compliance
- `divergence_report.json` — replay-live divergence
- `correlation_check.json` — exposure alerts
- `kill_discipline_report.json` — kill rule status
- `promotion_gate_report.json` — promotion criteria
- `artifact_divergence_report.json` — truth checking
- `risk_oversight_report.json` — portfolio risk
- `portfolio_pnl_report.json` — P&L aggregation
- `position_monitor.json` — broker reconciliation
- `alert_history.json` — alert cooldown tracking

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

**FX backtest (new):**
```
python -m argus_flow.ops.fx_backtest --config argus_flow/configs/gbpusd_range_paper_v1.json --data argus_flow/data/gbpusd_1m.csv
```

### Backtest Rules
- `_ensure_bt_cfg()` forces `EXECUTION_MODE=ENGINE` (non-negotiable)
- Backtest must never touch `live_events.csv` / `live_signals.csv` (sandbox enforced)
- `ARGUS_BT_ARTIFACT_DIR` env var can override artifact dir — use `run_backtest.ps1` which cleans this

## Data

- Historical crypto candles: `data/eth_usd_1m.csv`, `data/btc_usd_1m_90d.csv`
- IBKR FX data: streamed live via TWS, downloadable via `download_ibkr_bars.py`
- FX backtest results: `argus_flow/data/backtest_results/`
