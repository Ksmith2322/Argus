# Breakout sweep -- 52-week / monthly high breakouts on ETFs

Generated: `2026-05-26T19:11:32.820381+00:00`
71 tickers x 4 variants  |  Slippage: 6.0bps RT  |  Gate: CI >= 1.2, PF >= 1.3, min n=50

## Summary: 10 SURVIVES, 7 MARGINAL, 263 FAIL, 0 INSUFFICIENT

**Ship**:
- `forge_vug_quarterly_high_breakout` -> +6.4 fills/yr, PF 2.43, CI lower 1.30
- `forge_ewz_monthly_high_breakout` -> +11.6 fills/yr, PF 1.73, CI lower 1.27
- `forge_slv_fresh_high_with_atr_stop` -> +3.6 fills/yr, PF 2.10, CI lower 1.30
- `forge_slv_monthly_high_breakout` -> +11.8 fills/yr, PF 1.72, CI lower 1.27
- `forge_aapl_fresh_52w_high` -> +4.2 fills/yr, PF 2.14, CI lower 1.22
- `forge_aapl_fresh_high_with_atr_stop` -> +7.8 fills/yr, PF 1.67, CI lower 1.21
- `forge_aapl_monthly_high_breakout` -> +15.3 fills/yr, PF 1.97, CI lower 1.51
- `forge_aapl_quarterly_high_breakout` -> +6.0 fills/yr, PF 2.37, CI lower 1.56
- `forge_nvda_monthly_high_breakout` -> +15.8 fills/yr, PF 1.60, CI lower 1.23
- `forge_nvda_quarterly_high_breakout` -> +6.7 fills/yr, PF 1.96, CI lower 1.24

**Marginals**:
- `forge_vlue_monthly_high_breakout` -> n=139, PF 1.71, CI lower 1.14
- `forge_copx_fresh_high_with_atr_stop` -> n=56, PF 1.87, CI lower 1.10
- `forge_copx_monthly_high_breakout` -> n=181, PF 1.64, CI lower 1.16
- `forge_ura_monthly_high_breakout` -> n=159, PF 1.72, CI lower 1.16
- `forge_lit_quarterly_high_breakout` -> n=63, PF 2.08, CI lower 1.12
- `forge_googl_quarterly_high_breakout` -> n=117, PF 1.77, CI lower 1.13
- `forge_nvda_fresh_high_with_atr_stop` -> n=156, PF 1.58, CI lower 1.15

## All results

| Ticker | Variant | Years | n | trades/yr | PF | CI lower | WR | Avg | Verdict |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| SLV | fresh_52w_high | 20.0 | 35 | 1.75 | 3.88 | 1.55 | 0.49 | 4.54% | **FAIL** |
| LIT | fresh_52w_high | 15.8 | 31 | 1.96 | 3.36 | 1.36 | 0.48 | 3.44% | **FAIL** |
| COPX | fresh_52w_high | 16.1 | 26 | 1.62 | 3.11 | 1.15 | 0.42 | 3.53% | **FAIL** |
| XLC | quarterly_high_breakout | 7.9 | 44 | 5.55 | 2.64 | 1.24 | 0.64 | 1.14% | **FAIL** |
| VUG | quarterly_high_breakout | 10.0 | 64 | 6.41 | 2.43 | 1.30 | 0.70 | 1.02% | **SURVIVES** |
| AAPL | quarterly_high_breakout | 20.0 | 121 | 6.05 | 2.37 | 1.56 | 0.59 | 2.01% | **SURVIVES** |
| VUG | fresh_52w_high | 10.0 | 49 | 4.91 | 2.30 | 1.14 | 0.65 | 1.21% | **FAIL** |
| QUAL | fresh_52w_high | 10.0 | 45 | 4.51 | 2.16 | 1.12 | 0.67 | 0.85% | **FAIL** |
| AAPL | fresh_52w_high | 20.0 | 83 | 4.15 | 2.14 | 1.22 | 0.45 | 1.94% | **SURVIVES** |
| SLV | fresh_high_with_atr_stop | 20.0 | 73 | 3.65 | 2.10 | 1.30 | 0.53 | 1.34% | **SURVIVES** |
| LIT | quarterly_high_breakout | 15.8 | 63 | 3.98 | 2.08 | 1.12 | 0.56 | 1.57% | **MARGINAL** |
| GLD | fresh_52w_high | 20.0 | 51 | 2.55 | 2.00 | 1.08 | 0.47 | 1.48% | **FAIL** |
| EWY | fresh_52w_high | 20.0 | 43 | 2.15 | 1.99 | 0.86 | 0.42 | 1.72% | **FAIL** |
| AAPL | monthly_high_breakout | 20.0 | 306 | 15.3 | 1.97 | 1.51 | 0.54 | 0.79% | **SURVIVES** |
| NVDA | quarterly_high_breakout | 20.0 | 133 | 6.65 | 1.96 | 1.24 | 0.43 | 2.14% | **SURVIVES** |
| COPX | fresh_high_with_atr_stop | 16.1 | 56 | 3.48 | 1.87 | 1.10 | 0.50 | 1.04% | **MARGINAL** |
| BRK-B | fresh_52w_high | 20.0 | 66 | 3.3 | 1.87 | 1.07 | 0.55 | 1.17% | **FAIL** |
| SLV | quarterly_high_breakout | 20.0 | 84 | 4.2 | 1.86 | 1.08 | 0.44 | 1.79% | **FAIL** |
| USO | fresh_52w_high | 20.0 | 40 | 2.0 | 1.85 | 0.73 | 0.33 | 1.73% | **FAIL** |
| NVDA | fresh_52w_high | 20.0 | 105 | 5.25 | 1.82 | 1.00 | 0.28 | 1.83% | **FAIL** |
| AMZN | fresh_52w_high | 20.0 | 77 | 3.85 | 1.80 | 1.01 | 0.40 | 1.42% | **FAIL** |
| EWY | quarterly_high_breakout | 20.0 | 91 | 4.55 | 1.78 | 1.06 | 0.52 | 1.14% | **FAIL** |
| MSFT | fresh_52w_high | 20.0 | 74 | 3.7 | 1.77 | 1.03 | 0.53 | 1.02% | **FAIL** |
| GOOGL | quarterly_high_breakout | 20.0 | 117 | 5.85 | 1.77 | 1.13 | 0.52 | 1.22% | **MARGINAL** |
| QQQ | fresh_52w_high | 20.0 | 95 | 4.76 | 1.73 | 1.08 | 0.53 | 0.86% | **FAIL** |
| EWZ | monthly_high_breakout | 20.0 | 232 | 11.61 | 1.73 | 1.27 | 0.47 | 0.71% | **SURVIVES** |
| JNJ | fresh_52w_high | 20.0 | 50 | 2.5 | 1.72 | 0.89 | 0.54 | 0.85% | **FAIL** |
| URA | monthly_high_breakout | 15.6 | 159 | 10.22 | 1.72 | 1.16 | 0.41 | 0.84% | **MARGINAL** |
| SLV | monthly_high_breakout | 20.0 | 237 | 11.85 | 1.72 | 1.27 | 0.45 | 0.75% | **SURVIVES** |
| VLUE | monthly_high_breakout | 10.0 | 139 | 13.92 | 1.71 | 1.14 | 0.61 | 0.36% | **MARGINAL** |
| MTUM | fresh_52w_high | 10.0 | 43 | 4.31 | 1.70 | 0.83 | 0.58 | 0.86% | **FAIL** |
| EWY | fresh_high_with_atr_stop | 20.0 | 83 | 4.15 | 1.70 | 1.04 | 0.45 | 0.62% | **FAIL** |
| VLUE | quarterly_high_breakout | 10.0 | 55 | 5.51 | 1.69 | 0.91 | 0.64 | 0.71% | **FAIL** |
| VLUE | fresh_52w_high | 10.0 | 32 | 3.2 | 1.67 | 0.61 | 0.53 | 0.82% | **FAIL** |
| AAPL | fresh_high_with_atr_stop | 20.0 | 157 | 7.85 | 1.67 | 1.21 | 0.47 | 0.69% | **SURVIVES** |
| KWEB | fresh_52w_high | 12.8 | 27 | 2.11 | 1.66 | 0.56 | 0.37 | 1.28% | **FAIL** |
| GOOGL | fresh_52w_high | 20.0 | 75 | 3.75 | 1.66 | 0.92 | 0.43 | 1.11% | **FAIL** |
| DBA | quarterly_high_breakout | 19.4 | 67 | 3.46 | 1.66 | 0.91 | 0.52 | 0.74% | **FAIL** |
| VIG | fresh_52w_high | 10.0 | 43 | 4.31 | 1.65 | 0.79 | 0.67 | 0.50% | **FAIL** |
| COPX | monthly_high_breakout | 16.1 | 181 | 11.24 | 1.64 | 1.16 | 0.46 | 0.66% | **MARGINAL** |
| COPX | quarterly_high_breakout | 16.1 | 67 | 4.16 | 1.63 | 0.93 | 0.45 | 1.25% | **FAIL** |
| BRK-B | quarterly_high_breakout | 20.0 | 98 | 4.9 | 1.63 | 1.01 | 0.63 | 0.68% | **FAIL** |
| SMH | fresh_52w_high | 20.0 | 87 | 4.35 | 1.63 | 0.92 | 0.39 | 1.04% | **FAIL** |
| MSFT | quarterly_high_breakout | 20.0 | 111 | 5.55 | 1.61 | 1.05 | 0.54 | 0.87% | **FAIL** |
| NVDA | monthly_high_breakout | 20.0 | 316 | 15.8 | 1.60 | 1.23 | 0.36 | 0.77% | **SURVIVES** |
| QQQ | quarterly_high_breakout | 20.0 | 142 | 7.11 | 1.60 | 1.09 | 0.63 | 0.60% | **FAIL** |
| FXI | fresh_52w_high | 20.0 | 32 | 1.6 | 1.60 | 0.49 | 0.34 | 1.12% | **FAIL** |
| LIT | fresh_high_with_atr_stop | 15.8 | 61 | 3.85 | 1.59 | 0.91 | 0.46 | 0.64% | **FAIL** |
| NVDA | fresh_high_with_atr_stop | 20.0 | 156 | 7.8 | 1.58 | 1.15 | 0.44 | 0.98% | **MARGINAL** |
| XLY | quarterly_high_breakout | 20.0 | 118 | 5.91 | 1.55 | 0.99 | 0.56 | 0.59% | **FAIL** |
| VUG | monthly_high_breakout | 10.0 | 162 | 16.22 | 1.55 | 1.10 | 0.60 | 0.32% | **FAIL** |
| GLD | quarterly_high_breakout | 20.0 | 95 | 4.76 | 1.54 | 0.96 | 0.55 | 0.74% | **FAIL** |
| XLI | fresh_high_with_atr_stop | 20.0 | 121 | 6.06 | 1.53 | 1.04 | 0.46 | 0.34% | **FAIL** |
| LIT | monthly_high_breakout | 15.8 | 189 | 11.93 | 1.53 | 1.06 | 0.47 | 0.51% | **FAIL** |
| SPY | fresh_52w_high | 20.0 | 87 | 4.35 | 1.51 | 0.95 | 0.60 | 0.50% | **FAIL** |
| META | fresh_52w_high | 14.0 | 68 | 4.85 | 1.51 | 0.76 | 0.40 | 0.94% | **FAIL** |
| EMB | fresh_high_with_atr_stop | 18.4 | 60 | 3.25 | 1.51 | 0.88 | 0.47 | 0.13% | **FAIL** |
| HD | fresh_52w_high | 20.0 | 70 | 3.5 | 1.49 | 0.86 | 0.44 | 0.75% | **FAIL** |
| META | quarterly_high_breakout | 14.0 | 88 | 6.28 | 1.49 | 0.89 | 0.48 | 0.99% | **FAIL** |
| FXI | quarterly_high_breakout | 20.0 | 79 | 3.95 | 1.47 | 0.82 | 0.47 | 0.79% | **FAIL** |
| LQD | fresh_high_with_atr_stop | 20.0 | 60 | 3.0 | 1.47 | 0.86 | 0.47 | 0.12% | **FAIL** |
| XLK | fresh_52w_high | 20.0 | 99 | 4.96 | 1.46 | 0.91 | 0.49 | 0.63% | **FAIL** |
| KWEB | quarterly_high_breakout | 12.8 | 56 | 4.37 | 1.45 | 0.77 | 0.45 | 0.91% | **FAIL** |
| EWT | fresh_52w_high | 20.0 | 59 | 2.95 | 1.45 | 0.72 | 0.42 | 0.65% | **FAIL** |
| XLC | fresh_52w_high | 7.9 | 33 | 4.16 | 1.44 | 0.68 | 0.55 | 0.57% | **FAIL** |
| GLD | fresh_high_with_atr_stop | 20.0 | 111 | 5.56 | 1.44 | 0.95 | 0.46 | 0.32% | **FAIL** |
| GLD | monthly_high_breakout | 20.0 | 249 | 12.46 | 1.44 | 1.06 | 0.53 | 0.29% | **FAIL** |
| META | monthly_high_breakout | 14.0 | 210 | 14.98 | 1.43 | 1.02 | 0.42 | 0.47% | **FAIL** |
| XLK | quarterly_high_breakout | 20.0 | 135 | 6.76 | 1.43 | 0.94 | 0.59 | 0.49% | **FAIL** |
| QUAL | quarterly_high_breakout | 10.0 | 70 | 7.01 | 1.42 | 0.81 | 0.67 | 0.34% | **FAIL** |
| USO | monthly_high_breakout | 20.0 | 249 | 12.45 | 1.41 | 0.99 | 0.45 | 0.43% | **FAIL** |
| XLF | fresh_high_with_atr_stop | 20.0 | 97 | 4.86 | 1.41 | 0.92 | 0.41 | 0.29% | **FAIL** |
| USMV | fresh_52w_high | 10.0 | 37 | 3.7 | 1.40 | 0.66 | 0.57 | 0.32% | **FAIL** |
| TLT | fresh_high_with_atr_stop | 20.0 | 59 | 2.95 | 1.38 | 0.78 | 0.47 | 0.28% | **FAIL** |
| MTUM | quarterly_high_breakout | 10.0 | 62 | 6.21 | 1.37 | 0.78 | 0.66 | 0.44% | **FAIL** |
| USO | quarterly_high_breakout | 20.0 | 94 | 4.7 | 1.37 | 0.82 | 0.46 | 0.75% | **FAIL** |
| TLT | fresh_52w_high | 20.0 | 28 | 1.4 | 1.37 | 0.38 | 0.36 | 0.60% | **FAIL** |
| URA | fresh_52w_high | 15.6 | 28 | 1.8 | 1.37 | 0.37 | 0.25 | 0.84% | **FAIL** |
| XLP | fresh_52w_high | 20.0 | 58 | 2.9 | 1.35 | 0.71 | 0.64 | 0.30% | **FAIL** |
| GOOGL | fresh_high_with_atr_stop | 20.0 | 130 | 6.5 | 1.35 | 0.93 | 0.42 | 0.38% | **FAIL** |
| XLC | fresh_high_with_atr_stop | 7.9 | 67 | 8.46 | 1.34 | 0.81 | 0.42 | 0.26% | **FAIL** |
| VIG | quarterly_high_breakout | 10.0 | 66 | 6.61 | 1.33 | 0.74 | 0.65 | 0.24% | **FAIL** |
| JNJ | quarterly_high_breakout | 20.0 | 99 | 4.95 | 1.33 | 0.83 | 0.60 | 0.41% | **FAIL** |
| EWT | fresh_high_with_atr_stop | 20.0 | 109 | 5.45 | 1.32 | 0.87 | 0.41 | 0.25% | **FAIL** |
| QQQ | monthly_high_breakout | 20.0 | 336 | 16.82 | 1.32 | 1.03 | 0.56 | 0.21% | **FAIL** |
| SMH | fresh_high_with_atr_stop | 20.0 | 154 | 7.7 | 1.32 | 0.95 | 0.42 | 0.34% | **FAIL** |
| HD | fresh_high_with_atr_stop | 20.0 | 125 | 6.25 | 1.32 | 0.90 | 0.40 | 0.31% | **FAIL** |
| MSFT | fresh_high_with_atr_stop | 20.0 | 138 | 6.9 | 1.31 | 0.91 | 0.41 | 0.31% | **FAIL** |
| XLI | monthly_high_breakout | 20.0 | 285 | 14.27 | 1.31 | 0.99 | 0.53 | 0.20% | **FAIL** |
| DBA | fresh_52w_high | 19.4 | 28 | 1.44 | 1.31 | 0.49 | 0.36 | 0.53% | **FAIL** |
| EWY | monthly_high_breakout | 20.0 | 254 | 12.7 | 1.31 | 0.95 | 0.45 | 0.29% | **FAIL** |
| MSFT | monthly_high_breakout | 20.0 | 286 | 14.3 | 1.31 | 0.95 | 0.46 | 0.28% | **FAIL** |
| BRK-B | fresh_high_with_atr_stop | 20.0 | 128 | 6.4 | 1.30 | 0.89 | 0.42 | 0.25% | **FAIL** |
| AMZN | monthly_high_breakout | 20.0 | 306 | 15.3 | 1.30 | 0.99 | 0.41 | 0.33% | **FAIL** |
| AMZN | fresh_high_with_atr_stop | 20.0 | 120 | 6.0 | 1.30 | 0.86 | 0.41 | 0.41% | **FAIL** |
| MTUM | monthly_high_breakout | 10.0 | 152 | 15.22 | 1.30 | 0.88 | 0.56 | 0.20% | **FAIL** |
| MTUM | fresh_high_with_atr_stop | 10.0 | 81 | 8.11 | 1.28 | 0.77 | 0.41 | 0.21% | **FAIL** |
| XLC | monthly_high_breakout | 7.9 | 115 | 14.51 | 1.28 | 0.85 | 0.52 | 0.19% | **FAIL** |
| XLV | fresh_52w_high | 20.0 | 65 | 3.25 | 1.28 | 0.74 | 0.55 | 0.33% | **FAIL** |
| UNG | fresh_52w_high | 19.1 | 24 | 1.26 | 1.27 | 0.12 | 0.17 | 0.69% | **FAIL** |
| HD | quarterly_high_breakout | 20.0 | 121 | 6.05 | 1.26 | 0.83 | 0.54 | 0.43% | **FAIL** |
| KWEB | fresh_high_with_atr_stop | 12.8 | 50 | 3.9 | 1.25 | 0.63 | 0.38 | 0.30% | **FAIL** |
| JNJ | fresh_high_with_atr_stop | 20.0 | 97 | 4.85 | 1.25 | 0.81 | 0.40 | 0.19% | **FAIL** |
| IEF | fresh_52w_high | 20.0 | 33 | 1.65 | 1.24 | 0.49 | 0.46 | 0.17% | **FAIL** |
| SPY | quarterly_high_breakout | 20.0 | 136 | 6.81 | 1.24 | 0.84 | 0.63 | 0.23% | **FAIL** |
| VUG | fresh_high_with_atr_stop | 10.0 | 99 | 9.91 | 1.24 | 0.80 | 0.39 | 0.16% | **FAIL** |
| XLI | fresh_52w_high | 20.0 | 69 | 3.45 | 1.23 | 0.75 | 0.49 | 0.31% | **FAIL** |
| EWT | monthly_high_breakout | 20.0 | 253 | 12.65 | 1.22 | 0.90 | 0.48 | 0.19% | **FAIL** |
| USO | fresh_high_with_atr_stop | 20.0 | 66 | 3.3 | 1.22 | 0.71 | 0.39 | 0.34% | **FAIL** |
| WMT | monthly_high_breakout | 20.0 | 264 | 13.2 | 1.21 | 0.91 | 0.50 | 0.16% | **FAIL** |
| SMH | monthly_high_breakout | 20.0 | 301 | 15.05 | 1.21 | 0.92 | 0.47 | 0.20% | **FAIL** |
| QUAL | monthly_high_breakout | 10.0 | 166 | 16.62 | 1.20 | 0.83 | 0.55 | 0.10% | **FAIL** |
| JPM | fresh_52w_high | 20.0 | 58 | 2.9 | 1.20 | 0.64 | 0.36 | 0.38% | **FAIL** |
| GOOGL | monthly_high_breakout | 20.0 | 312 | 15.6 | 1.20 | 0.88 | 0.43 | 0.20% | **FAIL** |
| SMH | quarterly_high_breakout | 20.0 | 131 | 6.55 | 1.19 | 0.79 | 0.44 | 0.35% | **FAIL** |
| DIA | fresh_52w_high | 20.0 | 79 | 3.95 | 1.19 | 0.72 | 0.56 | 0.20% | **FAIL** |
| AMZN | quarterly_high_breakout | 20.0 | 128 | 6.4 | 1.18 | 0.75 | 0.42 | 0.37% | **FAIL** |
| BRK-B | monthly_high_breakout | 20.0 | 269 | 13.45 | 1.17 | 0.85 | 0.47 | 0.13% | **FAIL** |
| KWEB | monthly_high_breakout | 12.8 | 153 | 11.94 | 1.17 | 0.73 | 0.38 | 0.20% | **FAIL** |
| JPM | quarterly_high_breakout | 20.0 | 108 | 5.4 | 1.16 | 0.74 | 0.47 | 0.30% | **FAIL** |
| HD | monthly_high_breakout | 20.0 | 290 | 14.5 | 1.16 | 0.88 | 0.49 | 0.15% | **FAIL** |
| QUAL | fresh_high_with_atr_stop | 10.0 | 96 | 9.61 | 1.16 | 0.75 | 0.43 | 0.09% | **FAIL** |
| SPY | monthly_high_breakout | 20.0 | 330 | 16.52 | 1.16 | 0.88 | 0.53 | 0.08% | **FAIL** |
| EWC | monthly_high_breakout | 20.0 | 270 | 13.5 | 1.15 | 0.84 | 0.51 | 0.11% | **FAIL** |
| VIG | monthly_high_breakout | 10.0 | 161 | 16.12 | 1.15 | 0.78 | 0.55 | 0.07% | **FAIL** |
| XLV | quarterly_high_breakout | 20.0 | 115 | 5.76 | 1.15 | 0.77 | 0.54 | 0.17% | **FAIL** |
| WMT | fresh_52w_high | 20.0 | 63 | 3.15 | 1.15 | 0.58 | 0.38 | 0.23% | **FAIL** |
| ITB | fresh_high_with_atr_stop | 20.0 | 97 | 4.85 | 1.14 | 0.72 | 0.38 | 0.17% | **FAIL** |
| EWG | quarterly_high_breakout | 20.0 | 92 | 4.61 | 1.14 | 0.70 | 0.55 | 0.18% | **FAIL** |
| ITB | fresh_52w_high | 20.0 | 59 | 2.95 | 1.14 | 0.61 | 0.42 | 0.25% | **FAIL** |
| DIA | monthly_high_breakout | 20.0 | 317 | 15.87 | 1.14 | 0.87 | 0.53 | 0.07% | **FAIL** |
| USMV | monthly_high_breakout | 10.0 | 148 | 14.82 | 1.14 | 0.72 | 0.53 | 0.06% | **FAIL** |
| XLY | fresh_52w_high | 20.0 | 79 | 3.95 | 1.14 | 0.66 | 0.51 | 0.19% | **FAIL** |
| JPM | monthly_high_breakout | 20.0 | 267 | 13.35 | 1.13 | 0.82 | 0.48 | 0.12% | **FAIL** |
| XLV | fresh_high_with_atr_stop | 20.0 | 125 | 6.26 | 1.13 | 0.75 | 0.39 | 0.09% | **FAIL** |
| QQQ | fresh_high_with_atr_stop | 20.0 | 189 | 9.46 | 1.12 | 0.82 | 0.39 | 0.09% | **FAIL** |
| XLF | fresh_52w_high | 20.0 | 57 | 2.85 | 1.12 | 0.56 | 0.49 | 0.17% | **FAIL** |
| XLRE | fresh_52w_high | 10.6 | 22 | 2.07 | 1.12 | 0.39 | 0.55 | 0.14% | **FAIL** |
| EWJ | fresh_52w_high | 20.0 | 45 | 2.25 | 1.11 | 0.55 | 0.38 | 0.16% | **FAIL** |
| IEF | fresh_high_with_atr_stop | 20.0 | 73 | 3.65 | 1.11 | 0.64 | 0.42 | 0.04% | **FAIL** |
| XBI | quarterly_high_breakout | 20.0 | 106 | 5.3 | 1.11 | 0.68 | 0.42 | 0.21% | **FAIL** |
| IWM | fresh_high_with_atr_stop | 20.0 | 95 | 4.76 | 1.10 | 0.68 | 0.38 | 0.10% | **FAIL** |
| VYM | fresh_high_with_atr_stop | 10.0 | 63 | 6.31 | 1.10 | 0.64 | 0.41 | 0.06% | **FAIL** |
| VTV | monthly_high_breakout | 10.0 | 158 | 15.82 | 1.09 | 0.73 | 0.54 | 0.05% | **FAIL** |
| MA | quarterly_high_breakout | 20.0 | 132 | 6.6 | 1.09 | 0.72 | 0.53 | 0.15% | **FAIL** |
| WMT | quarterly_high_breakout | 20.0 | 108 | 5.4 | 1.09 | 0.67 | 0.47 | 0.15% | **FAIL** |
| VTV | fresh_52w_high | 10.0 | 36 | 3.6 | 1.09 | 0.50 | 0.56 | 0.10% | **FAIL** |
| XLE | fresh_high_with_atr_stop | 20.0 | 80 | 4.0 | 1.09 | 0.66 | 0.40 | 0.11% | **FAIL** |
| EMB | quarterly_high_breakout | 18.4 | 77 | 4.18 | 1.08 | 0.59 | 0.58 | 0.06% | **FAIL** |
| XLY | monthly_high_breakout | 20.0 | 301 | 15.07 | 1.08 | 0.82 | 0.48 | 0.06% | **FAIL** |
| EWJ | quarterly_high_breakout | 20.0 | 97 | 4.86 | 1.08 | 0.67 | 0.51 | 0.10% | **FAIL** |
| IWM | monthly_high_breakout | 20.0 | 276 | 13.82 | 1.08 | 0.82 | 0.51 | 0.06% | **FAIL** |
| XBI | fresh_52w_high | 20.0 | 66 | 3.3 | 1.08 | 0.56 | 0.32 | 0.15% | **FAIL** |
| V | monthly_high_breakout | 18.2 | 275 | 15.12 | 1.08 | 0.79 | 0.49 | 0.07% | **FAIL** |
| TLT | quarterly_high_breakout | 20.0 | 74 | 3.7 | 1.07 | 0.56 | 0.42 | 0.10% | **FAIL** |
| EWG | monthly_high_breakout | 20.0 | 262 | 13.11 | 1.07 | 0.77 | 0.48 | 0.05% | **FAIL** |
| WMT | fresh_high_with_atr_stop | 20.0 | 104 | 5.2 | 1.06 | 0.68 | 0.39 | 0.07% | **FAIL** |
| URA | fresh_high_with_atr_stop | 15.6 | 41 | 2.64 | 1.06 | 0.53 | 0.39 | 0.14% | **FAIL** |
| JNJ | monthly_high_breakout | 20.0 | 268 | 13.4 | 1.06 | 0.78 | 0.48 | 0.04% | **FAIL** |
| USMV | fresh_high_with_atr_stop | 10.0 | 81 | 8.11 | 1.06 | 0.66 | 0.38 | 0.03% | **FAIL** |
| DBA | fresh_high_with_atr_stop | 19.4 | 58 | 2.99 | 1.06 | 0.59 | 0.34 | 0.05% | **FAIL** |
| UNH | monthly_high_breakout | 20.0 | 281 | 14.05 | 1.05 | 0.77 | 0.42 | 0.06% | **FAIL** |
| EMB | monthly_high_breakout | 18.4 | 223 | 12.1 | 1.05 | 0.74 | 0.51 | 0.02% | **FAIL** |
| XLK | fresh_high_with_atr_stop | 20.0 | 186 | 9.31 | 1.05 | 0.75 | 0.37 | 0.04% | **FAIL** |
| VTV | quarterly_high_breakout | 10.0 | 61 | 6.11 | 1.05 | 0.56 | 0.61 | 0.05% | **FAIL** |
| VTV | fresh_high_with_atr_stop | 10.0 | 70 | 7.01 | 1.05 | 0.61 | 0.40 | 0.03% | **FAIL** |
| EWG | fresh_52w_high | 20.0 | 51 | 2.55 | 1.05 | 0.53 | 0.49 | 0.06% | **FAIL** |
| XLF | monthly_high_breakout | 20.0 | 273 | 13.66 | 1.04 | 0.78 | 0.46 | 0.04% | **FAIL** |
| XLI | quarterly_high_breakout | 20.0 | 120 | 6.01 | 1.04 | 0.69 | 0.54 | 0.06% | **FAIL** |
| XLK | monthly_high_breakout | 20.0 | 338 | 16.92 | 1.04 | 0.80 | 0.49 | 0.03% | **FAIL** |
| EEM | fresh_high_with_atr_stop | 20.0 | 92 | 4.61 | 1.04 | 0.64 | 0.37 | 0.03% | **FAIL** |
| SPY | fresh_high_with_atr_stop | 20.0 | 168 | 8.41 | 1.03 | 0.72 | 0.38 | 0.02% | **FAIL** |
| IEF | quarterly_high_breakout | 20.0 | 82 | 4.1 | 1.03 | 0.58 | 0.48 | 0.02% | **FAIL** |
| EEM | quarterly_high_breakout | 20.0 | 96 | 4.81 | 1.03 | 0.60 | 0.47 | 0.04% | **FAIL** |
| VYM | monthly_high_breakout | 10.0 | 142 | 14.22 | 1.03 | 0.68 | 0.51 | 0.01% | **FAIL** |
| EWC | fresh_52w_high | 20.0 | 62 | 3.1 | 1.02 | 0.58 | 0.52 | 0.03% | **FAIL** |
| XBI | monthly_high_breakout | 20.0 | 278 | 13.9 | 1.02 | 0.75 | 0.40 | 0.03% | **FAIL** |
| XLV | monthly_high_breakout | 20.0 | 277 | 13.87 | 1.02 | 0.77 | 0.51 | 0.01% | **FAIL** |
| MA | fresh_high_with_atr_stop | 20.0 | 160 | 8.0 | 1.02 | 0.71 | 0.37 | 0.02% | **FAIL** |
| V | fresh_52w_high | 18.2 | 90 | 4.95 | 1.02 | 0.63 | 0.42 | 0.03% | **FAIL** |
| DBA | monthly_high_breakout | 19.4 | 204 | 10.52 | 1.01 | 0.72 | 0.51 | 0.01% | **FAIL** |
| ITB | monthly_high_breakout | 20.0 | 257 | 12.85 | 1.01 | 0.76 | 0.42 | 0.01% | **FAIL** |
| EEM | monthly_high_breakout | 20.0 | 243 | 12.16 | 1.01 | 0.75 | 0.47 | 0.01% | **FAIL** |
| EWZ | fresh_52w_high | 20.0 | 35 | 1.75 | 1.01 | 0.36 | 0.26 | 0.01% | **FAIL** |
| EWC | fresh_high_with_atr_stop | 20.0 | 106 | 5.3 | 1.00 | 0.64 | 0.37 | 0.00% | **FAIL** |
| VYM | fresh_52w_high | 10.0 | 33 | 3.3 | 1.00 | 0.45 | 0.58 | 0.00% | **FAIL** |
| EWJ | fresh_high_with_atr_stop | 20.0 | 94 | 4.71 | 0.99 | 0.61 | 0.35 | -0.00% | **FAIL** |
| EWT | quarterly_high_breakout | 20.0 | 103 | 5.15 | 0.99 | 0.60 | 0.45 | -0.01% | **FAIL** |
| XLY | fresh_high_with_atr_stop | 20.0 | 150 | 7.51 | 0.99 | 0.68 | 0.35 | -0.01% | **FAIL** |
| IWM | quarterly_high_breakout | 20.0 | 105 | 5.26 | 0.99 | 0.61 | 0.49 | -0.01% | **FAIL** |
| XLE | monthly_high_breakout | 20.0 | 259 | 12.96 | 0.99 | 0.73 | 0.42 | -0.01% | **FAIL** |
| XLP | quarterly_high_breakout | 20.0 | 112 | 5.61 | 0.99 | 0.64 | 0.52 | -0.01% | **FAIL** |
| DIA | quarterly_high_breakout | 20.0 | 126 | 6.31 | 0.98 | 0.65 | 0.59 | -0.02% | **FAIL** |
| XLE | quarterly_high_breakout | 20.0 | 92 | 4.61 | 0.98 | 0.57 | 0.43 | -0.03% | **FAIL** |
| XLB | monthly_high_breakout | 20.0 | 255 | 12.76 | 0.98 | 0.72 | 0.45 | -0.01% | **FAIL** |
| XLRE | fresh_high_with_atr_stop | 10.6 | 40 | 3.77 | 0.98 | 0.41 | 0.33 | -0.02% | **FAIL** |
| EWZ | quarterly_high_breakout | 20.0 | 93 | 4.66 | 0.98 | 0.60 | 0.39 | -0.05% | **FAIL** |
| VLUE | fresh_high_with_atr_stop | 10.0 | 66 | 6.61 | 0.97 | 0.53 | 0.33 | -0.02% | **FAIL** |
| VIG | fresh_high_with_atr_stop | 10.0 | 93 | 9.31 | 0.97 | 0.61 | 0.37 | -0.01% | **FAIL** |
| TIP | quarterly_high_breakout | 20.0 | 84 | 4.2 | 0.97 | 0.57 | 0.51 | -0.01% | **FAIL** |
| XOM | quarterly_high_breakout | 20.0 | 86 | 4.3 | 0.97 | 0.55 | 0.43 | -0.05% | **FAIL** |
| EEM | fresh_52w_high | 20.0 | 45 | 2.25 | 0.97 | 0.42 | 0.47 | -0.05% | **FAIL** |
| MA | monthly_high_breakout | 20.0 | 317 | 15.85 | 0.97 | 0.73 | 0.43 | -0.04% | **FAIL** |
| XLF | quarterly_high_breakout | 20.0 | 107 | 5.36 | 0.96 | 0.59 | 0.53 | -0.06% | **FAIL** |
| IWM | fresh_52w_high | 20.0 | 59 | 2.95 | 0.96 | 0.51 | 0.42 | -0.07% | **FAIL** |
| XOM | monthly_high_breakout | 20.0 | 253 | 12.65 | 0.96 | 0.70 | 0.40 | -0.04% | **FAIL** |
| FXI | fresh_high_with_atr_stop | 20.0 | 66 | 3.3 | 0.95 | 0.52 | 0.33 | -0.06% | **FAIL** |
| INDA | fresh_52w_high | 14.3 | 36 | 2.52 | 0.95 | 0.38 | 0.36 | -0.08% | **FAIL** |
| META | fresh_high_with_atr_stop | 14.0 | 106 | 7.56 | 0.95 | 0.60 | 0.32 | -0.08% | **FAIL** |
| XLU | fresh_high_with_atr_stop | 20.0 | 83 | 4.15 | 0.95 | 0.56 | 0.34 | -0.04% | **FAIL** |
| LQD | quarterly_high_breakout | 20.0 | 85 | 4.25 | 0.94 | 0.54 | 0.49 | -0.04% | **FAIL** |
| PG | fresh_52w_high | 20.0 | 56 | 2.8 | 0.93 | 0.48 | 0.48 | -0.10% | **FAIL** |
| VYM | quarterly_high_breakout | 10.0 | 57 | 5.71 | 0.93 | 0.49 | 0.54 | -0.07% | **FAIL** |
| USMV | quarterly_high_breakout | 10.0 | 61 | 6.11 | 0.92 | 0.49 | 0.57 | -0.07% | **FAIL** |
| EMB | fresh_52w_high | 18.4 | 31 | 1.68 | 0.92 | 0.37 | 0.52 | -0.07% | **FAIL** |
| DIA | fresh_high_with_atr_stop | 20.0 | 145 | 7.26 | 0.92 | 0.61 | 0.37 | -0.05% | **FAIL** |
| JPM | fresh_high_with_atr_stop | 20.0 | 96 | 4.8 | 0.92 | 0.58 | 0.35 | -0.10% | **FAIL** |
| XOM | fresh_high_with_atr_stop | 20.0 | 78 | 3.9 | 0.91 | 0.53 | 0.29 | -0.11% | **FAIL** |
| EWU | monthly_high_breakout | 20.0 | 247 | 12.35 | 0.91 | 0.65 | 0.47 | -0.07% | **FAIL** |
| V | quarterly_high_breakout | 18.2 | 121 | 6.65 | 0.90 | 0.59 | 0.47 | -0.16% | **FAIL** |
| KRE | monthly_high_breakout | 19.9 | 249 | 12.5 | 0.90 | 0.65 | 0.38 | -0.11% | **FAIL** |
| URA | quarterly_high_breakout | 15.6 | 64 | 4.11 | 0.90 | 0.39 | 0.28 | -0.29% | **FAIL** |
| XLU | fresh_52w_high | 20.0 | 50 | 2.5 | 0.90 | 0.46 | 0.50 | -0.15% | **FAIL** |
| ITB | quarterly_high_breakout | 20.0 | 113 | 5.65 | 0.90 | 0.55 | 0.42 | -0.22% | **FAIL** |
| TIP | fresh_52w_high | 20.0 | 38 | 1.9 | 0.89 | 0.39 | 0.53 | -0.07% | **FAIL** |
| TLT | monthly_high_breakout | 20.0 | 212 | 10.61 | 0.89 | 0.62 | 0.46 | -0.08% | **FAIL** |
| UNH | quarterly_high_breakout | 20.0 | 117 | 5.85 | 0.88 | 0.56 | 0.44 | -0.22% | **FAIL** |
| V | fresh_high_with_atr_stop | 18.2 | 155 | 8.52 | 0.87 | 0.59 | 0.34 | -0.15% | **FAIL** |
| INDA | monthly_high_breakout | 14.3 | 186 | 13.01 | 0.87 | 0.61 | 0.47 | -0.11% | **FAIL** |
| EWA | monthly_high_breakout | 20.0 | 244 | 12.2 | 0.86 | 0.61 | 0.45 | -0.13% | **FAIL** |
| FXI | monthly_high_breakout | 20.0 | 245 | 12.27 | 0.85 | 0.60 | 0.36 | -0.17% | **FAIL** |
| MA | fresh_52w_high | 20.0 | 103 | 5.15 | 0.85 | 0.52 | 0.39 | -0.26% | **FAIL** |
| LQD | fresh_52w_high | 20.0 | 33 | 1.65 | 0.85 | 0.39 | 0.48 | -0.14% | **FAIL** |
| XBI | fresh_high_with_atr_stop | 20.0 | 107 | 5.35 | 0.85 | 0.54 | 0.31 | -0.23% | **FAIL** |
| XLP | monthly_high_breakout | 20.0 | 277 | 13.87 | 0.85 | 0.63 | 0.48 | -0.09% | **FAIL** |
| XLP | fresh_high_with_atr_stop | 20.0 | 113 | 5.66 | 0.84 | 0.55 | 0.34 | -0.11% | **FAIL** |
| EWA | quarterly_high_breakout | 20.0 | 92 | 4.6 | 0.83 | 0.47 | 0.47 | -0.26% | **FAIL** |
| HYG | monthly_high_breakout | 19.1 | 221 | 11.56 | 0.83 | 0.55 | 0.47 | -0.06% | **FAIL** |
| XLE | fresh_52w_high | 20.0 | 54 | 2.7 | 0.82 | 0.37 | 0.32 | -0.35% | **FAIL** |
| EWC | quarterly_high_breakout | 20.0 | 108 | 5.4 | 0.82 | 0.51 | 0.53 | -0.25% | **FAIL** |
| EWA | fresh_high_with_atr_stop | 20.0 | 67 | 3.35 | 0.82 | 0.43 | 0.28 | -0.17% | **FAIL** |
| XLRE | monthly_high_breakout | 10.6 | 130 | 12.24 | 0.81 | 0.51 | 0.43 | -0.16% | **FAIL** |
| IEF | monthly_high_breakout | 20.0 | 220 | 11.01 | 0.80 | 0.57 | 0.45 | -0.07% | **FAIL** |
| KRE | quarterly_high_breakout | 19.9 | 90 | 4.52 | 0.80 | 0.46 | 0.40 | -0.41% | **FAIL** |
| XLB | fresh_high_with_atr_stop | 20.0 | 98 | 4.91 | 0.80 | 0.48 | 0.32 | -0.20% | **FAIL** |
| UNG | quarterly_high_breakout | 19.1 | 62 | 3.25 | 0.80 | 0.36 | 0.27 | -0.57% | **FAIL** |
| LQD | monthly_high_breakout | 20.0 | 244 | 12.2 | 0.79 | 0.57 | 0.46 | -0.07% | **FAIL** |
| PG | quarterly_high_breakout | 20.0 | 105 | 5.25 | 0.78 | 0.47 | 0.47 | -0.32% | **FAIL** |
| XLRE | quarterly_high_breakout | 10.6 | 47 | 4.43 | 0.78 | 0.39 | 0.47 | -0.35% | **FAIL** |
| KRE | fresh_52w_high | 19.9 | 45 | 2.26 | 0.78 | 0.27 | 0.27 | -0.47% | **FAIL** |
| UNG | monthly_high_breakout | 19.1 | 199 | 10.42 | 0.78 | 0.51 | 0.25 | -0.34% | **FAIL** |
| PG | monthly_high_breakout | 20.0 | 265 | 13.25 | 0.77 | 0.57 | 0.44 | -0.19% | **FAIL** |
| KRE | fresh_high_with_atr_stop | 19.9 | 74 | 3.71 | 0.77 | 0.44 | 0.31 | -0.31% | **FAIL** |
| INDA | quarterly_high_breakout | 14.3 | 76 | 5.32 | 0.76 | 0.45 | 0.50 | -0.34% | **FAIL** |
| XOM | fresh_52w_high | 20.0 | 44 | 2.2 | 0.76 | 0.23 | 0.25 | -0.53% | **FAIL** |
| XLU | monthly_high_breakout | 20.0 | 264 | 13.21 | 0.76 | 0.56 | 0.42 | -0.20% | **FAIL** |
| TIP | fresh_high_with_atr_stop | 20.0 | 81 | 4.05 | 0.75 | 0.46 | 0.36 | -0.09% | **FAIL** |
| XLU | quarterly_high_breakout | 20.0 | 96 | 4.81 | 0.75 | 0.46 | 0.45 | -0.36% | **FAIL** |
| EFA | monthly_high_breakout | 20.0 | 270 | 13.51 | 0.75 | 0.55 | 0.45 | -0.19% | **FAIL** |
| XLB | quarterly_high_breakout | 20.0 | 100 | 5.01 | 0.75 | 0.45 | 0.46 | -0.36% | **FAIL** |
| EWZ | fresh_high_with_atr_stop | 20.0 | 55 | 2.75 | 0.73 | 0.38 | 0.29 | -0.45% | **FAIL** |
| EWJ | monthly_high_breakout | 20.0 | 263 | 13.16 | 0.73 | 0.52 | 0.42 | -0.22% | **FAIL** |
| EWU | quarterly_high_breakout | 20.0 | 95 | 4.75 | 0.72 | 0.43 | 0.53 | -0.37% | **FAIL** |
| EWG | fresh_high_with_atr_stop | 20.0 | 101 | 5.06 | 0.71 | 0.43 | 0.29 | -0.26% | **FAIL** |
| INDA | fresh_high_with_atr_stop | 14.3 | 70 | 4.9 | 0.70 | 0.38 | 0.27 | -0.23% | **FAIL** |
| UNH | fresh_52w_high | 20.0 | 89 | 4.45 | 0.69 | 0.39 | 0.35 | -0.59% | **FAIL** |
| PG | fresh_high_with_atr_stop | 20.0 | 96 | 4.8 | 0.69 | 0.42 | 0.27 | -0.29% | **FAIL** |
| EFA | fresh_52w_high | 20.0 | 58 | 2.9 | 0.67 | 0.37 | 0.45 | -0.45% | **FAIL** |
| TIP | monthly_high_breakout | 20.0 | 230 | 11.5 | 0.67 | 0.48 | 0.43 | -0.10% | **FAIL** |
| EFA | quarterly_high_breakout | 20.0 | 105 | 5.26 | 0.66 | 0.41 | 0.45 | -0.45% | **FAIL** |
| UNH | fresh_high_with_atr_stop | 20.0 | 135 | 6.75 | 0.65 | 0.42 | 0.27 | -0.48% | **FAIL** |
| UNG | fresh_high_with_atr_stop | 19.1 | 29 | 1.52 | 0.64 | 0.24 | 0.28 | -1.07% | **FAIL** |
| XLB | fresh_52w_high | 20.0 | 61 | 3.05 | 0.63 | 0.30 | 0.34 | -0.64% | **FAIL** |
| EFA | fresh_high_with_atr_stop | 20.0 | 106 | 5.31 | 0.62 | 0.38 | 0.27 | -0.28% | **FAIL** |
| EWU | fresh_high_with_atr_stop | 20.0 | 87 | 4.35 | 0.61 | 0.33 | 0.24 | -0.32% | **FAIL** |
| EWA | fresh_52w_high | 20.0 | 42 | 2.1 | 0.52 | 0.20 | 0.31 | -0.98% | **FAIL** |
| HYG | quarterly_high_breakout | 19.1 | 72 | 3.76 | 0.52 | 0.27 | 0.50 | -0.37% | **FAIL** |
| HYG | fresh_high_with_atr_stop | 19.1 | 47 | 2.46 | 0.49 | 0.20 | 0.23 | -0.18% | **FAIL** |
| EWU | fresh_52w_high | 20.0 | 50 | 2.5 | 0.48 | 0.24 | 0.36 | -0.93% | **FAIL** |
| HYG | fresh_52w_high | 19.1 | 29 | 1.52 | 0.46 | 0.15 | 0.38 | -0.33% | **FAIL** |