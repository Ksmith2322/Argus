# MambaFX Complete Rulebook

**Source:** Anthony Alvarenga (@mambafx) — 671K YouTube subscribers
**Compiled from:** "The ONLY Trading Strategy You Need for 2026," "This Strategy Makes You Profitable INSTANTLY," "The Only 1-Minute Scalping Strategy You'll EVER NEED," "This NEW 2026 Futures Strategy is INSANE"
**Last updated:** 2026-04-19 — consolidated from 5 transcripts including regime-indicator video, candle-pattern video, 6-step breakout video, and Legacy prop-firm walkthrough.

**Primary consolidated reference:** `memory/project_strategy_playbooks_20260419.md` §2 (Mamba FX).
**Audit / subset-edge findings:** `memory/project_mamba_tori_review_20260419.md`.

---

## The One-Sentence Strategy

"Mark up support, resistance, trend lines at 6:25 on the 5-minute... once we see that breakout occur with volume on the 1-minute + candle structure... take the trade with tight stop and 1:3-1:5 target."

---

## Markets
- **Only:** US30 (YM) and NAS100 (NQ)
- They correlate — pick the cleaner setup or trade both
- Futures preferred: zero spread, real-time data, prop-firm friendly

## Session
- **Only:** New York session
- **6:25 AM PST (9:25 EST):** Start marking charts
- **6:30 AM PST (9:30 EST):** Volume kicks in at open
- **Trade window:** First 45-60 minutes after open
- **Hard cutoff:** ~10:30-11:00 AM EST — done for the day
- **Max:** 1-2 trades. Many days = 0.

## Timeframes
- **5-min (or 15-min):** Direction, bias, S/R levels, trendlines
- **1-min:** Entry, market structure, breakout confirmation
- **Nothing else.** "I'm not looking at the 30. I'm not looking at the hourly."

---

## Chart Marking (6:25 AM on 5-min)

1. Draw horizontal **support** — 2-4+ clear touches = strong
2. Draw horizontal **resistance** — same, especially big wick rejections
3. Draw **every trendline** price respects — "they all matter... 1 2 3 4 5 6 whatever. 10 touches"
4. Sometimes box consolidation zones with rectangle tool
5. Identify BOTH bullish and bearish sides — ready for either direction
6. If choppy/consolidating pre-open → likely no trade

## Bias Determination (5-min)

**Bullish:**
- Strong support with multiple touches + price pushing up
- Higher highs / higher lows forming
- Exhaustion at lows (big wicks down then rejection)
- "Sellers are getting tired... we now have buying power"

**Bearish:**
- Strong resistance with repeated rejections (big wicks)
- Lower highs / lower lows forming
- Price failing to break higher
- "Huge wick, came right back down... price is bearish"

## Entry (1-min chart after 9:30 EST)

**Bullish (Long):**
1. Break above resistance or trendline on 1-min
2. Bullish structure: higher high → higher low → higher high
3. Strong bullish closing candle + volume spike
4. Enter on breakout (aggressive) or candle close (safe)

**Bearish (Short):**
1. Break below support or trendline on 1-min
2. Bearish structure: lower low → lower high → lower low
3. Strong bearish candle + volume
4. Enter on breakdown (aggressive) or candle close (safe)

**Confluences = sniper setup:**
- Trendline break + S/R break + structure shift = "three confirmations... takes you from 50% to 80%"
- One break = OK. Two or three = high probability.

**Candle reading:**
- Big wicks = exhaustion/rejection (key for bias)
- Strong impulse candles with volume = conviction for entries
- "I'm able to read candle patterns... huge wick... sellers took back over"

**Retests:**
- Price often retests the broken level after breakout
- Can add to position or use as confirmation

## Risk Management

**Stop Loss:**
- Very tight — just beyond the broken level, recent swing, or wick
- Example: "Nice tight stop loss. 14 points."
- Typically 10-20 points on indices

**Take Profit:**
- Primary: 1:3, 1:4, or 1:5 R:R (he styles 1:4-1:5)
- Scale or trail on strong moves: "could have trailed... went for a 1:8"
- Use next zones/previous highs/lows for exits
- "When the market hits my zone and starts to react... I'm going to get out"

**Time:**
- Max hold: 10-30 minutes. "20 minutes, we're out."
- Exit within the NY open window

**Why high R:R works:**
- "Even if you just took 1:3s... you can lose six trades in a row and still be break even"
- One or two big winners cover many small losses

## Hard Rules

### DO:
- Mark charts at 6:25 AM PST every day
- Wait for volume at open
- Use multiple confirmations
- Check both US30 and NAS100 — trade the cleaner one
- Practice until robotic ("Watch this video 100,000 times")
- Review every trade daily

### DON'T:
- Force trades or trade outside NY open
- Trade pre-open or low-volume/choppy conditions
- Hold through reversals ("markets are bipolar")
- Overtrade (max 1-2 per day)
- Look at the dollar amount — "look at the chart, the chart tells you things"
- Ignore false breakouts — be ready to flip if structure changes

---

## Algorithm Translation Notes

### Bias Detection (5-min, 9:25 AM EST)
```
1. Scan last 4-6 hours of 5-min bars for swing highs/lows
2. Identify horizontal S/R: price levels with 2+ reversals within 0.1% tolerance
3. Fit trendlines through swing points (ascending + descending)
4. Count touches per level/trendline (more = stronger)
5. Classify:
   - BULLISH: recent HH + HL pattern + price above key support
   - BEARISH: recent LH + LL pattern + price below key resistance
   - NEUTRAL: no clear structure → SKIP DAY
```

### Entry Detection (1-min, 9:30-10:30 AM EST)
```
1. After 9:30, monitor 1-min bars
2. For BULLISH bias:
   a. Detect 1-min HH (swing high exceeds prior swing high)
   b. Then HL (pullback holds above prior swing low)
   c. Then breakout candle: closes above 5-min resistance/trendline
   d. Volume on breakout candle > average
   e. → ENTER LONG
3. For BEARISH bias:
   a. Detect 1-min LL
   b. Then LH
   c. Then breakdown candle: closes below 5-min support/trendline
   d. Volume confirmation
   e. → ENTER SHORT
```

### Confluence Scoring
```
Each confirmation adds to conviction:
  - S/R level break: +1
  - Trendline break: +1
  - 1-min structure shift (HH/HL or LL/LH): +1
  - Volume spike (>2x avg): +1
  - Candle quality (large body, small wicks): +1

  1 confluence = marginal (skip or minimum size)
  2 confluences = good (standard size)
  3+ confluences = sniper (max size, highest conviction)
```

---

## v2 updates from 2026-04-19 transcript consolidation

Five Mamba videos reviewed produced the following **consolidated entry stack** that supersedes the older 5-confluence counter. Treat this as the authoritative rule set for future Mamba bot work.

### The 3-confirmation stack (primary trigger)

All three must align for high-conviction entry:

1. **Rejection wick** on a candle at a level — showing a failed attempt by the opposite side
2. **Trend-line break** in the intended direction
3. **S/R level break** co-located with the trend-line break

Where our current bot requires 5 confluences, the unified Mamba method is **3**, and they are *specific* (wick + trend line + S/R), not *any five of N*.

### Two-candle engulfing alternative trigger

Also valid as a single-pattern trigger (with S/R break):
- Bullish candle pushes up by N points
- Next 2 bearish candles' combined range ≥ N (the bullish candle is swallowed)
- Paired with S/R break in bearish direction → short
- Symmetric logic for longs

### Regime filter (directional bias)

Mamba names "Trend Indicator A" on TradingView. Exact author is unspecified. For implementation, substitute one of:
- SuperTrend
- Fast-slow EMA cross (e.g., 8/18)
- MACD-histogram-sign

Bullish regime = longs only; bearish = shorts only.

### Tiered position sizing (new in 2026-04-19 consolidation)

- **Normal breakout** (single confirmation): 2% risk
- **3-confirmation stack OR two-candle engulfing with S/R**: 3-5% risk

### R:R targets — his videos vary

Stated targets across five videos: 1:2, 1:3, 1:4, 1:5. Use **1:3 as the pragmatic mid-point** for backtesting. Actual live runs can vary.

### Real 1-minute data requirement

The current backtest synthesizes 1-minute bars from 5-minute bars (`forge/mamba/runner.py:120`). **This invalidates any Mamba confidence number**, because the entire edge (wick rejection, sweep-reclaim, structure confirmation) lives in real intrabar behavior that synthetic 1m cannot represent.

Any Mamba v2 must be validated against **real 1-minute data** before its confidence artifact is trustworthy.

### Subset edge (from `forge/logs/mamba/backtest_trades.csv`)

| Ticker | Trades | WR | PnL$ | PF |
|---|--:|--:|--:|--:|
| NQ | 14 | 14.3% | -190.80 | 0.62 |
| YM | 15 | 33.3% | +117.22 | **1.98** |

**YM is the positive subset.** Run YM-only mode; keep NQ in `research_only` until real-1m validation passes.

### Entry timing: stated rule is ambiguous

Two videos show mutually-exclusive rules:
- Older video: *"We closed above. That's what I like to see."* (candle close)
- Newer video: *"As it starts to push below."* (pre-close, aggressive)

Default to **aggressive on-push entry** for the 3-confirmation stack (higher conviction), **candle-close entry** for single-confirmation setups (more confirmation needed).

### Backtest timing anomaly

Our CSV shows entries at 09:50-09:55 ET. His stated window is 09:30 ET. The bot is 20-25 minutes late. Audit the signal-detection logic to find the lag source.
