---
name: Strategy playbooks — 7 traders / 18 transcripts (2026-04-19)
description: Consolidated rulesets per trader, distilled from primary-source transcripts. Use as single source of truth for writers and v2 rebuilds.
type: project
originSessionId: 468782c5-c83c-4fc9-8b1b-60051a2f4e99
---
Consolidated ruleset for every trader reviewed in this session. Each section is what a faithful bot for that trader should enforce.

## 1. Tori Trades (9 transcripts)
**Style**: pure trend-line breakout, multi-TF, no indicators.
- **Instruments**: futures (gold, platinum, silver, copper, crude). Personal broker: TradeStation.
- **Time frames**: 4H for personal account, 1H as intermediate, **5-minute for Apex prop-firm compliance** (no overnight holds).
- **Setups** (ranked):
  1. **3-touch trend-line break (A+)** — her bread-and-butter
  2. **2-touch trend-line break** — acceptable on low TFs only
  3. **Break and retest** — price breaks action line, touches back from the other side
  4. **Bounce** — price respects the line, bounces off (action = bounce, safety = same line)
- **A+ grading formula (derived from her rating video):**
  ```
  grade = touch_points_score + data_span_score − invalidation_penalty
  touch_points_score: 3+ = 5pts, 2 = 3pts, 1 = 0
  data_span_score: wide bar range = 5pts, narrow = 2pts
  invalidation_penalty: -1 per recent cross
  A+ ≥ 9, A = 7-8, acceptable = 6, delete ≤ 3
  ```
- **Line-drawing rules**:
  - Draw from **wicks**, not bodies
  - Price cannot intersect the line (wicks included)
  - Top-down: monthly → weekly → daily → 4H → lower TF
  - All lines must connect across TFs
  - Don't place lines on open candles
- **Line lifecycle**: maintain over regenerate. Adjust Point B on closed candles with clear pullbacks. Delete if line becomes horizontal or inverted. **Don't adjust lines during a trade.**
- **Entry**: on the break, not on candle close.
- **Safety line (stop) at entry**: opposing trend line (angled), fixed for life of trade.
- **Trail during trade**: NEW steeper trend line off post-entry higher-lows/lower-highs. Replaces the original safety as the active stop.
- **Exit**: three candidates in confluence — current safety break OR HTF trend-line contact OR S/R level contact. Whichever fires first.
- **Exit confirmation**: wait for candle close only if uncertain (fakeout check).
- **No take profit**: ride the trend.
- **Risk per trade**: 1-3% for beginners; she risks 4-7% on conviction setups.
- **One attempt per trend line**: once a line has been traded and failed, don't re-use it.

## 2. Mamba FX (5 transcripts — internally inconsistent, unified here)
**Style**: NY-open breakout scalper, S/R and trend-line fusion.
- **Instruments**: NQ + YM only. No gold / forex / crypto.
- **Session gate**: setup 6:20 AM PST, execute 6:30 AM PST (= 9:30 AM ET / NY open).
- **Time frames**: 5m primary, 15m in chop, 1m for confirmation. Never 30m+.
- **3-confirmation stack (the unified entry rule)**:
  1. Rejection wick — a candle that fails to break a level, showing failed reversal
  2. Trend-line break — structural shift in direction
  3. S/R break — level-based confirmation
  All three aligned = high-conviction trade.
- **Two-candle engulfing pattern**: bullish candle swallowed by 2 bearish whose combined range ≥ the bullish candle (or reverse). Paired with S/R break.
- **Regime filter**: "Trend Indicator A" on TradingView (exact author unspecified). Substitute candidates for implementation: SuperTrend / fast-slow EMA cross / MACD-histogram-sign.
- **S/R validity**: 3-4+ touches minimum.
- **Entry**: aggressive, on the push through. NOT on candle close.
- **Stop**: structure-based, just beyond the broken level (wick or swing). NOT ATR.
- **Tiered sizing**:
  - Normal breakout: 2% risk
  - 3-confirmation stack: 3-5% risk
- **R:R target**: his videos vary 1:2 / 1:3 / 1:4 / 1:5. Use **1:3** as mid-point for testing.
- **Max 2-3 trades per day.**
- **Fake-out rule**: don't predict. React. If faked out, take the loss and wait for next setup.
- **Real 1-minute data is mandatory.** The bot's synthetic 1m (resampled from 5m) invalidates any confidence number.

## 3. Cue Banks (2 transcripts)
**Style**: break + retest + confluence stack, multi-TF, limit-order execution.
- **Core rule (repeated 10+ times)**: **"No retest, no entry."** Price must break, then touch back from the other side before entry.
- **Candle-closure validation**:
  - Half-close over level + next candle rejects = **fake break** (skip)
  - Full body close over + next candle forms past level = **valid break**
- **Tool set** (only these four):
  1. Support/resistance drawn from **wicks**
  2. Supply/demand zones: previous wick → breakout candle body
  3. Fibonacci PRZ: 38.2 / 61.8 / 78.6 / 88.6
  4. Harmonic patterns (bullish bat: X→A leg, A→B at 50%, B→C at 78.6%, C→D at 88.6% of X→A)
- **Confluence**: entry requires 2-3 confirmations stacked (e.g., support + uptrend + retest)
- **Order type**: pre-placed buy/sell limits (reversal) or buy/sell stops (continuation) 1 pip past the level. Prefers ECN brokers for tight spreads.
- **Time frames**: analysis on 4H/1H/daily; execution on 5-minute.
- **Dashboard bot gap**: current "Confluence + Fib" implementation likely missing retest enforcement, candle-closure validation, harmonic patterns, supply/demand zones.

## 4. Trading Geek (1 transcript)
**Style**: SMC/ICT probability-tiered framework.
- **Vocabulary**:
  - **BOS** = break of structure (swing high/low)
  - **MSS** = market shift, internal structure reversal
  - **Swing range** = swing low → swing high
  - **Internal structure** = everything between
  - **POI** = point of interest (supply/demand/FVG/liquidity zone)
- **Three-tier probability ranking**:
  - **A+ (high)**: pro-internal orderflow + high-quality POI + liquidity sweep fires
  - **Medium**: counter-internal but at extreme/unmitigated POI
  - **Low**: counter-internal, no special context — **avoid**
- **Entry gates (all must be true)**:
  1. Pro-internal orderflow
  2. POI is unmitigated / extreme / has imbalance / near liquidity
  3. Liquidity sweep happens before entry (stop-hunt confirmation)
  4. Aligned with HTF structure
- **Premium/discount**: discount (lower half of swing range) = bias long; premium (upper half) = bias short.
- **Not in current fleet.** Would require new `forge/smc/` module if pursued.

## 5. Jooviers Gems — Strategy A: HA Doji Scalp (1 transcript)
**Style**: 1-minute Heikin Ashi scalping.
- **Chart**: Heikin Ashi candles + 100 EMA
- **Time frame**: 1-minute only
- **Bias**: above 100 EMA = buys only; below = sells only
- **Pullback requirement**: 2+ opposite-direction HA candles with no wicks on the "wrong" side
- **Entry trigger**: high-volume doji (body ≥ 2 prior candles in size)
- **Entry timing**: at close of doji
- **Stop**: below doji wick (buy) / above doji wick (sell)
- **Target**: fixed 1:1 R:R
- **Time window**: 10 AM – 12 PM ET
- **CRITICAL TRAP**: HA close is NOT a tradable market price (it's a computed average). Any backtest using HA close as entry price inflates results. Faithful backtest must use HA for signals and **real OHLC** for execution.
- **Not in current fleet.**

## 6. Jooviers Gems — Strategy B: Break-and-Retest (1 transcript)
**Style**: Multi-TF break and retest (essentially Cue Banks with different branding).
- **Zone identification**: draw S/R zone on 1H or 4H from top of wick to body of highest candle
- **Confirmation**: body closes outside the zone on the SAME TF you drew it on
- **Execution**: drop to 1-minute chart, wait for price to retest broken zone, then form either consolidation / HH-LL / trend line
- **Entry**: on break of secondary structure
- **Stop**: below recent swing low (or above swing high)
- **Target**: next major high/low on 1H/4H
- **Claimed stats**: 36% win rate, 2.97:1 avg R:R, 36% monthly return (more plausible than her HA strategy's 75% WR claim).
- **Overlap with Cue Banks**: ~90%. If Cue Banks is built faithfully, it covers this too.

## 7. Craig Peroco (1 transcript)
**Style**: Fair value gap (FVG) + trend level, continuation + reversal models.
- **Fair Value Gap**: 3-candle pattern where candle 1's wick high and candle 3's wick low don't overlap. Creates a "gap" that price tends to fill.
- **Two models**:
  - **Continuation**: downward trend breaks + FVG produced → enter on FVG retest + trend-level contact, trail along new FVGs
  - **Reversal**: trend level broken + FVG intersecting → enter at FVG midpoint, stop above FVG candle, 1:4 target
- **Session gate**: New York open (same as Mamba)
- **Trailing stop**: moves to each new FVG produced in trade direction
- **Risk per trade**: $100-500, targeting 3-5R+ wins
- **Not in current fleet.** Shares FVG concept with Trading Geek.

---

## Overlap matrix

| Mechanic | Tori | Mamba | Cue Banks | Trading Geek | Jooviers A | Jooviers B | Craig |
|---|---|---|---|---|---|---|---|
| Trend lines | ✓✓✓ | ✓✓ | ✓ | — | — | — | ✓ |
| S/R break | — | ✓✓✓ | ✓✓ | ✓ | — | ✓✓✓ | — |
| Break + retest | subtype | — | ✓✓✓ | — | — | ✓✓✓ | — |
| Candle close validation | conditional | no | ✓ | — | ✓ | ✓ | — |
| Retest required | optional | no | **required** | — | — | **required** | **required** |
| Multi-TF | ✓✓ | — | ✓✓ | ✓✓ | — | ✓✓ | ✓ |
| Fib / harmonics | — | — | ✓✓ | — | — | — | — |
| FVG | — | — | — | ✓ | — | — | ✓✓✓ |
| SMC vocabulary | — | — | — | ✓✓✓ | — | — | ✓ |
| Heikin Ashi | — | — | — | — | ✓✓✓ | — | — |
| NY open session gate | — | ✓✓✓ | — | — | mid-day | — | ✓ |
| Fixed take profit | no | 1:2–1:5 | variable | no | 1:1 | 1:3 avg | 1:4 reversal / no-TP continuation |

## Confidence scoring invariants (applied to all writers)

Regardless of trader, every artifact must respect:
1. `p_expectancy_positive == null` when `n_total < 10`
2. `p_expectancy_positive == null` when `source` contains "hardcoded_estimate"
3. `bt_pf` must be a scalar float, not a range
4. `n_live + n_paper == n_total`
5. When n_live = 0 and n_paper > 0, `sample_warning` must flag "historical only"

**Why:** Consolidated 2026-04-19 from 18 transcripts spanning 7 traders. Two of the traders (Tori, Mamba) have bots with audited gaps documented in `project_mamba_tori_review_20260419.md`. Three have rulebook files in `forge/<strategy>/`. Two are not in the current fleet.

**How to apply:** When the user asks about any of these strategies, this is the rulebook. Do NOT invent rules from training data — cite this document. When building a writer or v2 for a strategy, the rules here define the contract.
