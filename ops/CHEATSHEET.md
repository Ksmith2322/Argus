# Argus Quick Reference Cheatsheet

All commands run from: C:\Argus\repo
Python: C:\Argus\.venv\Scripts\python.exe

## LAUNCH
```powershell
.\argus_flow\launch_all.ps1                          # All 3 IBKR runners + checks
python -m argus_flow.runner_eurusd                    # EUR/USD only
python -m argus_flow.runner_mnq                       # MNQ only
python -m argus_flow.runner_gbpusd                    # GBP/USD only
python ops/dashboard.py                               # Dashboard (localhost:8080)
.\ops\autostart_runner.ps1                            # Argus crypto runners
```

## MONITOR
```powershell
python -m argus_flow.ops.health_check                 # Fleet status overview
python -m argus_flow.ops.heartbeat_monitor             # Runner alive/dead check
python -m argus_flow.ops.position_monitor              # IBKR position reconciliation
python -m argus_flow.ops.divergence_guard              # Replay vs live comparison
python -m argus_flow.ops.correlation_guard             # EUR+GBP exposure check
python -m argus_flow.ops.daily_report                  # Daily performance summary
python -m argus_flow.ops.discord_alerts --test         # Test Discord connection
python -m argus_flow.ops.discord_alerts --watch        # Watch for trades (background)
python -m argus_flow.ops.discord_alerts --summary      # Send daily Discord summary
```

## TEST
```powershell
python -m argus_flow.ops.smoke_test                    # Pre-launch 7-point check
python -m argus_flow.ops.config_check                  # Validate + hash configs
python -m argus_flow.tests.adversarial_tests           # Tier 1 adversarial tests
python -m argus_flow.tests.test_kraken_connectivity    # Kraken API check
```

## DATA
```powershell
python -m argus_flow.ops.refresh_ibkr_data             # Pull latest IBKR bars
python -m argus_flow.capture.ingest_kraken_trades --pair XBTUSD --days 14
```

## BACKTEST / RESEARCH
```powershell
.\ops\run_backtest.ps1                                # Argus crypto backtest
.\ops\run_queue.ps1                                   # Run backtest queue
.\ops\run_oos_validation.ps1 -Coin ETH                # Out-of-sample test
.\ops\run_walk_forward.ps1 -Coin ETH                  # Walk-forward test
python -m argus_flow.run_pipeline --trades [file]      # Cascade pipeline
python -m argus_flow.analytics.eurusd_payoff_test      # EUR/USD strategy test
python -m argus_flow.analytics.eurusd_stress_test      # EUR/USD 7-test suite
python -m argus_flow.strategies.test_all_strategies    # Compare FVG/Sweep/VP
```

## KEY PATHS
```
Repo:           C:\Argus\repo
Venv:           C:\Argus\.venv\Scripts\python.exe
Config:         C:\Argus\repo\.env
IBKR Configs:   argus_flow/configs/*.json
IBKR Logs:      argus_flow/logs/{eurusd,mnq,gbpusd}/
Argus Logs:     ops/logs/
Dashboard:      http://localhost:8080
```

## KEY ACCOUNTS
```
IBKR:     DUP472829 (paper) — port 4002
Kraken:   API keys in .env
GitHub:   Ksmith2322/Argus (branch: phase6-hardening)
Discord:  Webhook in .env
```

## EMERGENCY
```powershell
# Check positions immediately:
python -m argus_flow.ops.position_monitor

# Kill all IBKR runners:
# Close PowerShell windows titled IBKR-EURUSD, IBKR-MNQ, IBKR-GBPUSD

# Manual position close:
# Open IBKR Trader Workstation GUI > right-click position > Close

# Full system restart:
.\argus_flow\launch_all.ps1
```