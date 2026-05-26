# Time-series momentum (TSMOM) sweep

Generated: `2026-05-26T22:54:34.662913+00:00`
Per Moskowitz-Ooi-Pedersen 2012; per-asset trend, monthly rebalance.
Slippage: 5.0bps RT  |  Gate: PF >= 1.3, CI lower >= 1.2, CAGR >= 13%, min n=20

## Summary: 11 SURVIVES, 17 MARGINAL, 588 FAIL/INS

**Ship-eligible** (passed all 4 gates including CAGR):
- `forge_nvda_tsmom_3m_long` -> n=27, PF 8.46, CI lower 2.98, CAGR 35.0%, +1.4 fills/yr
- `forge_nflx_tsmom_3m_long` -> n=24, PF 9.22, CI lower 2.22, CAGR 31.4%, +1.2 fills/yr
- `forge_amd_tsmom_3m_long` -> n=27, PF 4.89, CI lower 1.82, CAGR 22.0%, +1.4 fills/yr
- `forge_avgo_tsmom_3m_long` -> n=21, PF 6.66, CI lower 1.85, CAGR 22.0%, +1.2 fills/yr
- `forge_bkng_tsmom_3m_long` -> n=26, PF 8.35, CI lower 2.30, CAGR 21.6%, +1.3 fills/yr
- `forge_aapl_tsmom_3m_long` -> n=27, PF 6.66, CI lower 2.51, CAGR 19.2%, +1.4 fills/yr
- `forge_amzn_tsmom_3m_long` -> n=32, PF 5.43, CI lower 1.90, CAGR 18.2%, +1.6 fills/yr
- `forge_ma_tsmom_3m_long` -> n=27, PF 10.89, CI lower 3.03, CAGR 17.8%, +1.4 fills/yr
- `forge_smh_tsmom_3m_long` -> n=25, PF 7.28, CI lower 2.49, CAGR 17.1%, +1.2 fills/yr
- `forge_qqq_tsmom_3m_long` -> n=22, PF 13.03, CI lower 4.80, CAGR 13.7%, +1.1 fills/yr
- `forge_csx_tsmom_3m_long` -> n=27, PF 5.81, CI lower 2.04, CAGR 13.4%, +1.4 fills/yr

**Marginals** (passed PF + CI but missed CAGR or n):
- `forge_sbux_tsmom_3m_long` -> n=25, PF 4.65, CAGR 12.8%
- `forge_adbe_tsmom_3m_long` -> n=28, PF 4.23, CAGR 12.6%
- `forge_msft_tsmom_3m_long` -> n=28, PF 5.68, CAGR 12.4%
- `forge_cat_tsmom_3m_long` -> n=28, PF 4.29, CAGR 12.4%
- `forge_googl_tsmom_6m_long` -> n=20, PF 4.56, CAGR 12.1%
- `forge_ewy_tsmom_3m_long` -> n=26, PF 6.08, CAGR 12.0%
- `forge_xlk_tsmom_3m_long` -> n=25, PF 7.79, CAGR 11.7%
- `forge_v_tsmom_3m_long` -> n=23, PF 5.90, CAGR 11.6%
- `forge_googl_tsmom_3m_long` -> n=33, PF 4.07, CAGR 10.9%
- `forge_intu_tsmom_3m_long` -> n=26, PF 4.27, CAGR 10.5%
- `forge_etn_tsmom_3m_long` -> n=31, PF 3.37, CAGR 10.5%
- `forge_lly_tsmom_3m_long` -> n=32, PF 3.79, CAGR 9.5%
- `forge_axp_tsmom_6m_long` -> n=20, PF 4.79, CAGR 9.3%
- `forge_cost_tsmom_3m_long` -> n=29, PF 3.35, CAGR 8.5%
- `forge_unp_tsmom_3m_long` -> n=29, PF 3.72, CAGR 8.1%
- `forge_dhr_tsmom_6m_long` -> n=20, PF 4.69, CAGR 8.1%
- `forge_nke_tsmom_6m_long` -> n=20, PF 4.53, CAGR 8.0%

## Top 30 results by CAGR

| Ticker | Variant | n | PF | CI lower | CAGR | trades/yr | Verdict |
|---|---|---:|---:|---:|---:|---:|---|
| NVDA | tsmom_3m_long | 27 | 8.46 | 2.98 | 35.0% | 1.35 | **SURVIVES** |
| NFLX | tsmom_3m_long | 24 | 9.22 | 2.22 | 31.4% | 1.2 | **SURVIVES** |
| TSLA | tsmom_6m_long | 21 | 15.24 | 0.76 | 30.9% | 1.32 | **FAIL** |
| NFLX | tsmom_12m_long | 9 | 63.04 | 13.15 | 29.9% | 0.45 | **FAIL** |
| NVDA | tsmom_6m_long | 13 | 28.84 | 0.70 | 28.7% | 0.65 | **FAIL** |
| AVGO | tsmom_12m_long | 10 | 32.51 | 1.70 | 28.7% | 0.6 | **FAIL** |
| AVGO | tsmom_12m_with_vol | 10 | 43.38 | 1.38 | 27.0% | 0.6 | **FAIL** |
| TSLA | tsmom_3m_long | 25 | 10.40 | 0.59 | 26.0% | 1.57 | **FAIL** |
| AVGO | tsmom_6m_long | 15 | 13.23 | 2.77 | 25.9% | 0.89 | **FAIL** |
| AMD | tsmom_6m_long | 15 | 9.97 | 2.29 | 25.9% | 0.75 | **FAIL** |
| NFLX | tsmom_6m_long | 17 | 10.62 | 1.79 | 25.2% | 0.85 | **FAIL** |
| NVDA | tsmom_12m_long | 9 | 17.35 | 0.84 | 24.9% | 0.45 | **FAIL** |
| TSLA | tsmom_12m_long | 18 | 10.88 | 0.41 | 23.8% | 1.13 | **FAIL** |
| AMD | tsmom_3m_long | 27 | 4.89 | 1.82 | 22.0% | 1.35 | **SURVIVES** |
| AVGO | tsmom_3m_long | 21 | 6.66 | 1.85 | 22.0% | 1.25 | **SURVIVES** |
| BKNG | tsmom_3m_long | 26 | 8.35 | 2.30 | 21.6% | 1.3 | **SURVIVES** |
| AMZN | tsmom_12m_long | 11 | 21.27 | 3.13 | 21.5% | 0.55 | **FAIL** |
| NVDA | tsmom_12m_with_vol | 9 | 26.57 | 0.78 | 21.3% | 0.45 | **FAIL** |
| BKNG | tsmom_6m_long | 19 | 10.78 | 2.91 | 21.0% | 0.95 | **FAIL** |
| META | tsmom_12m_long | 5 | 1329.78 | 24.08 | 20.6% | 0.36 | **FAIL** |
| AMZN | tsmom_6m_long | 15 | 12.56 | 2.42 | 20.6% | 0.75 | **FAIL** |
| AAPL | tsmom_6m_long | 14 | 15.23 | 2.40 | 20.3% | 0.7 | **FAIL** |
| META | tsmom_6m_long | 11 | 9.31 | 0.82 | 19.5% | 0.78 | **FAIL** |
| META | tsmom_3m_long | 16 | 8.78 | 2.42 | 19.4% | 1.14 | **FAIL** |
| AAPL | tsmom_3m_long | 27 | 6.66 | 2.51 | 19.2% | 1.35 | **SURVIVES** |
| TSLA | tsmom_12m_with_vol | 18 | 11.07 | 0.51 | 18.4% | 1.13 | **FAIL** |
| AMZN | tsmom_3m_long | 32 | 5.43 | 1.90 | 18.2% | 1.6 | **SURVIVES** |
| AMZN | tsmom_12m_with_vol | 11 | 23.56 | 3.86 | 18.1% | 0.55 | **FAIL** |
| MA | tsmom_3m_long | 27 | 10.89 | 3.03 | 17.8% | 1.35 | **SURVIVES** |
| SMH | tsmom_3m_long | 25 | 7.28 | 2.49 | 17.1% | 1.25 | **SURVIVES** |