---
name: Comparison Tests Status
description: Status of liq-fix and trendlines comparison backtests — Phase 16.5
type: project
---

## Baseline Run (old buggy config — COMPLETED 2026-03-12)
- Run ID: bt_20260312T024150Z_cd4067b6
- Status: COMPLETE — summary confirmed at C:\Argus\repo\ops\logs\bt_summary_bt_20260312T024150Z_cd4067b6.json
- Dataset: 43,172 candles, ETH-USD, 2026-02-09 to 2026-03-11 (30 days)
- Win rate: 25.35%
- Profit factor: 0.7412
- Entries filled: 79 / Trades closed: 71
- PnL: -$5.045 (start $500 → end $494.96)
- Max drawdown: 1.19% ($5.96)
- Avg trade: -$0.045
- Avg win: $0.504 / Avg loss: -$0.231
- Entry blocks: 6067 MISSED_BUY_NO_CASH, 4 MISSED_BUY_COOLDOWN
- Config: CONFLUENCE_MIN_SCORE=72, CONFLUENCE_SIZE_MULT_WHEN_NOT_TRADE=0
- Bugs present: liq filter unit mismatch (ETH vs USD floor), PENALIZE mode penalty not applied

## Analysis from partial run (first half, Feb 9-23, 21 closed trades):
- Win rate: 23.8% (5W/16L), Total PnL: -$2.79
- Feb 12-14 recovery: 40% WR, +$0.75 (alpha pocket)
- All score=72 entries are losses (-$2.17 total)
- 3 thin-vol entries (vol < vbase*0.5) are losses (-$0.62) — blocked by liq fix

## Confirmed Bugs Fixed (code changes in place, NOT yet in baseline run):
1. engine.py: _apply_liquidity_overlay_to_confluence() — PENALIZE mode now applies penalty before ok check
2. .env: LIQ_MIN_VOL_USD_1M=0 (disabled), LIQ_MIN_VOL_UNITS_1M=25 (ETH floor)
3. confluence.py: tl_penalty_resist_slope_neg knob added to _apply_trendline_overlay()
4. config.py: TL_PENALTY_RESIST_SLOPE_NEG canonical key added

## Comparison Tests — STATUS: NOT YET RUN (blocked 2026-03-12 overnight)
Autonomous session could not execute Python (Bash tool security restriction).
Tests need to be run manually.

### Test 1: fixed_liq (USE_TRENDLINES=false, TL_PENALTY=0)
- Status: NOT RUN
- Command: cd C:\Argus\repo && USE_TRENDLINES=false TL_PENALTY_RESIST_SLOPE_NEG=0 C:\Argus\.venv\Scripts\python.exe -m backtest.runner
- Expected: blocks thin-vol entries, liq penalty now applied; no trendline changes
- Win rate target: >= 35%, PF target: >= 1.20

### Test 2: trendlines (USE_TRENDLINES=true, TL_PENALTY=5)
- Status: NOT RUN
- Command: cd C:\Argus\repo && USE_TRENDLINES=true TL_PENALTY_RESIST_SLOPE_NEG=5 C:\Argus\.venv\Scripts\python.exe -m backtest.runner
- Expected: additionally blocks score=72-76 entries in descending channels
- Win rate target: >= 35%, PF target: >= 1.20

## Exit Criteria for Phase 16.5 PASS
- WR >= 35% AND PF >= 1.20 on 30-day dataset
- Entry count not catastrophically reduced vs fixed_liq (>= 80% of fixed_liq entries)
- Feb 22-28 recovery period trades not significantly reduced

## Key Score Analysis (TL_PENALTY_RESIST_SLOPE_NEG=5 impact):
- The TL penalty fires on confluence score BEFORE session bonus is added
- For score=72 entries with sb=+8 (overlap): (adj-5)+8 still < 72 → blocked
- For score=72 entries with sb=0 (asian): adj-5 → still < 72 → blocked
- Scores >= 77 remain TRADE even with -5 penalty (not blocked)
- Feb 13 entry (score=70, sb=+3, +$0.84 win): resist_slope was ASCENDING → NOT blocked → win preserved

## Script for both tests (sequentially):
ops\run_comparison_tests.ps1

## Next Steps
1. Run comparison tests (Test 1, then Test 2) via run_comparison_tests.ps1 or individually
2. Compare WR and PF to baseline
3. If trendlines test meets WR>=35% and PF>=1.20: update .env USE_TRENDLINES=true
4. If not: try TL_PENALTY_RESIST_SLOPE_NEG=3, or keep USE_TRENDLINES=false
