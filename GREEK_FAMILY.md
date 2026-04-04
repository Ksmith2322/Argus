# THE GREEK FAMILY — Argus Strategy Multiverse
## One brain, many strategies, shared infrastructure
**Date**: 2026-04-03

---

## ARCHITECTURE

```
                    ┌─────────────────────┐
                    │       ARGUS          │
                    │    (The Brain)       │
                    │                     │
                    │  IBKR Connection    │
                    │  Risk Management   │
                    │  Promotion Pipeline │
                    │  Dashboard + Viz   │
                    │  Governance        │
                    └────────┬────────────┘
                             │
            ┌────────────────┼────────────────┐
            │                │                │
     ┌──────┴──────┐  ┌──────┴──────┐  ┌──────┴──────┐
     │   HELIO     │  │   HERMES    │  │   APOLLO    │
     │  (Swing)    │  │ (Momentum)  │  │ (Reversion) │
     └──────┬──────┘  └──────┬──────┘  └──────┬──────┘
            │                │                │
     ┌──────┴──────┐  ┌──────┴──────┐  ┌──────┴──────┐
     │  ARTEMIS    │  │   ARES      │  │   ATLAS     │
     │ (Session OR)│  │  (Event)    │  │ (Pairs/Arb) │
     └─────────────┘  └─────────────┘  └─────────────┘
```

---

## THE FAMILY

### ARGUS — The All-Seeing Eye (Brain + Drift Capture)
**Role**: Platform brain + intraday session-gated drift capture
**Status**: ACTIVE — paper trading, signal proven real
**Timeframe**: 1min bars, 45-75 min holds
**Edge**: Range expansion during London session predicts small directional drift
**Instruments**: FX pairs (AUD/JPY, USD/JPY, EUR/USD, GBP/USD, etc.)
**Direction**: Both long and short
**Best PF**: AUD/JPY 1.685, USD/JPY 1.807

### HELIO — The Sun (Swing Trend Following)
**Role**: Daily/4hr trend following with pullback entries
**Status**: ACTIVE — watcher mode, backtest PF 4.0+ on Gold
**Timeframe**: Daily bars, 1-15 day holds, 1hr entry timing
**Edge**: Established trends + pullback to 50 EMA + range expansion + volume surge
**Instruments**: MGC (Gold), MNQ (Nasdaq), MES (S&P), MYM (Dow), GLD, SPY
**Direction**: Both long and short
**Best PF**: Gold 4.25, Nasdaq 1.33

### HERMES — The Messenger (Momentum/Breakout)
**Role**: Capture explosive directional moves on consolidation breaks
**Status**: TO BUILD
**Timeframe**: 15min-4hr bars, 1-24 hour holds
**Edge**: Price consolidates in tight range → volume spike breaks the range → momentum carries
**Entry**: Break of N-bar consolidation range with volume > 2x average
**Exit**: Trailing stop at 1.5x ATR, target at 3x risk
**Instruments**: Same FX pairs + futures as Argus/Helio
**Direction**: Both (breakout direction)
**Key difference from Argus**: Argus enters ON range expansion. Hermes enters AFTER consolidation breaks.

### APOLLO — The Oracle (Mean Reversion)
**Role**: Capture snap-back moves when price overextends from mean
**Status**: TO BUILD
**Timeframe**: 1min-1hr bars, 15-60 min holds
**Edge**: When price moves > 2x ATR from EMA in a session, it tends to revert
**Entry**: RSI extreme + distance from EMA > 2x ATR + reversal candle
**Exit**: Return to EMA or 1x ATR reversion, tight stop at new extreme
**Instruments**: Same FX pairs (works best on liquid, mean-reverting pairs)
**Direction**: Both (counter-trend)
**Key difference from Argus**: Argus trades WITH the drift. Apollo trades AGAINST the overextension.

### ARTEMIS — The Hunter (Session Open Range Breakout)
**Role**: Trade the breakout of the first 30 minutes of London/NY sessions
**Status**: TO BUILD
**Timeframe**: 5min bars for range, hold 2-6 hours
**Edge**: First 30 min of London (07:00-07:30 UTC) establishes the session range. Breakout direction predicts session direction ~55-60% of the time.
**Entry**: Price breaks above/below the first-30-min high/low with volume
**Exit**: Session end (end of London or NY overlap), or trailing stop
**Instruments**: EUR/USD, GBP/USD, USD/JPY (most active at London open)
**Direction**: Both (breakout direction)
**Key difference from Argus**: Fixed session window, fixed range definition, simpler logic.

### ARES — The Warrior (Event-Driven) [FUTURE]
**Role**: Trade the reaction to scheduled high-impact economic events
**Status**: FUTURE — after other strategies prove themselves
**Edge**: Market overreacts to NFP/FOMC/ECB → fade or ride the move based on deviation from consensus

### ATLAS — The Titan (Pairs/Stat Arb) [FUTURE]
**Role**: Trade correlated pair divergence/convergence
**Status**: FUTURE — needs cointegration research
**Edge**: When EUR/USD and GBP/USD diverge beyond normal range, trade the convergence

---

## INSTRUMENTS BY STRATEGY

All strategies start on **current pairs only** — no new instruments until proven.

| Instrument | Argus (Drift) | Helio (Swing) | Hermes (Momentum) | Apollo (Reversion) | Artemis (Session OR) |
|-----------|:---:|:---:|:---:|:---:|:---:|
| EUR/USD | x | | x | x | x |
| GBP/USD | x | | x | x | x |
| USD/JPY | x | | x | x | x |
| AUD/JPY | x | | x | x | |
| EUR/JPY | x | | x | x | |
| GBP/JPY | x | | x | | |
| AUD/USD | x | | x | x | |
| CAD/JPY | x | | x | | |
| MGC (Gold) | | x | x | | |
| MNQ (Nasdaq) | | x | x | | |
| MES (S&P) | | x | x | | |
| MYM (Dow) | | x | | | |

---

## BUILD ORDER

### Phase 1: NOW (backtest + deploy watchers)
1. **Hermes** — momentum/breakout (natural complement to Argus drift)
2. **Apollo** — mean reversion (uses the signals Argus rejects)
3. **Artemis** — session open range (classic, well-documented)

### Phase 2: AFTER Phase 1 proves itself
4. **Ares** — event-driven
5. **Atlas** — pairs/stat arb

### Phase 3: SCALE
- Expand winning strategies to new instruments
- Cross-strategy portfolio optimization
- Edge-weighted allocation across families

---

## SHARED INFRASTRUCTURE (from Argus brain)

Every strategy reuses:
- IBKR connection + contract handling
- BarBuffer + multi-timeframe aggregation
- Risk management (drawdown, correlation, sizing)
- Promotion pipeline (watcher → paper → real)
- Dashboard (one tab per strategy or unified view)
- Trade journal + execution quality tracking
- Governance + alerts
- Kill switch / graceful exit / pause

Each strategy adds:
- Its own entry/exit logic
- Its own config files
- Its own log directory
- Its own backtest parameters
