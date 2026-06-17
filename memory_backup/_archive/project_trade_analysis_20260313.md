---
name: Trade analysis 2026-03-13 — duration is the key signal
description: 30-day backtest trade pattern analysis showing 1-4hr trades are 97% losers; time-stop at 90min implemented (MAX_HOLD_SECONDS=5400)
type: project
---

Trade analysis from run bt_20260313T115346Z_c69d4cc7 (77 closed trades, 30-day ETH-USD, CONFLUENCE_MIN_SCORE=72):

**Key finding: Duration predicts outcome**
- <1hr: 29 trades, 41% WR, +$3.71 (WINNING)
- 1-4hr: 29 trades, 3% WR, -$6.64 (NEARLY ALL LOSSES)
- 4-12hr: 18 trades, 28% WR, -$0.74

**Win/Loss ratio is healthy (2.25x)**
- Avg win: $0.524, Median win: $0.720
- Avg loss: $-0.233, Median loss: $-0.225
- Breakeven WR at this ratio: 30.7%
- Actual WR: 24.7% → edge = -6.1%

**Big winners carry the portfolio**
- 14 of 19 wins are >$0.20 (total $9.57)
- Max consecutive losses: 10

**Time-stop implemented 2026-03-13**
- MAX_HOLD_SECONDS=5400 (90 min) added to config.py + engine.py
- Priority: STOP_LOSS > MIN_HOLD > TIME_STOP > TREND_INVALIDATION > TRAIL_STOP > TAKE_PROFIT
- Expected impact: recover $3-5 from the 1-4hr loser bucket
- Set to 0 in .env to disable

**Why:** 1-4hr trades are almost always losers (3% WR). If price hasn't moved favorably in 90 min, the trade thesis is invalidated.
**How to apply:** Compare backtest results with and without time-stop. Tune the threshold (60/90/120 min) based on results.
