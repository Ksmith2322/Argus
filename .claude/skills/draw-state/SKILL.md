---
name: draw-state
description: Read the current drawdown state. Peak PnL (R), current PnL (R), pause flag, minimum-peak-floor threshold. Quick check of whether the risk breaker is armed.
allowed-tools: Bash
---

Show the drawdown-breaker state.

```bash
C:/Argus/.venv/Scripts/python.exe -c "
import json
s = json.load(open('C:/Argus/repo/argus_flow/logs/_risk/portfolio_risk_state.json'))
peak = s.get('peak_pnl', 0)
cur = s.get('current_pnl', 0)
paused = s.get('drawdown_pause', False)

# Read MIN_PEAK_R_FOR_BREAKER from runner_unified (default 10.0)
MIN_PEAK_R = 10.0  # keep in sync with runner_unified.py PortfolioRiskManager

print(f'  peak_pnl_r    : {peak:.2f}R')
print(f'  current_pnl_r : {cur:.2f}R')
if peak > 0:
    dd_pct = (peak - cur) / abs(peak) * 100
    print(f'  drawdown_pct  : {dd_pct:.1f}%')
print(f'  drawdown_pause: {paused}')
print()

# Threshold analysis
if peak < MIN_PEAK_R:
    print(f'  BREAKER DISABLED — peak {peak:.2f}R < min floor {MIN_PEAK_R}R (warmup phase)')
    print(f'  Entries will NOT be paused by the drawdown breaker until peak reaches {MIN_PEAK_R}R.')
else:
    print(f'  BREAKER ARMED — peak {peak:.2f}R >= min floor {MIN_PEAK_R}R.')
    if paused:
        print(f'  ** CURRENTLY PAUSED ** — drop RESET_DRAWDOWN file or wait for 00:00 UTC session boundary:')
        print(f'      echo reset > C:/Argus/repo/RESET_DRAWDOWN')

# Any manual reset flag already armed?
import os
reset_file = 'C:/Argus/repo/RESET_DRAWDOWN'
if os.path.exists(reset_file):
    print(f'  RESET_DRAWDOWN flag present — will be consumed on next can_enter call.')
"
```

Interpret:
- **BREAKER DISABLED** — peak_pnl < 10R floor. Drawdown pause cannot fire. Normal during warmup.
- **BREAKER ARMED** — peak_pnl >= 10R floor. Drawdown pause will trigger if dd% exceeds `max_drawdown_pct` (default 5% for paper).
- **CURRENTLY PAUSED** — breaker engaged; entries blocked. Either wait for 00:00 UTC reset, or `echo reset > RESET_DRAWDOWN`.

Context: the min-peak-floor was added 2026-04-20 to prevent tiny-peak tyranny (+5R → +2R reading as 60% "drawdown" and locking entries forever). `runner_unified.py:3619`.
