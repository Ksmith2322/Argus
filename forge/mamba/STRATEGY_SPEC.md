# Mamba — Index Futures Breakout Scalping System

**Named after:** MambaFX (Anthony Alvarenga, @mambafx)
**Instruments:** US30 (MYM micro Dow) + NAS100 (MNQ micro Nasdaq)
**Timeframes:** 5-min for bias/levels, 1-min for entry
**Style:** Pure price-action breakout scalping at NY open
**Session:** NY only — 9:25-10:30 AM EST (hard cutoff)
**Max trades:** 1-2 per day, or zero

## The Method (from MambaFX's rulebook)

### 1. Prep (5-min chart, 9:20-9:25 AM EST)
- Mark horizontal support/resistance (more touches = stronger)
- Draw trendlines where price respects them
- Determine bias:
  - Bullish: price respecting support + higher highs/lows
  - Bearish: price respecting resistance + lower highs/lows
- If price is choppy/consolidating with no clear bias → NO TRADE today

### 2. Wait for Volume (9:30 AM EST)
- Volume must pick up at open
- Don't enter pre-open
- If no momentum by 9:35 → keep waiting

### 3. Entry (switch to 1-min chart)
- **Bullish:** Break above resistance/trendline + bullish 1-min structure (higher high → higher low → breakout candle close)
- **Bearish:** Break below support/trendline + bearish 1-min structure (lower low → lower high → breakdown candle close)
- Multiple confluences = higher probability (trendline break + S/R break + structure shift)
- Enter on breakout candle close (confirmed) or aggressively on the break

### 4. Risk Management
- **Stop:** Tight — just beyond the broken level or recent swing (10-20 points on indices)
- **Target:** 1:3 to 1:5 R:R (he typically aims 1:4 to 1:5)
- **Hold time:** 10-30 minutes max. "20 minutes, we're out."
- **Scale out** at key zones or trail stops on strong moves
- **Cut losses fast** if no follow-through after breakout

### 5. Hard Rules (Non-Negotiable)
- Max 1-2 trades per day
- NY session only (9:25-10:30 AM EST)
- If no setup by ~10:30 AM → done for the day
- Never trade pre-open, low-volume, or choppy conditions
- Never hold through reversals
- Never overtrade or extend session
- No indicators — pure price action

## What Changed From Our V1 Build

| V1 (wrong) | V2 (correct per rulebook) |
|------------|--------------------------|
| 5-min for everything | 5-min bias + 1-min entry |
| London + NY + overlap sessions | **NY open only: 9:25-10:30 AM EST** |
| Many trades per day | **Max 1-2, often zero** |
| 1:4 fixed target | **1:3 to 1:5, scale out** |
| Trendline breaks only | **S/R + trendlines + structure on 1-min** |
| Any time of day | **First 45-60 min after open only** |
| Volume > 1.5x filter | **Volume conviction at open (qualitative)** |
| Hold until target or stop | **10-30 min max hold, manual exit on fade** |

## Instruments

**Primary:** Both US30 (YM/MYM) and NAS100 (NQ/MNQ) — they correlate but offer independent setups.

Via IBKR:
- MNQ: Micro E-mini Nasdaq-100, $2/point, CME/GLOBEX, client ID 102
- MYM: Micro E-mini Dow, $0.50/point, CME/GLOBEX, client ID 103

## Algorithm Translation

### Bias Detection (5-min, computed at 9:25 AM EST)
1. Identify swing highs/lows from overnight session (last 12 hours)
2. Fit support/resistance levels (horizontal clusters where price reversed 2+ times)
3. Fit trendlines through swing points
4. Classify bias:
   - BULLISH: 2+ higher lows AND price above key support
   - BEARISH: 2+ lower highs AND price below key resistance
   - NEUTRAL/CHOPPY: no clear structure → skip day

### Entry Detection (1-min, 9:30-10:30 AM EST)
1. After open, monitor 1-min bars for structure shift confirming bias
2. For BULLISH bias: wait for 1-min higher high → higher low → breakout candle closing above resistance/trendline
3. For BEARISH bias: wait for 1-min lower low → lower high → breakdown candle closing below support/trendline
4. Breakout candle should show conviction (large body, small wicks, above-average volume)
5. Multiple confluences boost conviction score

### Position Management
1. Stop: below the 1-min swing low (bullish) or above swing high (bearish)
2. Target: stop distance × 3 minimum, × 4-5 preferred
3. Trail stop to breakeven at 1:2
4. Close 50% at 1:3, let rest run to 1:5 with trailing stop
5. Hard time stop: close everything at 10:30 AM EST regardless
6. Max 2 entries per day across both instruments

## Sizing
- Base: 1% equity risk per trade
- Touch count multiplier: 3-4 touches = 1.0x, 5+ = 1.25x
- Conviction scorer integration: 0.25x to 2.0x from fleet conductor
- Max risk: 3% equity per day (if both trades taken)

## Expected Performance (realistic)
- Win rate: 35-45% (tight stops mean many small losses)
- Average winner: 1:3 to 1:5 R:R
- Profit factor: 2.0-3.0
- Trades per week: 3-8 (many skip days)
- Monthly return target: 5-10% on deployed capital
