# Phase 7 Roadmap — Close Every Gap, Capture Every Angle

**Created:** 2026-04-12
**Philosophy:** Make money at every angle. If a move can be reverse-engineered, add it to the toolbox.

## The Vision

The fleet shouldn't just trade strategies — it should capture value from every measurable market inefficiency: timing, events, positioning, sentiment, structure, and flow. Every dollar sitting idle should be working. Every known edge should be tested. Every gap is a missed opportunity.

---

## TIER 1 — Foundation (Do First, Week of Apr 18-25)

These aren't strategies. They're the infrastructure that makes EVERYTHING else more profitable.

### 1.1 Benchmark Tracker — "Am I Actually Making Money?"
- **What:** Daily fleet P&L vs SPY buy-and-hold comparison
- **Why:** Without this, you can't prove the fleet adds value. Period.
- **Build:** Aggregate all system P&L daily, compare to SPY return over same period, track cumulative alpha
- **Output:** Daily report: "Fleet: +X.X% | SPY: +Y.Y% | Alpha: +/-Z.Z%"
- **Effort:** 1 day

### 1.2 Slippage Measurement — "What's My Real Edge?"
- **What:** Compare backtest fills to actual IBKR fills on every trade
- **Why:** If backtest says 1% edge and slippage eats 0.3%, true edge is 0.7%. If it eats 0.8%, you have no edge.
- **Build:** Parse orders.csv for each system, compute fill price vs expected price, aggregate
- **Output:** Per-system slippage report: avg slippage in bps, % of theoretical edge consumed
- **Effort:** 1 day

### 1.3 Correlation-Aware Position Sizing — "Hidden Risk Killer"
- **What:** Before any entry, check what OTHER systems are holding. If Titan is long NVDA and Apollo wants long NVDA, total exposure is managed.
- **Why:** Without this, you think you have 5 independent bets but actually have 3 correlated ones
- **Build:** Shared positions ledger. Before entry, compute net sector/ticker exposure across all systems. Scale new position size down if correlated exposure exists.
- **Output:** "Titan wants long NVDA but Apollo already holds NVDA → reduce Titan size by 50%"
- **Effort:** 2 days

### 1.4 Drawdown Recovery Protocol — "How to Get Back In"
- **What:** Systematic rules for resuming trading after a drawdown pause
- **Why:** Currently: pause at 3% DD, then... nothing. Manual judgment. That's a gap.
- **Build:** Tiered recovery: resume at 50% size after 3 green days, 75% after 5 green days, 100% after 10 green days. If DD hits 5%, require manual review. If 8%, kill the system.
- **Effort:** 1 day

### 1.5 Economic Calendar Gate — "Don't Trade Into a Storm"
- **What:** Flip Atlas from LOG_ONLY to GATE mode for calendar events only
- **Why:** FOMC/CPI/NFP create 2-3x normal volatility. Reducing exposure 24h before is free risk reduction.
- **Build:** Already built in Atlas fleet_gate.py. Just flip the mode for calendar events (not regime — that needs more proof)
- **Effort:** Half day

---

## TIER 2 — New Alpha Captures (Week of Apr 25 - May 9)

Each of these is a new way to make money that the fleet currently can't.

### 2.1 VIX Mean-Reversion Strategy — "Buy the Panic"
- **What:** When VIX spikes above 30, buy SPY. Hold 1-3 months. 85% historical win rate.
- **Why:** Single highest-confidence standalone signal in all of finance. Atlas already detects it.
- **Edge:** VIX above 30 has led to positive SPY returns over next 1-3 months in ~85% of cases since 1990
- **Build:** Atlas trigger → SPY position → time-based exit. Simple, mechanical, proven.
- **Frequency:** 2-4 trades/year. Low frequency, high conviction.
- **Effort:** 1 day

### 2.2 Sector Rotation from Atlas — "Trade the Macro"
- **What:** Atlas says "favor XLE, avoid EEM." Actually trade that with sector ETFs.
- **Why:** XLE +25.5% YTD, FXI -9% YTD. Atlas called both. Nobody executed.
- **Build:** Monthly rotation: go long top 3 Atlas-favored sectors, avoid bottom 3. Equal weight. Rebalance when Atlas regime shifts.
- **Assets:** XLE, XLF, XLK, XLV, XLI, XLU, XLY, XLP + GLD, TLT as defensive
- **Edge:** Atlas YTD was 67% accurate on sector direction. Even 55% hit rate with 2:1 reward:risk = profitable.
- **Effort:** 2 days

### 2.3 Commodity Exporter Cascade — "The EWZ Trade"
- **What:** When oil spikes >20%, go long commodity exporters (EWZ, EWA, EWC). When oil crashes, short them.
- **Why:** EWZ +28% YTD was our biggest miss. The connection is structural: Brazil/Australia/Canada export commodities. Oil up = their economies boom.
- **Build:** Oil price monitor → when USO 20-day return exceeds ±10%, enter commodity exporter positions
- **Effort:** 1 day

### 2.4 Defensive Rotation Basket — "Risk-Off Profits"
- **What:** When Atlas flips to RISK_OFF, buy XLU+XLP+GLD basket. When RISK_ON, sell it.
- **Why:** XLU +9.5%, XLP +6.6%, GLD +9.8% YTD in a risk-off year. Defensives make money DURING fear.
- **Build:** Atlas regime trigger → enter defensive basket → exit when regime transitions to NEUTRAL/RISK_ON
- **Effort:** 1 day

### 2.5 Regime Transition Detector — "Catch the Recovery"
- **What:** Detect when risk-off is ENDING (VIX dropping from 30→20, credit spreads tightening). Go aggressive.
- **Why:** The April rally (+3.7% SPY in one week) happened on a regime transition. Atlas stayed RISK_OFF and missed it.
- **Build:** Track VIX rate-of-change. When VIX drops >5 points in 5 days from above 25 → flag RECOVERY. Increase position sizes to 125%.
- **Effort:** 1 day

### 2.6 Congressional Cluster Trades — "Follow the Insiders"
- **What:** When Themis detects a cluster buy (3+ members, same stock, 14 days), enter a long position. Hold 60-90 days.
- **Why:** Congressional cluster buys have documented alpha. The MSFT cluster signal right now is exactly this.
- **Build:** Themis signal → Titan/Apollo integration as a filter or standalone entries
- **Effort:** 1 day (signal already exists, just needs execution layer)

---

## TIER 3 — Edge Optimization (May 9-23)

These improve EXISTING strategies rather than adding new ones.

### 3.1 Per-Stock News Sentiment — "Don't Buy Bad News"
- **What:** Before any Titan/Apollo entry, check recent headlines for the ticker. If sentiment is strongly negative, skip.
- **Why:** Titan might see a technical breakout on a stock that just had a fraud allegation. Headlines would catch that.
- **Build:** yfinance news feed per ticker → FinBERT sentiment score → block entry if negative > 0.7
- **Effort:** 1 day

### 3.2 Earnings Calendar Gate for Titan — "Avoid ER Traps"
- **What:** Titan should never enter a position within 5 trading days of earnings
- **Why:** Earnings create binary risk that technical analysis can't predict. Apollo trades earnings specifically; Titan should avoid them.
- **Build:** yfinance earnings calendar → if ER within 5 days, Titan skips the ticker
- **Effort:** Half day

### 3.3 Position Aging / Time Stops — "Don't Hold Dead Weight"
- **What:** Fleet-wide rule: any position held >20 trading days with <2% unrealized P&L gets reviewed. >40 days = force close.
- **Why:** Capital tied up in flat positions can't be deployed on new opportunities. Opportunity cost is real.
- **Build:** Daily scan of all open positions across systems, flag aging ones, auto-close at max age
- **Effort:** 1 day

### 3.4 Adaptive Stop Widths — "ATR-Scaled Stops Per Regime"
- **What:** Widen stops in high-vol regimes, tighten in low-vol. Currently stops are static.
- **Why:** Static stops get chopped out in volatile markets and leave money on the table in calm markets.
- **Build:** Atlas regime → vol state → multiply stop distance by 1.5x in HIGH vol, 0.8x in LOW vol
- **Effort:** 1 day

### 3.5 Themis Committee Enrichment — "Why Are They Buying?"
- **What:** Map each member to their committee assignments. Weight signals higher when the member sits on a relevant committee (Banking member buying bank stocks = stronger signal).
- **Build:** ProPublica Congress API (free) → committee mapping → relevance score per trade
- **Effort:** 1 day

---

## TIER 4 — Advanced Capture (May 23+)

These are the "every angle" plays — smaller edges that compound.

### 4.1 Cash Yield Optimization
- **What:** Uninvested cash in IBKR earns ~4-5%. Ensure it's swept to highest-yield instrument (auto-invest in BIL/SHV).
- **Why:** If 60% of capital is idle, 4% yield on that = 2.4% free return on total portfolio.
- **Effort:** Config change, not code

### 4.2 Tax-Loss Harvesting
- **What:** Track cost basis on all positions. When unrealized loss > 3% AND holding period > 30 days, consider harvesting.
- **Why:** At $10K portfolio, saves maybe $50-100/year in taxes. At $100K, saves $1-3K/year. Scales with AUM.
- **Build:** Cost basis tracker → harvest scanner → replacement position logic (avoid wash sale)
- **Effort:** 2 days

### 4.3 Options Overlay — "Income on Existing Positions"
- **What:** Sell covered calls on long stock positions (Titan/Apollo). Buy protective puts on concentrated positions.
- **Why:** Covered calls add 1-3% annual income. Protective puts reduce drawdown.
- **Build:** Requires IBKR options permissions. Overlay on existing positions.
- **Effort:** 3-5 days (options are complex)

### 4.4 Intraday Entry Timing
- **What:** Use 1H/15min data to time entries within the day rather than entering at close.
- **Why:** Daily bars say "buy today." But buying at 10am vs 3pm can differ by 0.5-1%. Over many trades, this adds up.
- **Build:** IBKR historical intraday bars → optimal entry time analysis per strategy
- **Effort:** 2-3 days

### 4.5 Portfolio Optimization
- **What:** Mean-variance optimization across all systems. Determine optimal capital allocation.
- **Why:** Currently equal-ish allocation. Data might show Argus deserves 40% and Hermes deserves 5%.
- **Build:** Historical returns per system → correlation matrix → efficient frontier → target weights
- **Effort:** 2 days

### 4.6 Reverse Engineering Patterns
- **What:** Take the top 100 biggest single-day stock moves each year. Reverse engineer: what happened before? Volume pattern? Sector rotation? Options flow? Congressional buying?
- **Why:** If 60% of 10%+ moves share 3 common precursors, that's a new signal.
- **Build:** yfinance → flag large moves → look back 5/10/20 days → feature extraction → pattern detection
- **Effort:** 3-5 days (research project)

### 4.7 Cross-System Signal Amplification
- **What:** When multiple systems agree (Titan breakout + Themis congressional buy + Atlas sector-favor), increase confidence and size.
- **Why:** Independent signals pointing the same direction is the strongest possible setup.
- **Build:** Signal aggregation layer that reads all system outputs, detects convergence, amplifies sizing
- **Effort:** 2 days

### 4.8 Automated Friday Review Report
- **What:** Auto-generate the Friday burn-in review instead of running manual commands.
- **Why:** Consistency. No missed checks. Saves 2 hours every Friday.
- **Build:** Script that runs all review commands, aggregates into a formatted report, posts to Discord
- **Effort:** 1 day

---

## The Build Calendar

### Week 1 (Apr 18-25) — Foundation
- Fri Apr 18: Burn-in review (no building)
- Mon-Tue: Benchmark tracker + slippage measurement
- Wed-Thu: Correlation-aware sizing + drawdown recovery
- Fri: Calendar gate activation + week review

### Week 2 (Apr 25 - May 2) — New Alpha
- Mon-Tue: VIX mean-reversion + sector rotation model
- Wed: Commodity exporter cascade + defensive basket
- Thu: Regime transition detector + congressional cluster execution
- Fri: Week review

### Week 3 (May 2-9) — Edge Optimization
- Mon: Per-stock news sentiment + earnings calendar gate
- Tue: Position aging + adaptive stops
- Wed: Themis committee enrichment
- Thu-Fri: Reverse engineering patterns (research)

### Week 4 (May 9-16) — Advanced
- Portfolio optimization + cross-system amplification
- Options overlay research
- Tax-loss harvesting framework
- Automated Friday report

---

## The Scorecard Target

By end of Phase 7, the fleet should be able to answer YES to all of these:

| Question | Current | Target |
|----------|---------|--------|
| Are we beating SPY? | Don't know | Tracked daily |
| Do we know our real edge after slippage? | No | Measured per system |
| Is cross-system risk managed? | Partially | Fully correlated sizing |
| Do we capture macro regime shifts? | Detect only | Trade them (sector rotation) |
| Do we profit from panic? | No | VIX mean-reversion |
| Do we follow smart money? | Detect only | Trade Themis clusters |
| Do we avoid known volatility storms? | LOG_ONLY | Calendar gate active |
| Is idle cash earning? | Maybe | Swept to highest yield |
| Do we harvest tax losses? | No | Systematic harvesting |
| Can multiple signals amplify each other? | No | Cross-system convergence |

## The Honest Reality

Not all of these will work. Some will get killed just like FOMC Drift and Form 4 Cluster. That's the point — test everything, keep what works, kill what doesn't. The goal isn't to build 20 strategies. It's to find the 5-8 that survive and let them compound.

**Every gap closed is either more return or less risk. Both compound.**
