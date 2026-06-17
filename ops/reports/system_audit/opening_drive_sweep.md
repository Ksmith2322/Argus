# Opening-drive sweep -- new intraday edge family

Generated: `2026-05-26T18:11:33.586355+00:00`
Slippage: 5.0bps RT  |  CI floor: 1.2  |  Point PF margin: 1.3  |  Min n: 100

## Results

| Ticker | Variant | Years | n | trades/yr | PF | CI lower | WR | Verdict |
|---|---|---:|---:|---:|---:|---:|---:|---|
| SPY | first_bar_continuation | 2.9 | 357 | 124.4 | 0.93 | 0.74 | 0.47 | **FAIL** |
| SPY | gap_continuation | 2.9 | 208 | 72.5 | 0.65 | 0.48 | 0.40 | **FAIL** |
| SPY | first_bar_reversal | 2.9 | 39 | 13.6 | 1.21 | 0.61 | 0.54 | **FAIL** |
| QQQ | first_bar_continuation | 2.9 | 353 | 123.0 | 1.01 | 0.81 | 0.48 | **FAIL** |
| QQQ | gap_continuation | 2.9 | 262 | 91.3 | 0.85 | 0.65 | 0.44 | **FAIL** |
| QQQ | first_bar_reversal | 2.9 | 87 | 30.3 | 0.83 | 0.51 | 0.41 | **FAIL** |
| IWM | first_bar_continuation | 2.9 | 356 | 124.1 | 0.85 | 0.69 | 0.42 | **FAIL** |
| IWM | gap_continuation | 2.9 | 268 | 93.4 | 0.73 | 0.55 | 0.40 | **FAIL** |
| IWM | first_bar_reversal | 2.9 | 86 | 30.0 | 1.08 | 0.69 | 0.49 | **FAIL** |
| DIA | first_bar_continuation | 2.9 | 358 | 124.8 | 0.89 | 0.71 | 0.44 | **FAIL** |
| DIA | gap_continuation | 2.9 | 197 | 68.7 | 0.81 | 0.59 | 0.42 | **FAIL** |
| DIA | first_bar_reversal | 2.9 | 36 | 12.5 | 1.21 | 0.60 | 0.53 | **FAIL** |
| XLK | first_bar_continuation | 2.9 | 353 | 123.0 | 1.04 | 0.84 | 0.47 | **FAIL** |
| XLK | gap_continuation | 2.9 | 284 | 99.0 | 0.87 | 0.67 | 0.47 | **FAIL** |
| XLK | first_bar_reversal | 2.9 | 109 | 38.0 | 0.81 | 0.54 | 0.40 | **FAIL** |
| XLF | first_bar_continuation | 2.9 | 358 | 124.8 | 0.84 | 0.68 | 0.41 | **FAIL** |
| XLF | gap_continuation | 2.9 | 232 | 80.9 | 0.69 | 0.52 | 0.41 | **FAIL** |
| XLF | first_bar_reversal | 2.9 | 55 | 19.2 | 0.92 | 0.52 | 0.44 | **FAIL** |
| XLE | first_bar_continuation | 2.9 | 371 | 129.3 | 0.88 | 0.72 | 0.41 | **FAIL** |
| XLE | gap_continuation | 2.9 | 301 | 104.9 | 0.91 | 0.71 | 0.44 | **FAIL** |
| XLE | first_bar_reversal | 2.9 | 116 | 40.4 | 0.99 | 0.68 | 0.47 | **FAIL** |
| MTUM | first_bar_continuation | 2.9 | 368 | 128.3 | 0.88 | 0.72 | 0.43 | **FAIL** |
| MTUM | gap_continuation | 2.9 | 261 | 91.0 | 0.81 | 0.62 | 0.44 | **FAIL** |
| MTUM | first_bar_reversal | 2.9 | 82 | 28.6 | 0.81 | 0.51 | 0.40 | **FAIL** |
| QUAL | first_bar_continuation | 2.9 | 354 | 123.4 | 0.81 | 0.66 | 0.42 | **FAIL** |
| QUAL | gap_continuation | 2.9 | 207 | 72.1 | 0.71 | 0.52 | 0.42 | **FAIL** |
| QUAL | first_bar_reversal | 2.9 | 41 | 14.3 | 1.08 | 0.57 | 0.51 | **FAIL** |
| USO | first_bar_continuation | 2.9 | 364 | 126.9 | 0.86 | 0.70 | 0.42 | **FAIL** |
| USO | gap_continuation | 2.9 | 332 | 115.7 | 0.85 | 0.67 | 0.43 | **FAIL** |
| USO | first_bar_reversal | 2.9 | 139 | 48.4 | 1.18 | 0.83 | 0.47 | **FAIL** |

## Summary: 0 SURVIVES, 0 MARGINAL

**No survivors.** Opening-drive momentum on these instruments + variants does not survive the disciplined gate at 5bps round-trip slippage.