# Cue Banks Complete Rulebook

**Source:** Cue Banks (Wall Street Academy, @CueBanks)
**Compiled from:** "Master the US30 Market," Confluence Trading 1.0/2.0/2.5 series, Gap Trading Masterclass, live trade breakdowns, 2026-04-19 live-room session + Confluence 1.0 educational video.
**Last updated:** 2026-04-19 — see v2 updates section at the bottom.

**Primary consolidated reference:** `memory/project_strategy_playbooks_20260419.md` §3 (Cue Banks).

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

---

## v2 updates from 2026-04-19 transcript consolidation

Two transcripts reviewed (live-room session + Confluence 1.0 educational). Confluence 1.0 is substantially more mechanical than earlier public material — surfaces some rules that the current bot likely does not enforce. Audit needed.

### His explicit toolset — only four tools

He states plainly: *"I don't use nothing crazy."*
1. Support/resistance — drawn from **wicks**, not bodies
2. Supply/demand zones — previous wick → breakout candle body
3. Fibonacci PRZ — **38.2 / 61.8 / 78.6 / 88.6**
4. Harmonic patterns — specifically the **bullish bat**:
   - X→A leg (initial move)
   - A→B at 50%
   - B→C at 78.6% (transcript auto-captioned as "seventy point six" — most likely 78.6, his standard PRZ level)
   - C→D at 88.6% of X→A (entry point)

### The single most important rule — repeated 10+ times

**"No retest, no entry."** Price must break the level, then touch back from the opposite side before he enters. He skips setups that run without retesting, even when they look strong.

### Candle-closure validation for breaks

- Half-close over level + next candle rejects = **fake break** (skip)
- Full body close over + next candle forms past level = **valid break**
- Same rule both directions

### Entry mechanics (first time stated precisely in any of his material)

- Pre-places **buy/sell limits** 1 pip past the level for reversal setups
- Pre-places **buy/sell stops** 1 pip past the level for continuation setups
- Prefers ECN brokers for tight spreads so limits fill accurately
- He does NOT manually market-order on the break

### Time-frame split

- Analysis: **4H, 1H, daily**
- Execution: **5-minute** — *"always look for retest on the five-minute chart"*

### Confluence construction (restated)

"A+ setup" = 2-3 confirmations stacked. Example he gives: *support level + uptrend + retest*.

### Current-bot audit gaps (needs code check)

The dashboard's Cue Banks row is *"US30 Confluence + Fib"* with PF 0.89 / 32% WR on 82 trades. Based on the transcripts:

- **Retest enforcement** — almost certainly missing or weak
- **Candle-closure validation** — likely not implemented
- **Harmonic patterns (bullish bat etc.)** — likely absent from `confluence.py`
- **Supply/demand zones** — unclear
- **Limit-vs-market order style** — likely using market orders on break instead of pre-placed limits past the level

If even 2-3 of these are missing, the current "Confluence + Fib" is a partial implementation of his method, not a faithful one. Bot audit required before v2 work.

### Rulebook priority for Cue Banks v2

1. Add `retest_required` gate — no entry without a break + retest from the opposite side
2. Add candle-closure validator — full body must close past the level on the same TF
3. Implement harmonic detector (bullish bat + bearish bat at minimum)
4. Implement supply/demand zone drawer (previous wick → breakout candle body)
5. Switch entry style to limit/stop orders past the level (1-2 ticks)
6. Confluence scorer: require ≥2 of {S/R, Fib PRZ, supply/demand, harmonic pattern} for A+ setup

### Caveat on the source material

Cue Banks' teaching content is notably less mechanical than Tori's. Heavy live-room chatter, mentality focus, vague "trust me" claims. Zero hard numbers (no win rate, no account size, no P&L). Marketing-heavy. The Confluence 1.0 video is the most actionable piece and should be the authoritative source for v2 rule extraction.

---

## 2026-04-19 evening: naive retest gate A/B test failed

Added `CUEBANKS_RETEST_REQUIRED` flag (default False) that, when True,
requires at least one close on the opposite side of the matched S/R level
within the last 10 bars before entering.

### A/B result
| Gate | Trades | WR | PnL | PF |
|---|--:|--:|--:|--:|
| OFF (default) | 82 | 32% | -$514 | 0.89 |
| ON  (retest)  | 83 | 29% | -$893 | **0.80** |

**Naive retest gate made it worse.** Root cause: in a volatile market,
"price was on the other side of some level at ANY point in the last 10
bars" is trivially satisfied — the check is not actually enforcing
"break then retest" in Cue Banks' sense.

### What a faithful retest gate needs

Cue Banks' stated rule is tighter:
1. Price **breaks** a specific level (full-body close past it)
2. Price **comes back** to touch that specific level from the **new** side
3. Entry happens on the retest touch, not on the break

A faithful implementation requires:
- Stateful tracking per level: `broken_at_bar`, `broken_direction`
- Level-close-past validation (not just "price was above/below")
- Retest-touch detection distinct from simple re-entry into the tolerance band
- Time-decay: a "break" from 2 weeks ago isn't a live retest opportunity

### Current status
- Flag kept at default False (v1 behavior preserved)
- Code in place for easy swap once the faithful tracker is built
- Next iteration: build per-level state tracker in `confluence.py` with
  `{level_price, broken_at_bar, broken_direction, retest_touched_at}`
- Then the gate becomes: "require `retest_touched_at >= current_bar - N`"

---

## 2026-04-19 late evening: faithful retest tracker A/B

Built `BreakTracker` class in `confluence.py` implementing stateful
per-level break + retest detection with:
- Body-close validation for break (not just wick)
- Time-decay for stale breaks (30-bar window)
- 3-bar recency window for "fresh retest"

Wired into the runner, rerun the A/B:

| Gate | Trades | WR | PnL | PF |
|---|--:|--:|--:|--:|
| OFF (v1 default) | 82 | 31.7% | -$514 | 0.89 |
| ON (faithful tracker) | 51 | **39.2%** | **-$649** | 0.76 |

### Interpretation

The faithful tracker IS working correctly — it filters 31 of 82 signals
(38% filtered) and does improve win rate (32% → 39%). But total PnL gets
worse and PF drops. That combination means **the gate is removing big
winners while keeping most of the losers**.

**Current Cue Banks bot's best trades are strong breakouts that never
retest.** Filtering those out hurts the strategy. Whatever edge exists in
this implementation is in momentum continuation, not in retest-and-
continuation — the opposite of Cue Banks' stated teaching.

### Implications

Either:
1. **Our S/R detection is finding levels that aren't actually Cue Banks'
   S/R**. His S/R drawing rules (from wicks, at psychological levels,
   multi-TF confluence) may differ materially from our `find_horizontal_sr`
   output. The "retest" we're gating may be against the wrong levels.
2. **The current bot is a breakout momentum strategy wearing a "Cue Banks"
   label**, but mechanically not his method. The 82-trade backtest PnL
   of -$514 may be noise around zero expectation.
3. **The strategy has no edge at the level of detail our code can reach**,
   and adding rules makes it worse because it's filtering on the wrong
   features.

### Current status (2026-04-19 late)
- BreakTracker class retained in `confluence.py` — the implementation is
  correct; it just doesn't lift this particular bot
- Flag defaults False
- Naive retest-check code removed (replaced with the tracker-based check
  that runs only when flag is True)
- **The next meaningful step is NOT more filter work** — it's auditing
  whether `find_horizontal_sr` produces levels consistent with Cue Banks'
  teaching. If our S/R levels are wrong, no amount of retest gating will
  help. That's where more teaching videos would actually move the needle.

---

## 2026-04-20: S/D zones + harmonics added — S/D zones are the fix

Built two missing Confluence 1.0 factors and A/B-tested each:

### Supply/demand zones (`find_supply_demand_zones`)

Draws zones from previous candle's wick to breakout candle's body — exactly
per his transcript. A demand zone (support) forms when price consolidates
then breaks up strongly; zone range = [prior wick low, breakout open].
Supply mirrors downward. Scored as +1 confluence factor when price is
inside the zone.

### Harmonic bat patterns (`detect_bullish_bat`, `detect_bearish_bat`)

Proper 5-point harmonic detection: X/A/B/C/D with retracements at
50% / 78.6% / 88.6%. Scored as +1.5 when the current price aligns with
point D.

### A/B result

| Config | Trades | WR | PnL | PF |
|---|--:|--:|--:|--:|
| Baseline (neither) | 82 | 32% | -$514 | 0.89 |
| **S/D zones ONLY** | **88** | **35%** | **+$2,134** | **1.34** |
| Harmonics ONLY | 84 | 32% | -$335 | 0.93 |
| Both | 88 | 35% | +$2,134 | 1.34 (same as S/D only) |

### Interpretation

**Supply/demand zones are what the Cue Banks bot was missing.** Moving from
PF 0.89 to 1.34 is a large lift and flips ruin fraction from 96% to 0.4%.
P(exp>0) moves from 0.32 to 0.84.

Harmonics don't help materially on this sample — the bullish/bearish bat
patterns rarely align perfectly with an H4 swing structure in our tolerance,
so they fire too infrequently to matter. Code retained for future tuning
(tighter tolerance, lower TF, or better swing detection might unlock them).

### Current defaults (post-v3)
- `CUEBANKS_USE_SD_ZONES = True`  (the fix)
- `CUEBANKS_USE_HARMONICS = False`  (insufficient evidence)
- `CUEBANKS_RETEST_REQUIRED = False`  (BreakTracker correct but didn't lift)

### Cue Banks current artifact
- **PF 1.34, WR 35.2%, P(exp>0) 0.84, MC ruin 0.4%**
- Still 100% backfill — 0 live fills
- Dashboard confidence should jump from 32% to ~84% after restart

### What's left for Cue Banks
1. Forward-paper to validate live edge
2. Retest-gate rebuild: the current BreakTracker removes winners. A better
   implementation might combine S/D-zone retest with the break tracker.
3. Harmonic tuning: lower tolerance, different TF, better swing detection
4. More transcripts to verify S/R detection matches his hand-drawn levels
