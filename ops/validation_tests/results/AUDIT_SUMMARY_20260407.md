# Argus Full Audit & Validation Summary — 2026-04-07

## Part 1: Code Audit (27 bugs found, all fixed)

### Critical fixes applied:
1. Swing detection look-ahead bias eliminated (mtf_analysis/swing_detection.py)
2. Backtest now includes slippage (1 pip) + commission ($2/lot) + Friday close
3. Fill-to-bracket race condition: order rejection handler added
4. Partial fills now accumulate instead of overwrite
5. Fill dedup persisted to disk across restarts
6. Regime gate activated in configs (GATE mode)

### Other fixes: BarBuffer.last(), risk_pct 10% cap, stale tick from env, CSV fsync, cancel_order returns bool, per-instrument reconciliation, drawdown breaker no auto-reset, sizing zero-warning

## Part 2: Strategy Validation (~100 backtests, honest costs)

### Strategies tested:
- range_accel (existing) x 16 FX pairs
- FVG x 16 pairs
- liquidity_sweep x 16 pairs  
- volume_profile x 16 pairs
- drift_scalp_v1 (custom, hour-specific directional) x 5 pairs
- mean_revert_rsi_v1 (custom, RSI extremes) x 16 pairs
- momentum_burst_v1 (custom, range expansion) x 16 pairs

### Results: ZERO strategies profitable after honest costs
- Best result: drift_scalp AUDJPY PF 1.01 (+0.06 pips/trade)
- Walk-forward: 2/4 folds profitable, avg OOS PF 1.06
- Every other combination is losing after 1 pip slippage + 0.2 pip commission

### Statistical findings:
- EURUSD range_accel: p=0.60 (not significant), all trades on Fridays (data artifact)
- Monte Carlo: 29% of random shuffles beat the actual result
- Bootstrap 95% CI on expectancy: [-3.65, +6.34] — includes zero
- Minimum sample needed: 500 trades (have 36)
- Parameter sensitivity: robust plateau (PF range 0.24 across ±40%)

## Part 3: Edge Discovery (16 pairs analyzed)

### Key findings:
1. EURUSD has SHORT bias in sample period (-1.81 avg 2hr return)
2. Only USDJPY (+2.06), AUDJPY (+1.38), USDCHF (+1.16) have natural long drift
3. MFE data: median favorable move is 4-5 pips in 60 min — 40-pip targets almost never hit
4. Best combined filter: AUDJPY h=0 RSI<30 dist<0.3 = 90% WR (but small sample: 84 bars)
5. Hour 21 (US close / Asian open) shows strong directional moves across pairs
6. 86% of exits are timeouts — strategy waits for targets that don't come

## Part 4: Conclusions

### The fundamental problem:
Retail FX on 1-minute bars with 1-2 pip round-trip costs has no exploitable edge with simple conditional rules. The edge (if any) is smaller than transaction costs on most entries.

### Why static rules fail:
- Market conditions shift week to week
- Edge is <1 pip per trade — noise overwhelms signal
- Cost floor (slippage + commission) consumes any small directional bias
- Sample sizes too small for statistical significance

### Recommended next steps:
1. **Adaptive ensemble** — rules that learn on-the-fly which conditions are working
2. **Different asset class** — futures (lower per-trade costs), crypto (higher volatility)
3. **Higher timeframes** — 15m/1H bars to reduce cost-per-trade impact
4. **LLM-as-judge** — AI reasoning about chart context for go/no-go decisions
