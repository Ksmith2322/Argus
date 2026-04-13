# Cue Banks Complete Rulebook

**Source:** Cue Banks (Wall Street Academy, @CueBanks)
**Compiled from:** "Master the US30 Market," Confluence Trading 1.0/2.0/2.5 series, Gap Trading Masterclass, live trade breakdowns

---

## The One-Sentence Strategy

"Top-down multi-TF analysis (Daily/H4 bias) → mark S/R, Fibs, exhaustion, and trendlines for confluence clusters → wait for break/pullback confirmation on M5 inside the zone → tight stop outside the level + high RR Fib targets (1:7+) → or sit out."

---

## Markets
- **Primary:** US30 (Dow Jones / YM futures) — this is 90%+ of his trading
- **Secondary:** Gold, major forex pairs (GBP/USD, AUD/USD) — occasional
- **Why US30:** Clean structure, strong trends, psychological round numbers (33000, 34000 etc.), high liquidity

## Timeframes (Multi-TF Confluence)
- **Daily:** Overall direction, major S/R, psychological levels
- **H4:** Go-to for setups and confluence analysis
- **H1:** Confirmation and finer structure
- **M5 (5-min):** Precise entries, stops, execution
- Always start top-down: Daily → H4 → H1 → M5

## Style
- **Confluence trading** — multiple technical factors must align (2-3+ minimum)
- **High R:R scalping/swing** — targets 1:7 to 1:8 risk-reward
- **Gap trading** as a secondary edge
- Patience-driven — many days have NO trade

---

## Core Concept: Confluence

The edge comes from OVERLAPPING technical levels. One level is not enough. He requires 2-3+ of:

1. **Horizontal S/R** (especially psychological round numbers and role-flip levels)
2. **Fibonacci retracement** (38.2%, 61.8% key levels)
3. **Fibonacci extensions** (-27%, -61.8% for targets)
4. **Market structure** (higher highs/lows, lower highs/lows)
5. **Exhaustion patterns** (wicks rejecting, higher lows at bottoms)
6. **Trendlines / counter-trend lines** (breaks confirm bias shift)
7. **Consolidation zone breaks** (breakout from range)

**"Where multiple elements overlap = the trade."**

Example: Fib 61.8% + past support turned resistance + exhaustion candle + structure shift = HIGH PROBABILITY entry

---

## Chart Marking Routine

### On Daily/H4:
1. Mark horizontal S/R (especially psychological levels — round thousands)
2. Draw Fibonacci retracements from recent swing high/low
3. Identify consolidation zones and exhaustion areas
4. Draw trendlines (with-trend AND counter-trend)
5. Note supply/demand areas
6. Look for **confluence clusters** where 3+ elements overlap
7. Mark counter-trend lines over consolidation — break = bias confirmation

### On H1/M5:
1. Refine the confluence zones from H4
2. Watch for structure shifts on M5 that confirm H4 bias
3. Entry on M5 candle close/pullback within the confluence zone

---

## Bias Determination

**Bullish:**
- Higher lows forming on H4
- Exhaustion at lows (big wicks down, rejection)
- Price respecting support
- Fib retracement targets aligning above

**Bearish:**
- Lower highs forming on H4
- Resistance holding (e.g., psychological level like 33,000)
- Counter-trend line break confirming seller control
- Consolidation failing to break upward

**Key rule:** Bias only changes on CLEAR structure break with multiple confirming factors. Sideways consolidation doesn't change bias.

---

## Entry Rules

### Short (Bearish) — His Most Common:
1. Daily/H4 shows resistance holding
2. Price breaks support or consolidation + counter-trend line
3. Pullback forms lower high with bearish pattern (engulfing etc.)
4. Enter on M5 after confirmation in the confluence zone
5. "Entered short around a lower high... based on close and pullback"

### Long (Bullish):
1. Daily/H4 shows support holding or exhaustion at lows
2. Price retraces into Fib levels (38.2% / 61.8%) with higher lows
3. Enter on M5 bounce/confirmation in the confluence zone
4. Often used as temporary longs inside overall sell bias (retracement plays)

### General Entry Filters:
- Must have higher-TF alignment (H4 bias)
- No entry in chop/consolidation without clear break
- Gap setups: trade only after confirming gap is respected or filled with confluence
- Must have 2-3+ confluence factors at the entry level

---

## Risk Management

**Stop Loss:**
- Tight — just outside the consolidation zone, recent swing, or confluence level
- Placed on M5 for minimal risk
- Example: $5K-$7K risk on large accounts

**Take Profit:**
- Primary: Fibonacci targets (38.2% first, then 61.8% or negative extensions -27%, -61.8%)
- **Target 1:7 to 1:8 risk-reward** — one big winner covers many small losses
- Manual exits on "satisfaction" — take profit when happy even if move continues
- Trail or scale on strong momentum

**Risk per trade:** Small fixed dollar amount relative to account. Precision reduces risk naturally.

---

## Gap Trading (Separate Edge)

From his Gap Trading Masterclass:
- Opening gaps act as magnets or rejection zones
- Has a "#1 Gap Trading Rule" around patience
- Gaps are traded only when confirmed with confluence (not blindly)
- Targets: opposite side of gap or measured move
- Patience is key — wait for the gap to "show its hand"

---

## Hard Rules

### DO:
- Always start top-down (Daily → H4 → H1 → M5)
- Require 2-3+ confluence factors for every entry
- Wait for pullback/confirmation, never chase
- Journal everything
- Study replays obsessively

### DON'T:
- Trade without confluence
- Force entries
- Ignore higher-TF bias
- Overtrade
- Trade in chop without a clear break
- Revenge trade after losses

---

## How Cue Banks Differs From Mamba and Tori

| Aspect | Mamba | Tori | Cue Banks |
|--------|-------|------|-----------|
| Style | Scalping (10-30 min) | Swing (days-weeks) | High-RR scalp/swing (min-hours) |
| Timeframe | 5m bias, 1m entry | Monthly→4H | Daily/H4 bias, M5 entry |
| Instrument | NAS100, US30 | PL, CL, GC, YM | **US30 primary** |
| Key concept | S/R breakout + volume | Action/Safety Line | **Confluence clusters** |
| R:R target | 1:3 to 1:5 | 2R+ trail | **1:7 to 1:8** |
| Unique tool | Structure on 1-min | Top-down trendlines | **Fibonacci + exhaustion + gaps** |
| Session | NY open only (45 min) | Any (alerts) | NY session (patient) |

**The three together cover the full spectrum:**
- Mamba: catches the opening momentum burst (minutes)
- Cue Banks: catches the intraday trend with high R:R (minutes to hours)
- Tori: catches the multi-day/week swing (days to weeks)

All three on US30/YM — different timeframes, different entry logic, same instrument. Plus Tori adds commodities.

---

## Algorithm Translation Notes

### Confluence Detection Engine
```
For each price level/zone on M5:
  score = 0
  
  # 1. Horizontal S/R
  if level is within 0.1% of a prior swing high/low on H4/Daily:
    score += 1
    if it's a psychological round number (divisible by 1000): score += 0.5
    if it's a role-flip (was support, now resistance or vice versa): score += 0.5
  
  # 2. Fibonacci retracement
  if level is within 0.2% of a 38.2% or 61.8% fib from recent H4 swing:
    score += 1
  
  # 3. Market structure
  if H4 shows clear HH/HL (bullish) or LH/LL (bearish) bias:
    score += 1
  
  # 4. Exhaustion
  if recent candles show long wicks rejecting the level (wick > 2x body):
    score += 1
  
  # 5. Trendline/counter-trend break
  if a trendline or counter-trend line was recently broken confirming direction:
    score += 1
  
  # 6. Consolidation break
  if price just broke out of a range and is pulling back to the breakout zone:
    score += 1
  
  MINIMUM to trade: score >= 3
  IDEAL (sniper): score >= 4
```

### Fibonacci Integration
```python
def compute_fib_levels(swing_high, swing_low, direction='long'):
    """Compute key fib levels for entries and targets."""
    diff = swing_high - swing_low
    
    # Retracement levels (entry zones)
    retracements = {
        '0.236': swing_high - diff * 0.236,
        '0.382': swing_high - diff * 0.382,  # KEY entry level
        '0.500': swing_high - diff * 0.500,
        '0.618': swing_high - diff * 0.618,  # KEY entry level (golden)
        '0.786': swing_high - diff * 0.786,
    }
    
    # Extension levels (targets)
    extensions = {
        '-0.272': swing_high + diff * 0.272,  # TP1
        '-0.618': swing_high + diff * 0.618,  # TP2 (big target)
        '-1.000': swing_high + diff * 1.000,  # TP3 (full extension)
    }
    
    return retracements, extensions
```

### Gap Detection
```python
def detect_gap(df, min_gap_pct=0.3):
    """Detect opening gaps between sessions."""
    # Gap = difference between prior close and current open
    # Gap > 0.3% of price = tradeable gap
    # Track: gap fill %, gap as magnet, gap rejection
```

### IBKR Contract
```
YM (Dow futures): Future("YM", exchange="CBOT") — client ID 108
MYM (Micro Dow):  Future("MYM", exchange="CBOT") — client ID 108
# Same instrument as Mamba's YM=F but different entry logic
# Mamba uses breakout on 1-min, Cue Banks uses confluence pullback on 5-min
```
