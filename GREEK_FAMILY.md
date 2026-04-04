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

### ARES — The Warrior (Event-Driven) [ROADMAP — Phase 2]
**Role**: Trade the reaction to scheduled high-impact economic events
**Status**: ROADMAP — build after Hermes/Apollo prove themselves in paper
**Timeframe**: 5min-1hr bars, 1-24 hour holds around events
**Edge**: Market overreacts to NFP/FOMC/ECB → fade or ride based on deviation from consensus
**Prerequisites**:
- Economic calendar API integration (scheduled event times + consensus estimates)
- Deviation detection (actual vs consensus → magnitude of surprise)
- Pre-event positioning logic (straddle or directional based on vol regime)
- Post-event fade/ride classifier
**Instruments**: EUR/USD (ECB, NFP), USD/JPY (FOMC, BOJ), GBP/USD (BOE)
**Estimated build**: 1-2 weeks after Phase 1 strategies validated
**Risk**: Events are binary — high reward but tail risk. Needs strict sizing (0.25% max per event trade)

### ATLAS — The Titan (Pairs/Stat Arb) [ROADMAP — Phase 2]
**Role**: Trade correlated pair divergence/convergence
**Status**: ROADMAP — needs cointegration research + spread modeling
**Timeframe**: 1hr-daily bars, 1-10 day holds
**Edge**: When EUR/USD and GBP/USD diverge beyond 2 standard deviations of historical spread, trade convergence
**Prerequisites**:
- Cointegration test (Engle-Granger or Johansen) on pair combinations
- Rolling z-score of spread
- Half-life estimation for mean reversion speed
- Simultaneous long/short execution on both legs
**Candidate pairs**:
- EUR/USD vs GBP/USD (USD factor)
- AUD/JPY vs NZD/JPY (risk sentiment)
- EUR/JPY vs GBP/JPY (JPY factor)
- Gold vs Silver (metals spread)
**Estimated build**: 2-3 weeks, research-heavy
**Risk**: Spreads can blow out during crises (correlation breakdown). Need regime filter.

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

### Phase 1.5: OPERATIONAL GAPS (Must fix before paper/real)

These are known issues discovered during deployment. Must be resolved before any strategy moves beyond watcher.

#### Critical

- [x] **Helio family watchdog** — Apollo/Hermes/Helio runners die silently and are NOT supervised by the Argus watchdog. Need either:
  - Integrate into `watchdog_managed.ps1` (add Helio process monitoring)
  - OR build a separate `helio_watchdog.ps1`
  - Runners have been found dead multiple times with stale heartbeats

- [x] **Cross-strategy position conflict enforcement** — `portfolio_guard.py` exists (built by Codex) but needs verification:
  - Test: Argus LONG AUD/JPY + Apollo SHORT AUD/JPY → should block the second entry
  - Test: Hermes LONG Gold + Helio LONG Gold → should allow (same direction) or limit total exposure
  - Verify portfolio guard reads heartbeat/state files from ALL strategy families

- [x] **Shared portfolio risk budget** — currently each strategy has its own risk limits siloed:
  - Argus: PortfolioRiskManager with 3% DD pause, 2 max same-currency
  - Helio/Apollo/Hermes: no cross-family risk aggregation
  - Need: total fleet exposure cap across ALL strategies (e.g., max 5% total risk at any time)

#### High

- [ ] **IBKR connection management** — 4 separate IBKR connections (clientId 1, 200, 210, 220):
  - IBKR TWS/Gateway has a limit of ~8 simultaneous connections
  - Each runner holds a persistent connection 24/7 even when only evaluating once daily
  - Consider: shared connection pool, or connect-evaluate-disconnect pattern for daily strategies

- [x] **Helio runners not in governance pipeline** — Argus has managed truth refresh, promotion gates, divergence guard, alert escalation. Helio family has none of this:
  - No promotion pipeline for Helio/Apollo/Hermes
  - No divergence guard comparing live vs backtest
  - No kill discipline for swing strategies
  - No dashboard integration (Helio heartbeats not shown on main dashboard)

- [ ] **Regime router validation** — `regime_router.py` classifies regimes and deprioritizes strategies, but:
  - Not tested against historical data
  - Unclear if regime classification is stable or noisy
  - Could incorrectly suppress the right strategy at the wrong time

#### Medium

- [x] **Duplicate runner prevention** — process locks exist but each launch creates new PIDs:
  - Watchdog restarts can create duplicates (seen: 2x APOLLO, 2x HERMES, 2x HELIO)
  - Need: check for existing process before launching, or use PID file locking

- [x] **Helio log directory structure** — currently at `helio/logs/` separate from `argus_flow/logs/`:
  - Dashboard reads from `argus_flow/logs/` only
  - Helio heartbeats invisible to dashboard system health
  - Need: either unify log roots or add Helio log scanning to dashboard

- [x] **Daily evaluation timing** — all Helio strategies evaluate at 21:00 UTC:
  - FX market close is 22:00 UTC Friday
  - US equity close is 20:00 UTC (16:00 ET)
  - Asian session instruments may need different evaluation time
  - Apollo on FX should evaluate after London close (17:00 UTC), not US close

- [ ] **Hermes sample size concern** — best backtest result is PF 21 on 6 trades:
  - Statistically meaningless at this sample
  - Need 30+ trades before trusting the result
  - May need longer backtest period or more instruments

- [ ] **Apollo stop placement** — current stop at 0.5x ATR is very tight for mean reversion:
  - Backtest shows high stop-out rate (60-70% of exits are stops)
  - Winning trades are large enough to compensate, but tight stops mean high churn
  - Consider widening to 1.0-1.5x ATR and testing impact

### Phase 2: FUTURE RELATIVES (After Phase 1 validated)

- [ ] **Ares** (Event-Driven) — trade NFP/FOMC/ECB reactions
- [ ] **Atlas** (Pairs/Stat Arb) — correlated pair divergence/convergence
- [x] **Unified dashboard tab** for all Greek family strategies
- [ ] **Cross-family performance report** comparing strategy PnL, drawdown, and correlation
- [ ] **Meta-allocator** — dynamically shift capital to whichever family is currently performing best

### Phase 3: SCALE
- Expand winning strategies to new instruments
- Cross-strategy portfolio optimization
- Edge-weighted allocation across families
- Consider adding: **Hephaestus** (grid/DCA for ranging markets) if regime router identifies extended sideways periods
- Consider adding: **Athena** (defensive/hedging strategy) that activates during drawdowns to protect portfolio

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
