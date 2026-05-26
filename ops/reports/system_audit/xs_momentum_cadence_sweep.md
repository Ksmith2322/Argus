# xs_momentum cadence sweep -- can we get more fills per year?

Generated: `2026-05-26T17:49:18.982375+00:00`
Window: 20y  |  Slippage: 10.0bps  |  Bootstrap CI floor: 1.2  |  Min n: 30

## Results

| Universe | Top-K | Cadence | n | trades/yr | PF | CI lower 95% | WR | Verdict |
|---|---:|---|---:|---:|---:|---:|---:|---|
| broad_8 | 2 | monthly | 454 | 22.7 | 1.49 | 1.19 | 0.58 | **MARGINAL** |
| broad_8 | 2 | biweekly | 990 | 49.6 | 1.22 | 1.03 | 0.56 | **FAIL** |
| broad_8 | 2 | weekly | 1980 | 99.1 | 1.12 | 1.00 | 0.55 | **FAIL** |
| sectors_spdr_11 | 2 | monthly | 164 | 20.7 | 1.74 | 1.16 | 0.58 | **MARGINAL** |
| sectors_spdr_11 | 2 | biweekly | 362 | 45.7 | 1.39 | 1.06 | 0.58 | **FAIL** |
| sectors_spdr_11 | 2 | weekly | 722 | 91.1 | 1.21 | 0.99 | 0.56 | **FAIL** |
| style_factors_8 | 2 | monthly | 214 | 21.4 | 1.74 | 1.20 | 0.64 | **SURVIVES** |
| style_factors_8 | 2 | biweekly | 470 | 47.1 | 1.41 | 1.10 | 0.60 | **FAIL** |
| style_factors_8 | 2 | weekly | 938 | 93.9 | 1.22 | 1.02 | 0.56 | **FAIL** |
| style_top3 | 3 | monthly | 321 | 32.1 | 1.74 | 1.30 | 0.63 | **SURVIVES** |
| style_top3 | 3 | biweekly | 705 | 70.6 | 1.44 | 1.18 | 0.60 | **MARGINAL** |
| style_top3 | 3 | weekly | 1407 | 140.9 | 1.24 | 1.07 | 0.56 | **FAIL** |
| legacy_sectors_countries_15 | 3 | monthly | 246 | 31.1 | 1.48 | 1.06 | 0.55 | **FAIL** |
| legacy_sectors_countries_15 | 3 | biweekly | 543 | 68.6 | 1.30 | 1.04 | 0.57 | **FAIL** |
| legacy_sectors_countries_15 | 3 | weekly | 1083 | 136.7 | 1.17 | 0.99 | 0.55 | **FAIL** |
| wide_global_47 | 6 | monthly | 492 | 62.1 | 1.68 | 1.34 | 0.57 | **SURVIVES** |
| wide_global_47 | 6 | biweekly | 1086 | 137.1 | 1.33 | 1.14 | 0.58 | **MARGINAL** |
| wide_global_47 | 6 | weekly | 2166 | 273.5 | 1.21 | 1.06 | 0.55 | **FAIL** |

## Summary: 3 SURVIVES, 4 MARGINAL

**Ship these as new variants** (passed all disciplined-gate layers):
- `style_factors_8` @ monthly -> +21.4 fills/year, PF 1.74, CI lower 1.20, WR 0.64
- `style_top3` @ monthly -> +32.1 fills/year, PF 1.74, CI lower 1.30, WR 0.63
- `wide_global_47` @ monthly -> +62.1 fills/year, PF 1.68, CI lower 1.34, WR 0.57

**Estimated additional fills/year**: 116  (~2.2/week)

**Marginal candidates** (CI lower 1.10-1.20; not quite gate-pass but close):
- `broad_8` @ monthly -> n=454, PF 1.49, CI lower 1.19
- `sectors_spdr_11` @ monthly -> n=164, PF 1.74, CI lower 1.16
- `style_top3` @ biweekly -> n=705, PF 1.44, CI lower 1.18
- `wide_global_47` @ biweekly -> n=1086, PF 1.33, CI lower 1.14