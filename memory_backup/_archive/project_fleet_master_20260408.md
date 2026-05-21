---
name: Fleet Master Plan — Source of Truth
description: Complete status of all trading systems (Greek family), what's built, what's pending, deployment order, IBKR constraints
type: project
---

# Helio Fleet Master Plan
**Updated: 2026-04-09**
**Goal:** 5-6 uncorrelated automated strategies running in parallel. Fleet compound target: 15-30%/year on $10K. Prove edge → fund → compound.

## IBKR Constraints
- **Max connections:** 32 simultaneous clientIds per account
- **Market data lines:** 100 concurrent (paper), need ~1 per symbol being streamed
- **Order rate:** 50 orders/sec (not a concern for swing/daily strategies)
- **Currently using:** ~10 clientIds (Argus 4 active + some killed configs still registered)
- **Available:** ~20 clientIds for Titan, Ares, Hermes, Oracle
- **Account:** U24860535, paper, $53 real equity, $10K model equity per strategy

## The Fleet

### ARGUS — FX Intraday
- **Strategy:** MTF trend (4H/1H/5M) + AI overlay (15 voters) + Governor (GBT classifier)
- **Instruments:** AUDJPY, USDJPY, GBPUSD, CADJPY
- **Timeframe:** 5m eval, 1-2hr holds
- **Edge:** Session timing + trend alignment + AI gating. PF 1.1-1.3 backtest.
- **Status:** LIVE PAPER

| Component | Status | Done? |
|-----------|--------|-------|
| Runner (runner_unified.py) | Running 4 pairs | YES |
| MTF Strategy Engine | 3-layer signal generation | YES |
| AI Overlay | 15 voters, cross-pair pooling | YES |
| Governor Model | LOG_ONLY, 114-trade training | YES |
| Feature Capture (schema v5) | 24 columns per signal | YES |
| Dashboard | Live, killed configs hidden | YES |
| Discord Alerts | Trade entry/exit + nightly summary | YES |
| Nightly Analysis (6 jobs) | Auto-runs, Claude prompt file | YES |
| Watchdog | EXISTS but needs verification | NEEDS CHECK |
| News Calendar Gate | Gate exists, no data source | NOT DONE |
| ATR-Scaled Stops | Fixed 15-pip stops currently | NOT DONE |
| Governor GATE Mode | Needs 30 live trades to validate | BLOCKED (data) |

**Pending milestones:**
- 30 trades → validate governor → GATE mode (~2 weeks)
- 50 trades → retrain on live data
- 100 trades → statistical keep/kill per pair

---

### TITAN — Stock/Commodity Swing
- **Strategy:** 4 sub-strategies (Trend Follow, Breakout, Mean Reversion, Trendline Cascade)
- **Instruments:** GLD, GDX, PLTR, MRNA, QQQ, SPY, SLV, TSLA, MARA, USO
- **Timeframe:** Daily + 4H, 2-20 day holds
- **Edge:** Long-only trend following. PF 1.42 backtest (longs only, 5yr, 954 trades).
- **Status:** SCANNER LIVE, no execution yet

| Component | Status | Done? |
|-----------|--------|-------|
| Data Pipeline | 18 symbols x 3 timeframes | YES |
| Swing Engine (4 strategies) | Trend, Breakout, MeanRev, Trendline | YES |
| Backtester | 954 trades analyzed | YES |
| Nightly Scanner | Discord alerts, long-only mode | YES |
| AI Overlay Port | Not started | NOT DONE |
| IBKR Execution Runner | Not started | NOT DONE |
| Paper Trading | Not started | NOT DONE |
| Task Scheduler (nightly) | Not added yet | NOT DONE |

**Pending milestones:**
- Add to nightly Task Scheduler
- 2-4 weeks manual signal tracking → validate scanner accuracy
- Port AI overlay from Argus → Titan trend following
- Build IBKR execution runner
- Paper trade 30+ signals → prove edge → fund

---

### ARES — Sector Rotation (Monthly)
- **Strategy:** Relative Strength Rotation — buy top 2 of 6 sector ETFs, rebalance monthly
- **Instruments:** SPY, QQQ, GDX, XLE, SMH, XBI
- **Timeframe:** Monthly rebalance
- **Edge:** Momentum factor, one of the oldest proven anomalies. Low maintenance.
- **Status:** NOT BUILT

| Component | Status | Done? |
|-----------|--------|-------|
| Strategy logic | Not started | NOT DONE |
| Backtester | Not started | NOT DONE |
| Monthly rebalance script | Not started | NOT DONE |
| Discord alerts (monthly) | Not started | NOT DONE |
| IBKR execution | Not started | NOT DONE |

**Build estimate:** 1-2 sessions. Simple strategy, monthly cadence.

---

### HERMES — Gap Fill (Daily)
- **Strategy:** Stocks that gap >2% at open → trade the fill back to prior close
- **Instruments:** Dynamic — scan top movers each morning
- **Timeframe:** Daily, 1-3 day holds
- **Edge:** ~70% of gaps fill. Mechanical entry/exit rules.
- **Status:** PARTIAL — old Helio runner exists (consolidation breakout), needs repurposing

| Component | Status | Done? |
|-----------|--------|-------|
| Gap detection scanner | Not started | NOT DONE |
| Backtester | Not started | NOT DONE |
| Morning alert (pre-market) | Not started | NOT DONE |
| Execution logic | Old helio/runner_hermes.py exists (different strategy) | NEEDS REPURPOSE |

**Build estimate:** 1-2 sessions. Note: old Hermes was consolidation breakout on gold, would need full rewrite for gap fill.

---

### ORACLE — Earnings/Catalyst Research (Semi-Manual)
- **Strategy:** Earnings drift + company catalysts (partnerships, mergers, insider buying)
- **Instruments:** Dynamic — upcoming earnings calendar
- **Timeframe:** 2-10 day holds around catalysts
- **Edge:** Post-earnings announcement drift is a robust anomaly. Human judgment required for catalyst quality.
- **Status:** NOT BUILT — Research layer, not fully automated

| Component | Status | Done? |
|-----------|--------|-------|
| Earnings calendar scanner | Not started | NOT DONE |
| Unusual options activity detector | Not started | NOT DONE |
| Insider buying/selling alerts | Not started | NOT DONE |
| Claude analysis prompt generator | Nightly analysis framework exists, extend it | PARTIAL |
| Discord alerts | Framework exists | PARTIAL |

**Build estimate:** 2-3 sessions. More research tool than bot — surfaces opportunities, you decide.

---

## KILLED / ARCHIVED Systems

| System | What | Why Killed |
|--------|------|-----------|
| Argus Crypto (ETH/BTC/SOL) | Range accel on Coinbase spot | 60bps fees, PF <1.0 |
| Argus Cascade | Structural break detection | Signal real but payoff non-existent on spot |
| Apollo FX Swing | Daily mean reversion on FX pairs | FX weekly range too small for swing (1.3%) |
| Vol Burst (MGC/MNQ) | Volume spike on micro futures | Poor results, killed in fleet consolidation |
| 30m Trail configs | Trailing stop on 30m FX | Killed before validation, fleet consolidation |
| 13-pair FX fleet | Broad FX coverage | 99% signal block rate, risk limits too tight |

---

## Deployment Order

| Phase | When | What | Dependencies |
|-------|------|------|-------------|
| **NOW** | Week 1 | Argus + Titan running, collecting data | None |
| **Phase A** | Week 2 | Build Ares (RSR), build Hermes (gap fill) | None |
| **Phase B** | Week 2-3 | Backtest Ares + Hermes, kill or keep | Phase A |
| **Phase C** | Week 3-4 | Argus governor → GATE mode (if validated) | 30 Argus trades |
| **Phase D** | Week 3-4 | Titan AI overlay + execution runner | 2wk Titan signal validation |
| **Phase E** | Week 4-5 | Deploy Ares + Hermes (if backtests pass) | Phase B |
| **Phase F** | Week 5+ | Build Oracle (earnings/catalyst research) | Phases C-E stable |
| **FUND** | Week 6+ | $10K funding if 3+ strategies net positive | All validations pass |

---

## Portfolio Risk Rules (Fleet-Wide)
- Max 2% total portfolio risk per trade across ALL systems
- Max 6% total open risk at any time
- Max 3 positions in same sector across all systems
- Daily fleet loss limit: -3% of portfolio → halt all entries for 24hr
- Each system has independent kill rules (100-trade review)

---

## IBKR ClientId Allocation
- Argus: 50-59 (FX intraday)
- Titan: 60-69 (stock/commodity swing)
- Ares: 70-79 (sector rotation)
- Hermes: 80-89 (gap fill)
- Oracle: manual trades (no dedicated clientId needed)
