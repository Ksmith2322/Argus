# Pairs / stat-arb sweep

Generated: `2026-05-26T18:14:08.822268+00:00`
Slippage: 8.0bps RT (both legs)  |  CI floor: 1.2  |  Point PF margin: 1.3

Params: {"lookback_days": 60, "entry_z": 2.0, "exit_z": 0.0, "stop_z": 3.0, "max_hold_days": 20}

## Results

| Pair | Years | n | trades/yr | PF | CI lower | WR | Avg pnl | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| XLK/XLF | 20.0 | 146 | 7.3 | 0.92 | 0.53 | 0.46 | -0.10% | **FAIL** |
| XLE/USO | — | — | — | — | — | — | — | **NO_DATA** |
| GLD/TLT | 20.0 | 139 | 7.0 | 1.11 | 0.67 | 0.47 | 0.14% | **FAIL** |
| TLT/IEF | 20.0 | 133 | 6.7 | 1.27 | 0.74 | 0.49 | 0.11% | **FAIL** |
| SPY/IWM | 20.0 | 149 | 7.5 | 1.04 | 0.66 | 0.45 | 0.02% | **FAIL** |
| EWJ/EWG | 20.0 | 168 | 8.4 | 1.14 | 0.70 | 0.46 | 0.11% | **FAIL** |
| XLY/XLP | 20.0 | 126 | 6.3 | 0.67 | 0.37 | 0.46 | -0.60% | **FAIL** |
| QQQ/SPY | 20.0 | 150 | 7.5 | 1.11 | 0.63 | 0.43 | 0.06% | **FAIL** |
| XLK/QQQ | 20.0 | 139 | 7.0 | 0.54 | 0.32 | 0.37 | -0.23% | **FAIL** |
| EEM/EFA | 20.0 | 138 | 6.9 | 1.02 | 0.61 | 0.51 | 0.02% | **FAIL** |

## Summary: 0 SURVIVES, 0 MARGINAL

**No survivors.** Pairs/stat-arb on these ETF pairs does not survive the disciplined gate at 8bps round-trip.