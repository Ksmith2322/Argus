---
name: trades
description: Show recent trades and PnL summary across all strategies. Optional symbol filter.
allowed-tools: Bash Read
argument-hint: '[symbol]'
---

## Pre-loaded fleet equity snapshot

!`curl -s "http://localhost:8080/api/fleet_equity_curve?window_days=90&include_backfill=false" 2>/dev/null | C:/Argus/.venv/Scripts/python.exe -c "import sys,json; d=json.load(sys.stdin); print(f'  anchor=\${d[\"anchor_capital_usd\"]:,.2f}  trades={d[\"total_trades\"]}  cum=\${d[\"current_cumulative_pnl_usd\"]:+.2f} ({d[\"current_cumulative_pnl_pct\"]:+.3f}%)'); print('  per-strategy:'); [print(f'    {k}  \${v:+.2f}') for k,v in d['contribution_by_strategy'].items()]"`

---

Show recent trades across the Argus multiverse. If $ARGUMENTS provided, filter to that symbol.

1. **Scan Argus trades** from `argus_flow/logs/*/trades.csv`
2. **Scan Helio family trades** from `helio/logs/*/trades.csv`
3. For each trade show: date, strategy, symbol, direction, entry, exit, PnL, exit reason, duration
4. Calculate fleet summary: total trades, win rate, profit factor, total PnL
5. Show per-strategy breakdown: Argus vs Helio vs Apollo vs Hermes
6. Highlight the best and worst performing pairs
