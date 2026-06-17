# PM-pattern sweep -- extend gld_pm_long to other instruments

Generated: `2026-05-26T17:58:39.477237+00:00`
Hypothesis: the 18/19/20 UTC PM-long pattern (target +1.0 ATR / stop -0.5 ATR / hold 4 bars) works on liquid US ETFs beyond GLD.

**Slippage calibration**: 5.0 bps round-trip (realistic for intraday ETF execution at IBKR retail). Hourly strategies pay slippage on every trade -- different cost profile from monthly-cadence strategies (where 10bps is standard).

## Results

| Ticker | Years | n | trades/yr | PF gross 0bps | PF net 5bps | CI lower | WR | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| GLD | 2.9 | 763 | 265.9 | 1.65 | 1.08 | 0.94 | 0.45 | **FAIL** |
| SPY | 2.9 | 798 | 278.1 | 1.28 | 0.82 | 0.71 | 0.39 | **FAIL** |
| QQQ | 2.9 | 778 | 271.1 | 1.26 | 0.91 | 0.78 | 0.40 | **FAIL** |
| IWM | 2.9 | 784 | 273.2 | 1.06 | 0.81 | 0.70 | 0.35 | **FAIL** |
| DIA | 2.9 | 788 | 274.6 | 1.16 | 0.75 | 0.64 | 0.37 | **FAIL** |
| XLK | 2.9 | 782 | 272.5 | 1.22 | 0.93 | 0.81 | 0.39 | **FAIL** |
| XLF | 2.9 | 783 | 272.9 | 1.10 | 0.77 | 0.67 | 0.36 | **FAIL** |
| XLE | 2.9 | 768 | 267.7 | 1.07 | 0.83 | 0.71 | 0.35 | **FAIL** |
| XLV | 2.9 | 785 | 273.6 | 0.98 | 0.67 | 0.58 | 0.33 | **FAIL** |
| TLT | 2.9 | 783 | 272.9 | 1.21 | 0.76 | 0.66 | 0.38 | **FAIL** |
| USO | 2.9 | 744 | 259.3 | 1.71 | 1.40 | 1.20 | 0.47 | **SURVIVES** |
| MTUM | 2.9 | 781 | 272.2 | 1.27 | 0.91 | 0.78 | 0.39 | **FAIL** |
| QUAL | 2.9 | 792 | 276.0 | 1.16 | 0.74 | 0.64 | 0.37 | **FAIL** |
| EEM | 2.9 | 768 | 267.7 | 1.39 | 0.90 | 0.77 | 0.41 | **FAIL** |

## Summary: 1 SURVIVES, 0 MARGINAL

**Ship these as new forge runners** (replicate gld_pm_long pattern):
- `forge_uso_pm_long` -> +259 fills/yr, PF net 1.40, CI lower 1.20

**Estimated additional fills/year (excluding existing GLD)**: 259  (~5.0/week)