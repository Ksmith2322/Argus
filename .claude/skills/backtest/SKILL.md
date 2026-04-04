---
name: backtest
description: Run a backtest on a specific pair and strategy. Usage /backtest AUDJPY or /backtest GOLD helio
allowed-tools: Bash Read
argument-hint: '[symbol] [strategy]'
disable-model-invocation: true
---

Run a backtest for $ARGUMENTS.

Parse arguments:
- First arg = symbol (AUDJPY, EURUSD, GOLD_F, SPY, etc.)
- Second arg = strategy family (argus, helio, apollo, hermes) — default: auto-detect from symbol

Strategy routing:
- FX pairs (EURUSD, AUDJPY, etc.) → run Argus backtest via `argus_flow/backtest/engine.py`
- Gold/Futures (GOLD_F, MGC, etc.) → run Helio/Hermes backtest via `helio/poc_backtest.py` or `helio/strategies_backtest.py`
- If strategy=apollo → run `apollo_backtest()` from `helio/strategies_backtest.py`
- If strategy=hermes → run `hermes_backtest()` from `helio/strategies_backtest.py`

Working directory: C:\Argus\repo

After the backtest completes, show:
- Trade count, win rate, profit factor
- Total PnL, max drawdown
- Exit reason breakdown (stop/target/timeout percentages)
- Comparison to previous run if available
