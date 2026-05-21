---
name: EUR/USD FX Strategy — First Viable Candidate (2026-03-22)
description: Payoff-first EUR/USD strategy via IBKR. Passed full 7-test stress suite. Paper trading phase next.
type: project
---

## Status: VIABLE — Preparing for paper deployment

### Strategy: T4 Full Stack
- **Trigger:** range_pct >= 0.0012 AND range_accel > 0 AND vol_z > 0 AND session 08:00-19:00 UTC
- **Direction:** Long if near session low (dist_from_low < 0.4), Short if near high (> 0.6), else long
- **Stop:** 20 pips
- **Target:** 40 pips
- **Timeout:** 60 minutes
- **Frequency:** ~11.7 signals/day

### Backtest Results (18 days, 210 signals)
- Win rate: 54.8%
- Expectancy: +0.84 pips/trade
- Total: +176 pips
- Max drawdown: -163 pips
- Max consecutive losses: 8

### Stress Test Results (ALL PASS)
1. Entry delay: survives 3-bar delay (8.7% decay)
2. Session: profitable in London, NY, US evening
3. Fees: viable up to 1.0bps RT (IBKR is ~0.2bps)
4. Direction: both long (+0.01p) and short (+2.05p) positive
5. Temporal: both halves of dataset profitable
6. Parameters: gradual degradation, no cliff collapse
7. Drawdown: 8 max consecutive losses, controlled

### Caution Flags
- Target hit rate low (1.4%) — most wins are positive timeouts
- Short side much stronger than long
- Max DD nearly equals total profit
- Only 18 days data — need more history
- Needs live execution validation (fill quality, slippage)

### IBKR Setup
- Account: DUP472829 (paper trading)
- Gateway: TWS Gateway on port 4002
- Capital: $1M paper (standard IBKR paper account)
- EUR/USD contract qualified and data flowing

### Next Steps
1. Paper trade the T4 strategy live via IBKR
2. Collect 30+ trades for statistical validation
3. Monitor fill quality, slippage, execution timing
4. If results match backtest → consider micro live
