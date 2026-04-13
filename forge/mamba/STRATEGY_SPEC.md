# Mamba — NAS100 5-Min Trendline Breakout Strategy

**Named after:** MambaFX (@mambafx)
**Instrument:** NAS100 (MNQ micro Nasdaq futures via IBKR)
**Timeframe:** 5-minute primary, 1-min for entry refinement
**Style:** Intraday breakout scalper with tight stops and wide targets

## The Edge

Price respects trendlines on 5-min NAS100 because:
1. Retail traders draw the same lines → stop clusters form at obvious levels
2. Algos detect and front-run these clusters
3. When the trendline breaks, the stop cascade creates a 1:4+ momentum move
4. More touches on the trendline = more stops clustered = bigger breakout

## Entry Rules

1. **Identify trendline:** Connect 3+ swing highs (descending) or swing lows (ascending) on 5-min chart
2. **Count touches:** Each time price tests and respects the trendline = 1 touch
   - 3-4 touches: MINIMUM for a valid setup
   - 5-6 touches: GOOD setup
   - 7+ touches: HIGH CONVICTION (like the 9-touch example)
3. **Wait for breakout:** Candle CLOSES beyond the trendline (not just a wick)
4. **Volume confirmation:** Breakout candle volume > 1.5x average of last 20 bars
5. **Entry:** Market order on the close of the breakout candle, or limit order at trendline retest

## Stop Loss

- Place stop just beyond the last swing point before breakout
- Typically 0.10-0.20% from entry on NAS100 (15-40 points)
- TIGHT — this is what creates the asymmetric R:R

## Take Profit

- Minimum 1:4 risk/reward ratio (if stop is 20 pts, target is 80 pts minimum)
- Scale out: 50% at 1:3, remainder at 1:5 with trailing stop
- Or: trail stop to breakeven at 1:2, let the rest run

## Filters

- **Session filter:** Only trade during high-volume sessions:
  - London open: 03:00-05:00 ET
  - NY open: 09:30-11:30 ET
  - London/NY overlap: 08:00-11:00 ET (BEST)
- **Avoid:** First 5 min of session (spread widening), last 30 min before close
- **No trading:** During FOMC/CPI/NFP releases (Atlas calendar gate)
- **Trend direction:** On 1H chart, prefer breakouts in the direction of the higher timeframe trend

## Sizing

- Base: 1 MNQ contract per $5,000 equity
- Conviction scoring:
  - 3-4 touches: 1x size
  - 5-6 touches: 1.25x size
  - 7+ touches: 1.5x size
  - Breakout WITH Atlas regime alignment: +0.25x
  - Breakout AGAINST Atlas regime: -0.25x

## Expected Performance (from MambaFX examples)

- Win rate: 35-45% (most breakouts fail, but winners are 4-5x losers)
- Average winner: 0.8-1.5% on NAS100
- Average loser: 0.15-0.25% on NAS100
- Profit factor: 2.0-3.5
- Trades per day: 2-5 setups, 1-3 taken (filtered)
- Monthly trades: 20-40

## Technical Implementation

### Trendline Detection Algorithm
1. Identify swing highs/lows using pivot point detection (N bars left, N bars right)
2. For descending trendlines: connect the two most recent swing highs
3. Extend the line forward
4. Count how many subsequent swing highs touch the line (within ATR*0.2 tolerance)
5. For ascending trendlines: same with swing lows

### Breakout Detection
1. Current 5-min candle closes beyond the trendline by > ATR*0.1
2. Volume of breakout candle > 1.5x 20-bar average volume
3. The trendline has been active for at least 30 minutes (6+ bars)
4. No breakout signal if price already moved >50% of expected target distance

### IBKR Integration
- Contract: MNQ (Micro E-mini Nasdaq-100 Futures)
- Exchange: CME/GLOBEX
- Client ID: 102 (forge reserved range 100-199)
- Data: 5-min real-time bars via reqHistoricalData or reqRealTimeBars
- Orders: bracket order (entry + stop + target)
