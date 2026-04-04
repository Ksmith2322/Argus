---
name: trades
description: Show recent trades and PnL summary across all strategies. Optional symbol filter.
allowed-tools: Bash Read
argument-hint: '[symbol]'
---

Show recent trades across the Argus multiverse. If $ARGUMENTS provided, filter to that symbol.

1. **Scan Argus trades** from `argus_flow/logs/*/trades.csv`
2. **Scan Helio family trades** from `helio/logs/*/trades.csv`
3. For each trade show: date, strategy, symbol, direction, entry, exit, PnL, exit reason, duration
4. Calculate fleet summary: total trades, win rate, profit factor, total PnL
5. Show per-strategy breakdown: Argus vs Helio vs Apollo vs Hermes
6. Highlight the best and worst performing pairs
