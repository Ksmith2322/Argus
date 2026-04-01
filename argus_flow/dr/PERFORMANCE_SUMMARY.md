# Argus FX — Performance Summary
# Auto-generated context: replaces RESEARCH dashboard tab (removed 2026-03-25)
# For live metrics, run: python -m argus_flow.ops.daily_report
# For promotion gate: python -m argus_flow.ops.promotion_gate_v2

## Active Cohort (Paper Trading)

| Pair | Strategy | Status | Trades | Valid | WR | PnL (pips) |
|------|----------|--------|--------|-------|----|------------|
| GBP/USD | range_accel | COLLECTING | 6 | 1 | 0% (valid only) | -4.65 |
| EUR/USD | T4_full_stack | COLLECTING | 3 | 0 | n/a | +7.15 (all) |
| EUR/JPY | T4_full_stack | COLLECTING | 1 | 0 | n/a | +5.80 (all) |

**Promotion target:** 30 valid trades per pair, positive expectancy, WR within 15pp of replay.

## Replay Expectations (from config)

| Pair | Signals/day | Win Rate | Exp (pips/trade) |
|------|-------------|----------|------------------|
| GBP/USD | 18.9 | 51.5% | +0.87 |
| EUR/USD | 8.0 | 54.2% | +0.98 |
| EUR/JPY | 5.0 | 55.0% | +1.50 |

## Key Metrics to Watch
- Invalid trade rate (target: <10%) — currently high due to reconnect tainting (fix deployed 2026-03-25)
- Signal frequency ratio (target: 0.5-1.5x replay) — promotion_gate bug fixed 2026-03-25
- Governor mode: LOG_ONLY (not blocking entries)

## Historical Notes
- Crypto runners (ETH/BTC) retired 2026-03-24 — pivot to IBKR FX
- SOL disabled earlier (PF 0.53-0.64, consistently unprofitable)
- Argus Cascade (crypto spot) closed 2026-03-22 — structural kill
- EUR/USD passed 7/7 stress tests in replay — first viable FX candidate

## How to Update
Run the daily report and paste key metrics:
```
C:\Argus\.venv\Scripts\python.exe -m argus_flow.ops.daily_report
C:\Argus\.venv\Scripts\python.exe -m argus_flow.ops.promotion_gate
```
Or check `argus_flow/logs/cohort_report_YYYYMMDD.json` for structured data.
