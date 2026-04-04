---
name: sweep
description: Run a parameter sweep on a strategy/pair. Usage /sweep AUDJPY apollo or /sweep GOLD_F hermes
allowed-tools: Bash Read
argument-hint: '[symbol] [strategy]'
disable-model-invocation: true
---

Run a parameter optimization sweep for $ARGUMENTS.

Working directory: C:\Argus\repo\helio

Strategy routing:
- apollo: `from strategies_backtest import apollo_backtest, compute_metrics` — sweep ema_period, extension_atr_mult, stop_mult, max_hold_days
- hermes: `from strategies_backtest import hermes_backtest, compute_metrics` — sweep consolidation_bars, breakout_atr_mult, vol_mult, atr_stop, atr_target
- helio: `from poc_backtest import run_swing_backtest` — sweep ema_period, atr_stop_mult, atr_trail_mult, max_hold_days
- argus: use `argus_flow/backtest/engine.py` with override_params

Show top 10 configurations ranked by profit factor. Include: params, trades, WR, PF, total%, max DD.
