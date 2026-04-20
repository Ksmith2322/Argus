# Tori Trades Complete Rulebook

**Source:** Victoria Duke (@ToriTrades) — toritradez.com
**Compiled from:** 9 transcripts (master class, exit criteria video, trendline maintenance video, trend reversal basics, 3-step beginner video, Apex 5m live, trendline rating video, live stream Q&A, and the original "Breaking Down My SIMPLE Trading Strategy")
**Last updated:** 2026-04-19 — see v2 updates section at the bottom of this file.

**Primary consolidated reference:** `memory/project_strategy_playbooks_20260419.md` §1 (Tori Trades).
**Audit / subset-edge findings:** `memory/project_mamba_tori_review_20260419.md`.

---

## The One-Sentence Strategy

"Top-down trendline markup (monthly to 4H) → identify A+ Action Line (3+ touches, proper spacing/slope) → wait for break/bounce/retest with clear opposing Safety Line → enter on 4H confirmation → stop beyond Safety Line (1-2% risk) → trail/exit on Safety Line break or 2R+ S/R."

---

## Markets
- **Primary:** Platinum (PL/XPTUSD), Crude Oil (CL/WTI), Gold (XAU/XAUUSD), DOW (YM/US30)
- These respect trendlines well and trend strongly
- Avoid instruments with frequent fake-outs
- Be careful during futures rollover periods

## Timeframes
- **Monthly → Weekly → Daily → 4H:** Top-down analysis (mandatory every setup)
- **4H:** Core execution timeframe — entries, exits, trendline management
- **1H/30m/5m:** Occasional precision on structure (not primary)
- Low screen time: set TradingView alerts on trendlines, walk away

## Style
- **Swing trading** — trades last days to weeks
- **No indicators, no SMC/ICT/Fibonacci** — pure manual trendline price action
- **Mechanical and rule-based** — treat trades as "science experiments"

---

## A+ Trendline Criteria (Strict Mechanical Rules)

| Rule | Requirement |
|------|-------------|
| Touchpoints | 3+ clean taps (wicks count). Will take 2-touch but prefers 3+ |
| Spacing | 6+ candles between taps (clean separation, more = better) |
| Slope | <45 degrees when viewing 3 months of data (not too steep) |
| Duration | 3+ weeks of price data from first touch to break |
| Integrity | No price intersection — if price slices through, line is invalid |

"Think of your trendline as support — truly holding the price up. Keep it clean. Keep it structured."

## Drawing Rules
- **Upward trendline (bullish):** Connect swing lows — price respects from below
- **Downward trendline (bearish):** Connect swing highs — price respects from above
- Use **ray tool** (not segment) in TradingView
- Drag Point B to most recent touch every time
- Draw BOTH directions on every chart — ready for either direction
- Color code: red downward, green upward

---

## Core Concepts: Action Line vs Safety Line

| State | Action Line | Safety Line |
|-------|-------------|-------------|
| Before break | Lines are neutral | Lines are neutral |
| After break | The BROKEN trendline = entry trigger | Opposing trendline = stop/exit |

"Action line = entry. Safety line = risk management = stop-loss = fire extinguisher."

---

## The Three Setups

### 1. Trendline Bounce (Lowest Risk — Beginner Favorite)
- Price touches and respects an existing trendline
- Action Line = Safety Line (same line)
- **Entry:** On the touch/bounce (4H candle respects the line)
- **Stop:** Close beyond the trendline invalidates
- Requires 2-3+ clear touchpoints + at least 1 week of data

### 2. Trendline Break (Core Setup)
- Price breaks through an established trendline
- **Entry:** On 4H candle CLOSE past the Action Line (not just a wick)
- Immediately draw opposing Safety Line
- **Variations:**
  - 2-Touchpoint Break: Faster but higher risk
  - 3-Touchpoint Break: Her go-to high-probability setup

### 3. Break & Retest (Lowest Risk Variation)
- Price breaks Action Line → pulls back → retests from opposite side
- **Entry:** On the retest (tighter risk, better confirmation)
- Safety Line still governs exit
- Often the best R:R setup

**Rule:** Only ONE attempt per trendline. If it fails, move on — no revenge.

---

## Entry Rules
1. Wait for setup to form via top-down analysis
2. Confirm A+ criteria (or acceptable 2-touch)
3. For breaks: enter on **4H candle close** past Action Line
4. For bounces: enter on touch/respect of the line
5. For break & retest: enter on retest
6. **No entry without a clear Safety Line** — if none exists at break, wait
7. Direction is revealed by the break — never predict in advance
8. Horizontal S/R levels are bonus confirmation but not required

## Risk Management
- **Stop Loss:** Just beyond Safety Line (give wicks room)
- **Risk per trade:** 1-2% of total capital
- **Position sizing:** Dynamic based on distance to Safety Line
  - $3K account → ~$60 risk
  - $5K → ~$100
  - $100K → $1K-$2K

## Exits (Multiple Options)
1. **Primary:** Exit when price breaks the Safety Line (trail stop along Safety Line as new swings form)
2. **Alternative:** Scale/exit at first horizontal S/R offering 2R+
3. Trail aggressively on strong moves
4. Manual exits based on structure changes

"If your potential loss feels like a fee, you're trading correctly."

---

## How Tori Differs From Mamba

| Aspect | Mamba (MambaFX) | Tori (Tori Trades) |
|--------|----------------|-------------------|
| Style | Scalping (10-30 min) | Swing trading (days-weeks) |
| Timeframe | 5-min bias, 1-min entry | Monthly→4H top-down, 4H entry |
| Session | NY open only (45 min) | Any time (use alerts) |
| Instruments | NAS100, US30 | Platinum, Crude Oil, Gold, US30 |
| Screen time | High (watch open) | Low (alerts, check 2-3x/day) |
| Entries/day | 1-2 max | 1-2 per WEEK |
| Hold time | 10-30 minutes | Days to weeks |
| R:R target | 1:3 to 1:5 | 2R+ minimum, trail for more |
| Key concept | S/R + breakout + volume | Action Line + Safety Line |

**They are completely complementary:**
- Mamba catches the intraday momentum at open
- Tori catches the multi-day/week swing moves
- Different timeframes, different instruments, same core skill (trendlines + price action)

---

## Algorithm Translation Notes

### Top-Down Trendline Detection
```
For each instrument (PL, CL, XAU, YM):
1. Download monthly, weekly, daily, 4H bars
2. On each timeframe:
   a. Find swing highs/lows using pivot detection
   b. Fit ascending trendlines through swing lows
   c. Fit descending trendlines through swing highs
   d. Score each line: touches, spacing, slope, duration
   e. Filter to A+ criteria (3+ touches, 6+ candle spacing, <45 degree slope)
3. Carry surviving lines down to 4H (the execution timeframe)
```

### Setup Detection (4H)
```
For each A+ trendline:
  BOUNCE:
    - If price is within 0.5 ATR of the trendline AND respecting it (wick touch + rejection candle)
    - → SIGNAL: enter in direction of the trendline

  BREAK:
    - If 4H candle CLOSES beyond the trendline by > 0.1 ATR
    - AND opposing Safety Line exists
    - → SIGNAL: enter in break direction

  BREAK & RETEST:
    - If trendline was broken (candle close beyond)
    - AND price returns to within 0.3 ATR of the broken line
    - AND shows rejection (wick + reversal candle)
    - → SIGNAL: enter in original break direction (lowest risk)
```

### Safety Line / Stop Placement
```
stop_price = safety_line_value_at(current_bar) + (direction * ATR * 0.3)
# Give room beyond the Safety Line for wicks
# Safety Line is dynamic — recalculate as new swings form
```

### Exit Logic
```
For each open position:
  1. Compute current Safety Line value
  2. If price closes beyond Safety Line → EXIT (stop hit)
  3. If unrealized P&L > 2R → trail stop to Safety Line
  4. As Safety Line updates with new swings → tighten trail
  5. Optional: partial exit at 2R, rest trails
```

### IBKR Contracts
```
PL (Platinum):  Future("PL", exchange="NYMEX")  — client ID 104
CL (Crude Oil): Future("CL", exchange="NYMEX")  — client ID 105
GC (Gold):      Future("GC", exchange="COMEX")   — client ID 106
YM (Dow):       Future("YM", exchange="CBOT")    — client ID 107
# Or micro contracts: MPL, MCL, MGC, MYM for smaller sizing
```

---

## v2 updates from 2026-04-19 transcript consolidation (9 videos)

### Exit-logic audit — CRITICAL ROOT CAUSE

The current bot's `_find_opposing_safety` at `forge/tori/trendlines.py:414-434` returns `recent swing high + 0.3*ATR` (or low) — a **flat horizontal level**, not an angled trend line.

CSV r-multiple distribution confirms the problem:
- 44 of 65 losers at ~-1R (stops working)
- 15 of 19 winners capped at 1-2R (trail too tight)
- 1 outlier at 11.64R (the one Bounce-A winner that keeps the subset edge alive)
- Sum of R-multiples = **-15.24**

Tori's method requires the occasional 5-10R runner. Our swing-based trail pulls the stop in too tight, slicing off the right tail of the return distribution. **This alone explains why Break-A and Break-A+ setups lose despite passing her setup grading — the entry is right, the exit is wrong.**

### Safety line is TWO stages (new mechanical insight)

From the dedicated exit-criteria video:

1. **At entry**: safety line = opposing trend line (angled), fixed at entry. Stop goes on the *other side* of this line (past it by 0.1-0.3%, never on it).
2. **During trade, once move develops**: draw a NEW steeper trend line using:
   - Point A = the last touch point from *before* the move
   - Point B = the most recent post-entry higher-low (longs) or lower-high (shorts)
   This steeper line **replaces** the original safety as the active stop.
3. **Exit fires on whichever comes first**:
   - Current safety line break
   - HTF trend line contact
   - S/R level contact

### Grading formula (from her rating video)

Concrete scoring to apply per trend line:
```
grade = touch_points_score + data_span_score − invalidation_penalty
touch_points_score: 3+ = 5pts, 2 = 3pts, 1 = 0
data_span_score: wide bar range = 5pts, narrow = 2pts
invalidation_penalty: -1 per recent cross

A+ (ready to trade at full conviction): ≥ 9
A (acceptable): 7-8
Borderline (smaller size): 6
Delete: ≤ 3
```

### Line lifecycle — maintain, don't regenerate

Current bot regenerates trend lines every bar by refitting `fit_ascending_trendline` / `fit_descending_trendline` on chunks of swing points. That's a **different architecture** from her method. Her rules:

- Lines are stable objects with persistent identity
- Adjust Point B to new swings as they form (only on closed candles, never open)
- Delete if line becomes horizontal (→ it's now a key level, not a trend line) or inverted
- Don't adjust lines **while in a trade** — entry attribution stays fixed
- One attempt per trend line — once a line has been traded and failed, don't reuse it

Faithful v2 needs a trend-line object store with create/adjust/delete semantics and stable IDs.

### Time frame is context-dependent

- **4H**: her default for personal capital, weekly/days-long holds
- **1H**: intermediate, day-long holds
- **5-minute**: for **Apex prop firm compliance** (no overnight holds), intra-day only

Same rules at every TF. Only bar size and hold horizon change.

### Entry timing (reconciled across 9 videos)

- **Enter on the break, NOT candle close.** Live-stream Q&A: *"I do not wait for the candle to close."*
- **Wait for candle close on EXIT when uncertain** — fakeout-check nuance from her rating video.

### Subset edge (from `forge/logs/tori/backtest_trades.csv`)

By setup × grade:
| Bucket | Trades | WR | PnL$ | PF |
|---|--:|--:|--:|--:|
| Bounce A | 5 | 20% | +810 | **3.56** |
| Break A | 44 | 32% | -1355 | 0.45 |
| Break A+ | 12 | 33% | -484 | 0.44 |
| Break Retest | 4 | 0% | -643 | 0.00 |

By instrument:
| Instrument | Trades | PF | PnL |
|---|--:|--:|--:|
| Dow | 22 | **1.85** | +835 |
| Gold | 10 | 0.65 | -177 |
| Platinum | 13 | 0.29 | -826 |
| Crude Oil | 20 | 0.08 | -1503 |

**Bounce-A-only and Dow-only are the positive subsets.** Break/Break-A+/Break-Retest lose badly — consistent with my exit-logic audit: even when setup grading passes, the wrong trail shape kills the trade.

### Bridge plan (priority order)

1. **Fix the exit logic first** — replace swing-based stop with two-stage trend-line trail. This alone should materially improve all break setups.
2. **Add grading function** per the formula above.
3. **Promote Bounce-A-only + Dow-only as active research candidates.** Disable other setups until they pass independent backtests with the new exit logic.
4. Add HTF context (monthly/weekly/daily trend lines propagated into 4H or 5m scan).
5. One-attempt-per-trend-line enforcement with stable `trendline_id`.
6. (Longer term) rebuild trend-line store as persistent objects with maintain/adjust/delete lifecycle.

### Honest expectation

Filtered to Bounce-A + Dow-only, we get ~5 trades of 65. Even with a perfectly implemented v2, the confidence artifact's `p_expectancy_positive` should remain `null` below the 10-trade sanity bar. The narrow-scope rebuild is expected to produce lower trade frequency and require more time to earn a real confidence number.
