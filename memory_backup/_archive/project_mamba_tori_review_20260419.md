---
name: Mamba & Tori backtest review (2026-04-19, expanded 2026-04-19+)
description: Local backtest breakdowns + subset edges + bridge plans + transcript-derived rules for both strategies
type: project
originSessionId: 468782c5-c83c-4fc9-8b1b-60051a2f4e99
---
## Current backtest numbers (authoritative)

**Mamba** — `forge/logs/mamba/backtest_trades.csv` (29 trades)
| Bucket | Trades | WR | PnL$ | PF |
|---|--:|--:|--:|--:|
| NQ  | 14 | 14.3% | -190.80 | 0.62 |
| YM  | 15 | 33.3% | +117.22 | **1.98** |
| All | 29 | 24.1% |  -73.58 | 0.88 |

**Tori** — `forge/logs/tori/backtest_trades.csv` (65 trades)
By setup × grade:
| Bucket | Trades | WR | PnL$ | PF |
|---|--:|--:|--:|--:|
| Bounce, A     |  5 | 20.0% | +810.01 | **3.56** |
| Break, A      | 44 | 31.8% | -1355.00| 0.45 |
| Break, A+     | 12 | 33.3% |  -483.51| 0.44 |
| Break Retest  |  4 |  0.0% |  -642.56| 0.00 |

By instrument:
| Instrument | Trades | PF | PnL$ |
|---|--:|--:|--:|
| Dow      | 22 | **1.85** | +834.90 |
| Gold     | 10 | 0.65 | -176.58 |
| Platinum | 13 | 0.29 | -826.29 |
| Crude Oil| 20 | 0.08 | -1503.09|

## Tori exit-logic audit (code verified)

The bot's `_find_opposing_safety` at [forge/tori/trendlines.py:414-434](forge/tori/trendlines.py) returns `recent swing high + 0.3*ATR` (or low) — a **flat horizontal level**, not an angled trend line. The bot's 120-bar max_hold at [forge/tori/runner.py:244](forge/tori/runner.py) fires only once in 65 trades; the swing-based trail does almost all the exiting.

CSV r-multiple distribution:
```
-1 to -0.5   : 44 (losers at stop)
 0 to 1      :  3
 1 to 2      : 15 (winners cluster here)
 2 to 3      :  0
 3 to 5      :  0
 >5          :  1 (Bounce-A outlier at 11.64R)
```

Winners capped at ~2R almost entirely. Sum R = **-15.24**. Tori's method requires the occasional 5-10R runner to be profitable. The swing-based trail pulls the stop in too tight, converting what should be a right-tailed distribution into a -1R/+1.5R roulette wheel. **This is the root cause of the break-setup losses.**

## Transcript-derived mechanical rules

### Tori (9 transcripts synthesized)
- **Primary setup**: 3-touch-point trend line break (her "A+"). 2-touch acceptable on low TFs but lower-grade.
- **Grading**: `touch_points_score + data_span_score − invalidation_penalty`. ≥9 = A+, 7-8 = A, 6 = acceptable, ≤3 = delete.
- **Entry**: on the break, **not** on candle close. (Live stream: *"I do not wait for the candle to close."*)
- **Exit confirmation**: wait for candle close only when EXITING and uncertain (fakeout check).
- **Safety line at entry**: opposing trend line (angled), fixed at entry, never moved during trade.
- **Trailing stop during trade**: NEW steeper trend line drawn along post-entry higher-lows/lower-highs. Point A = previous touch; Point B = most recent post-entry swing. As new swings form, line updates, stop drags with it.
- **No take profit**: ride until trend-line stop fires, or HTF trend line hits, or S/R level hits — whichever first (confluence exit).
- **No intersection rule**: wicks included. If price has poked through the line, it's invalid.
- **Top-down analysis**: monthly → weekly → daily → 4H (her TF). Lower TF traders go further. Step down only when no more non-intersecting lines fit current TF.
- **Lines connect across TFs**: new TF lines must anchor to points from higher-TF lines.
- **Lifecycle**: maintain over regenerate. Adjust Point B on closed candles with clear pullbacks. Delete only when line becomes horizontal or inverted.
- **Don't adjust lines during a trade.** Entry attribution stays fixed.
- **Time frame is context-dependent**: 4H for personal capital, 1H as intermediate, 5-minute for Apex prop firm (forces day-trading).
- **Setup inventory**: 3-touch break (A+), 2-touch break, break+retest, bounce, 2-touch bounce.

### Mamba (5 transcripts synthesized)
- **Instruments**: NQ + YM only. No gold / forex / crypto.
- **Session gate**: setup at 6:20 AM PST, execute at 6:30 AM PST (= 9:30 AM ET / NY open).
- **Time frames**: 5m primary, 15m in choppy conditions, 1m for break confirmation. Never 30m+.
- **Pattern library** (across his videos — internally inconsistent, unified here):
  - **3-confirmation stack**: rejection wick + trend-line break + S/R break (all three aligned = high conviction)
  - **Two-candle engulfing**: bullish candle swallowed by 2 bearish whose combined range ≥ bullish candle size (or reverse) + S/R break
  - **Double-break**: trend line + S/R at same level
  - **Regime indicator**: "Trend Indicator A" on TradingView (unverified which author; substitute: SuperTrend / fast-slow EMA / MACD-histogram-sign)
- **S/R validity**: 3-4+ touches minimum. 5-9 touches preferred.
- **Entry**: aggressive, on the push through — not on candle close.
- **Stop**: structure-based, just beyond the broken level (wick or swing). NOT ATR-based.
- **Tiered sizing**:
  - Normal breakout: 2% risk
  - 3-confirmation stack: 3-5% risk
- **R:R targets (internally inconsistent across videos)**: 1:2 / 1:3 / 1:4 / 1:5. Use **1:3** as pragmatic mid-point.
- **Max 2-3 trades per day.** If faked out, take the loss, wait, re-enter.
- **Fake-out rule**: don't try to predict. Let them happen; react.
- **CSV timing anomaly**: existing backtest shows entries at 09:50-09:55 ET, his stated window is 09:30 ET. Our bot is 20-25 min late.

## Honest-dashboard constraints (the guardrails)

For both strategies, the transcript-derived rules land us with much smaller effective sample sizes than the current unfiltered backtest. A faithful Mamba restricted to YM-only + 3-confirmation + NY open gets **roughly 15 of the 29 existing trades**. A faithful Tori restricted to Bounce-A + Dow-only gets **~5 of 65**. Neither survives the 10-trade sanity bar for honest confidence.

Implication: even with perfect implementations, **both artifacts' `p_expectancy_positive` should be `None` post-filter**, with sample_warning explaining the narrow-scope rebuild. That is honest, not a regression.

## Sources
- Tori public docs: [ChartFanatics Tori playbook](https://www.chartfanatics.com/strategies/trendline-strategy), [FX Replay Tori strategy](https://fxreplay.com/strategies/tori-trades-trendlines-strategy)
- Mamba: [thekingdm.com](https://www.thekingdm.com/), [mambacourses.com](https://mambacourses.com/)
- 14 transcripts (9 Tori, 5 Mamba) reviewed across this session — see project_strategy_playbooks_20260419.md for full synthesis

**Why:** Consolidated 2026-04-19 after reading actual backtest CSVs + 14 primary-source transcripts + auditing the Tori exit logic in code.
**How to apply:** Treat Mamba/Tori confidence as SIGNAL_ONLY until their own priority tests pass. Don't tune existing knobs; narrow scope to the positive subset first, then expand. See `project_strategy_playbooks_20260419.md` for the other 5 traders reviewed (Cue Banks, Trading Geek, Jooviers Gems, Craig Peroco).

---

## Tori 2-year validation update (2026-04-19 evening)

Ran Tori backtest on 3 independent windows to test whether the 6-month
headline (PF 1.80 with 88% outlier concentration) was the real picture or
a small-sample artifact. It was the latter:

| Window | Trades | WR | PF | Top-1 % of PnL |
|---|---:|---:|---:|---:|
| 6 months | 90 | 34% | 1.80 | **88%** |
| 12 months | 253 | 50% | 1.95 | 15% |
| 2 years | 677 | **51%** | **2.19** | **10%** |

**2-year data is now the canonical backtest.** Artifact shows
`p_expectancy_positive = 1.000`, `ruin_fraction = 0.0`, outlier
concentration falls to 10% (healthy). Strategy PnL = +$200,083 over 2
years with 677 trades.

### Per-setup at scale (the real distribution)
- **break_A+**: 114 trades, 57% WR, **PF 3.02** — her true A+ setup is
  genuinely strongest, matching her teaching
- **break_A**: 550 trades, 50% WR, **PF 2.06** — bread-and-butter holds
- **break_retest_A**: 5 trades, PF 5.67 (small sample)
- **bounce_A**: 8 trades, 12% WR, **PF 0.01** — **the 6-month bounce_A
  outperformance was false**. Bounce needs different filtering or is
  genuinely not her edge in our implementation.

### Dashboard impact
Tori: 6-month artifact at 84% → 2-year artifact at **100%**. Fleet
confidence: 25% → 50%. This is no longer "hopeful" confidence; it is
quantified conviction on 677 trades with distributed profitability and
zero ruin fraction.

### Honest next steps
1. **Validated — treat Tori as proven at backtest level.** Forward-paper
   trading is the next gate. Live fills will differ from backtest fills.
2. **bounce_A puzzle**: her teaching says Bounce is valid; our backtest
   says it's not. Either our bounce detector is wrong, her teaching
   overstates, or both. Research item — not urgent.
3. **Mamba and Cue Banks do NOT have this story.** Their MC stress
   showed 91% and 96% ruin fractions. Don't assume the Tori lift
   transfers.
