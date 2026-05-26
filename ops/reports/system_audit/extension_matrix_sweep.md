# Extension matrix sweep -- TOM + single-month calendar on ETFs

Generated: `2026-05-26T18:35:08.465406+00:00`
Slippage: 6.0bps RT  |  Gate: CI >= 1.2, PF >= 1.3, min n=20 (TOM) / 15 (single-month)

## Summary: 26 SURVIVES, 5 MARGINAL, 266 FAIL, 0 INSUFFICIENT

**Ship-eligible**:
- `forge_spy_tom_4_3` -> n=239, PF 1.79, CI lower 1.28, +12.0 fills/yr
- `forge_qqq_tom_4_3` -> n=239, PF 1.73, CI lower 1.25, +12.0 fills/yr
- `forge_dia_tom_4_3` -> n=239, PF 1.77, CI lower 1.26, +12.0 fills/yr
- `forge_xlk_tom_4_3` -> n=239, PF 1.66, CI lower 1.20, +12.0 fills/yr
- `forge_xlv_tom_4_3` -> n=239, PF 1.71, CI lower 1.23, +12.0 fills/yr
- `forge_xly_tom_4_3` -> n=239, PF 1.71, CI lower 1.23, +12.0 fills/yr
- `forge_eem_tom_4_3` -> n=239, PF 1.69, CI lower 1.23, +12.0 fills/yr
- `forge_inda_tom_4_3` -> n=171, PF 1.92, CI lower 1.28, +12.0 fills/yr
- `forge_spy_hold_jul` (Jul) -> n=20, PF 4.71, CI lower 1.55, +1.0 fills/yr
- `forge_spy_hold_nov` (Nov) -> n=20, PF 4.79, CI lower 1.46, +1.0 fills/yr
- `forge_qqq_hold_jul` (Jul) -> n=20, PF 7.40, CI lower 2.33, +1.0 fills/yr
- `forge_dia_hold_jul` (Jul) -> n=20, PF 4.58, CI lower 1.55, +1.0 fills/yr
- `forge_dia_hold_nov` (Nov) -> n=20, PF 4.39, CI lower 1.49, +1.0 fills/yr
- `forge_xlk_hold_jul` (Jul) -> n=20, PF 5.76, CI lower 1.89, +1.0 fills/yr
- `forge_xlv_hold_nov` (Nov) -> n=20, PF 3.94, CI lower 1.36, +1.0 fills/yr
- `forge_xly_hold_nov` (Nov) -> n=20, PF 4.46, CI lower 1.41, +1.0 fills/yr
- `forge_xlp_hold_apr` (Apr) -> n=20, PF 3.86, CI lower 1.44, +1.0 fills/yr
- `forge_xlp_hold_jul` (Jul) -> n=20, PF 3.58, CI lower 1.36, +1.0 fills/yr
- `forge_xlp_hold_nov` (Nov) -> n=20, PF 5.22, CI lower 1.99, +1.0 fills/yr
- `forge_xli_hold_nov` (Nov) -> n=20, PF 6.04, CI lower 1.92, +1.0 fills/yr
- `forge_xlu_hold_apr` (Apr) -> n=20, PF 5.06, CI lower 1.62, +1.0 fills/yr
- `forge_xlb_hold_nov` (Nov) -> n=20, PF 5.24, CI lower 1.39, +1.0 fills/yr
- `forge_ewg_hold_apr` (Apr) -> n=20, PF 3.51, CI lower 1.26, +1.0 fills/yr
- `forge_gld_hold_jan` (Jan) -> n=20, PF 4.23, CI lower 1.46, +1.0 fills/yr
- `forge_tlt_hold_jul` (Jul) -> n=20, PF 3.95, CI lower 1.41, +1.0 fills/yr
- `forge_ief_hold_jul` (Jul) -> n=20, PF 12.14, CI lower 4.21, +1.0 fills/yr

**Marginal (watch-list)**:
- `forge_xlf_tom_4_3` -> n=239, PF 1.66, CI lower 1.17
- `forge_xli_tom_4_3` -> n=239, PF 1.61, CI lower 1.14
- `forge_qual_tom_4_3` -> n=119, PF 1.81, CI lower 1.13
- `forge_ewz_tom_4_3` -> n=239, PF 1.56, CI lower 1.11
- `forge_efa_hold_apr` (Apr) -> n=20, PF 3.30, CI lower 1.19

## TOM (turn-of-month) results

| Ticker | n | PF | CI lower | WR | Avg | trades/yr | Verdict |
|---|---:|---:|---:|---:|---:|---:|---|
| INDA | 171 | 1.92 | 1.28 | 0.60 | 0.77% | 11.96 | **SURVIVES** |
| QUAL | 119 | 1.81 | 1.13 | 0.63 | 0.53% | 11.91 | **MARGINAL** |
| SPY | 239 | 1.79 | 1.28 | 0.63 | 0.56% | 11.96 | **SURVIVES** |
| DIA | 239 | 1.77 | 1.26 | 0.63 | 0.50% | 11.96 | **SURVIVES** |
| XLRE | 127 | 1.76 | 1.08 | 0.64 | 0.59% | 11.96 | **FAIL** |
| XLC | 95 | 1.75 | 1.00 | 0.65 | 0.66% | 11.99 | **FAIL** |
| QQQ | 239 | 1.73 | 1.25 | 0.62 | 0.68% | 11.96 | **SURVIVES** |
| XLY | 239 | 1.71 | 1.23 | 0.59 | 0.66% | 11.96 | **SURVIVES** |
| XLV | 239 | 1.71 | 1.23 | 0.62 | 0.48% | 11.96 | **SURVIVES** |
| EEM | 239 | 1.69 | 1.23 | 0.58 | 0.77% | 11.96 | **SURVIVES** |
| VTV | 119 | 1.68 | 1.00 | 0.61 | 0.37% | 11.91 | **FAIL** |
| VYM | 119 | 1.68 | 1.03 | 0.63 | 0.39% | 11.91 | **FAIL** |
| XLF | 239 | 1.66 | 1.17 | 0.62 | 0.64% | 11.96 | **MARGINAL** |
| XLK | 239 | 1.66 | 1.20 | 0.61 | 0.63% | 11.96 | **SURVIVES** |
| VLUE | 119 | 1.65 | 1.04 | 0.59 | 0.51% | 11.91 | **FAIL** |
| VIG | 119 | 1.64 | 1.02 | 0.56 | 0.35% | 11.91 | **FAIL** |
| USMV | 119 | 1.63 | 1.02 | 0.61 | 0.32% | 11.91 | **FAIL** |
| XLI | 239 | 1.61 | 1.14 | 0.56 | 0.54% | 11.96 | **MARGINAL** |
| VUG | 119 | 1.60 | 1.00 | 0.60 | 0.56% | 11.91 | **FAIL** |
| EWZ | 239 | 1.56 | 1.11 | 0.55 | 0.99% | 11.96 | **MARGINAL** |
| XLB | 239 | 1.56 | 1.09 | 0.56 | 0.57% | 11.96 | **FAIL** |
| FXI | 239 | 1.53 | 1.09 | 0.56 | 0.77% | 11.96 | **FAIL** |
| XLP | 239 | 1.51 | 1.08 | 0.56 | 0.31% | 11.96 | **FAIL** |
| MTUM | 119 | 1.44 | 0.88 | 0.58 | 0.38% | 11.91 | **FAIL** |
| EFA | 239 | 1.43 | 1.02 | 0.59 | 0.39% | 11.96 | **FAIL** |
| XLE | 239 | 1.41 | 0.99 | 0.52 | 0.57% | 11.96 | **FAIL** |
| XLU | 239 | 1.40 | 1.00 | 0.56 | 0.34% | 11.96 | **FAIL** |
| IWM | 239 | 1.40 | 1.00 | 0.57 | 0.46% | 11.96 | **FAIL** |
| EWG | 239 | 1.31 | 0.93 | 0.57 | 0.36% | 11.96 | **FAIL** |
| EWJ | 239 | 1.29 | 0.93 | 0.53 | 0.30% | 11.96 | **FAIL** |
| GLD | 239 | 1.24 | 0.88 | 0.54 | 0.22% | 11.96 | **FAIL** |
| TLT | 239 | 0.92 | 0.66 | 0.48 | -0.07% | 11.96 | **FAIL** |
| IEF | 239 | 0.89 | 0.64 | 0.47 | -0.05% | 11.96 | **FAIL** |

## Single-month calendar results (only SURVIVES / MARGINAL shown)

| Ticker | Month | n | PF | CI lower | WR | Avg | Verdict |
|---|---|---:|---:|---:|---:|---:|---|
| IEF | Jul | 20 | 12.14 | 4.21 | 0.75 | 1.00% | **SURVIVES** |
| QQQ | Jul | 20 | 7.40 | 2.33 | 0.80 | 2.93% | **SURVIVES** |
| XLI | Nov | 20 | 6.04 | 1.92 | 0.70 | 3.21% | **SURVIVES** |
| XLK | Jul | 20 | 5.76 | 1.89 | 0.75 | 2.77% | **SURVIVES** |
| XLB | Nov | 20 | 5.24 | 1.39 | 0.80 | 2.76% | **SURVIVES** |
| XLP | Nov | 20 | 5.22 | 1.99 | 0.70 | 2.13% | **SURVIVES** |
| XLU | Apr | 20 | 5.06 | 1.62 | 0.80 | 1.91% | **SURVIVES** |
| SPY | Nov | 20 | 4.79 | 1.46 | 0.65 | 2.18% | **SURVIVES** |
| SPY | Jul | 20 | 4.71 | 1.55 | 0.75 | 2.02% | **SURVIVES** |
| DIA | Jul | 20 | 4.58 | 1.55 | 0.65 | 1.81% | **SURVIVES** |
| XLY | Nov | 20 | 4.46 | 1.41 | 0.75 | 2.55% | **SURVIVES** |
| DIA | Nov | 20 | 4.39 | 1.49 | 0.70 | 2.38% | **SURVIVES** |
| GLD | Jan | 20 | 4.23 | 1.46 | 0.70 | 2.84% | **SURVIVES** |
| TLT | Jul | 20 | 3.95 | 1.41 | 0.65 | 1.35% | **SURVIVES** |
| XLV | Nov | 20 | 3.94 | 1.36 | 0.70 | 2.37% | **SURVIVES** |
| XLP | Apr | 20 | 3.86 | 1.44 | 0.60 | 1.43% | **SURVIVES** |
| XLP | Jul | 20 | 3.58 | 1.36 | 0.75 | 1.61% | **SURVIVES** |
| EWG | Apr | 20 | 3.51 | 1.26 | 0.75 | 2.94% | **SURVIVES** |
| EFA | Apr | 20 | 3.30 | 1.19 | 0.80 | 2.12% | **MARGINAL** |