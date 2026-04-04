# HELIO — Swing Trading System Plan
## "Patient capital, larger moves, proven setups"
**Date**: 2026-04-03  
**Status**: PLANNING  
**Relationship to Argus**: Sibling system, shared infrastructure foundation, separate strategy family

---

## THESIS

Intraday range_accel on FX captures 0.5-5 pip moves with 67% timeout rate.
Swing trading on indices captures 50-500+ point moves over 1-5 days with clear trend structure.

The edge thesis: indices trend more persistently than FX pairs on the 4hr/daily timeframe.
When a stock index expands its range on above-average volume during a trending regime,
the move tends to continue for 1-5 days before exhaustion.

This is the Tori Trades approach: wait for clean structure on the 4hr chart, enter on pullback
to key level, ride the trend with a trailing stop, exit on momentum exhaustion or time.

---

## TOP 10 INSTRUMENTS FOR SWING TRADING

Selected for: liquidity, trend persistence, available on IBKR as micro/mini futures or ETFs.

### Tier 1 — Core (highest liquidity, cleanest trends)

| # | Symbol | Instrument | Why | IBKR Type |
|---|--------|-----------|-----|-----------|
| 1 | **SPY** / **MES** | S&P 500 | Most liquid, cleanest trends, benchmark | ETF or Micro Future |
| 2 | **QQQ** / **MNQ** | Nasdaq 100 | Tech-heavy, strong momentum, volatile | ETF or Micro Future |
| 3 | **IWM** / **M2K** | Russell 2000 | Small cap, leads risk-on/risk-off turns | ETF or Micro Future |
| 4 | **DIA** / **MYM** | Dow 30 | Stable blue chips, clean 4hr structure | ETF or Micro Future |

### Tier 2 — Sector / Commodity Exposure

| # | Symbol | Instrument | Why | IBKR Type |
|---|--------|-----------|-----|-----------|
| 5 | **GLD** / **MGC** | Gold | Safe haven, trends for weeks, mean-reverts cleanly | ETF or Micro Future |
| 6 | **USO** / **MCL** | Crude Oil | Volatile, geopolitical catalyst, strong swings | ETF or Micro Future |
| 7 | **TLT** | 20+ Year Treasury | Rates-driven, macro swing trades | ETF |
| 8 | **XLF** | Financials ETF | Bank sector, rates-sensitive, clean 4hr | ETF |

### Tier 3 — International / Thematic

| # | Symbol | Instrument | Why | IBKR Type |
|---|--------|-----------|-----|-----------|
| 9 | **EEM** | Emerging Markets | Different cycle, diversification from US | ETF |
| 10 | **SMH** | Semiconductor ETF | AI/tech secular trend, high beta, strong momentum | ETF |

**Note**: Can trade either ETFs (simpler, fractional shares possible) or Micro Futures 
(leverage, nearly 24hr access, no PDT rule). Start with ETFs for simplicity.

---

## STRATEGY DESIGN

### Timeframes
- **Primary**: 4hr chart (entry decisions, trend structure)
- **Confirmation**: Daily chart (trend direction, support/resistance)
- **Entry timing**: 1hr chart (pullback entries within 4hr structure)

### Entry Logic (Tori Trades inspired)

```
TREND CHECK (Daily):
  - Price above 20 EMA = uptrend bias (favor longs)
  - Price below 20 EMA = downtrend bias (favor shorts)
  - 20 EMA slope confirms direction

SETUP (4hr):
  - Range expansion: 4hr ATR(14) > 1.2x average ATR
  - Volume confirmation: volume > 1.2x 20-period average
  - Structure: higher high + higher low (uptrend) or lower low + lower high (downtrend)
  - Pullback to key level: price retraces to 20 EMA or prior breakout level

ENTRY TRIGGER (1hr):
  - Reversal candle at support/resistance (hammer, engulfing, pin bar)
  - OR break of 1hr consolidation in trend direction
  - Spread and liquidity check at entry

CONVICTION FILTER:
  - VIX level (low VIX = trend-friendly, high VIX = choppy/mean-revert)
  - Sector rotation confirmation (is money flowing into this sector?)
  - No major earnings/FOMC within 24hr of entry
```

### Exit Logic

```
PRIMARY EXIT (Trailing Stop):
  - Initial stop: 1.5x ATR(14) on 4hr
  - After +1R: move stop to breakeven
  - After +2R: trail at 1x ATR below/above price
  - Let winners run until trail is hit

SECONDARY EXIT (Time-based):
  - Max hold: 5 trading days (25 4hr bars)
  - If no movement after 2 days: tighten stop to 0.5x ATR

TARGET (optional, for partial exits):
  - Partial close at 2R (take 50% off)
  - Let remainder trail
  - Full close at prior swing high/low

HARD STOP:
  - 2x ATR from entry (catastrophic protection)
  - Never risk more than 1% of portfolio per trade
```

### Position Sizing

```
Risk per trade: 0.5-1.0% of portfolio
Position size = (Portfolio × Risk%) / (Entry - Stop)

Example:
  Portfolio: $10,000
  Risk: 1% = $100
  SPY entry: $520, stop: $515 (5 points = $5/share)
  Size: $100 / $5 = 20 shares

Max concurrent positions: 5 (across all 10 instruments)
Max same-sector: 2
Max correlation group: 2 (e.g., SPY + QQQ count as same group)
```

---

## DATA REQUIREMENTS

### Historical Data for Backtesting
- **Minimum**: 2 years of 1hr bars per instrument (for 4hr aggregation)
- **Ideal**: 5 years (captures multiple regimes: bull, bear, sideways, crash)
- **Source**: IBKR historical data API (same as Argus uses)
- **Storage**: CSV files in `helio/data/`

### Live Data
- **4hr bars**: aggregated from 1hr feed (same pattern as Argus MTF)
- **Daily bars**: end-of-day aggregation
- **Volume**: critical for this strategy (unlike FX where volume is indicative)
- **VIX**: real-time for conviction filter

---

## INFRASTRUCTURE REUSE FROM ARGUS

| Component | Reuse | Adaptation Needed |
|-----------|-------|-------------------|
| IBKR connection (ib_insync) | 100% | Different contract types (STK vs CASH) |
| BarBuffer + MTF aggregation | 90% | Add 4hr as primary instead of 1min |
| Risk management framework | 80% | Daily risk instead of intraday |
| Promotion pipeline concept | 70% | Longer evaluation periods (weeks not days) |
| Dashboard framework | 60% | Different metrics (daily PnL, swing duration) |
| Backtest engine | 70% | Daily/4hr bars instead of 1min replay |
| State persistence | 100% | Same atomic write pattern |
| Governance / staging | 80% | Longer residency periods |
| Discord alerts | 100% | Same webhook |

### What's Genuinely New
- 4hr/daily trend detection (EMA slope, structure analysis)
- Pullback entry detector (support/resistance + candle patterns)
- Multi-day position management (overnight holds, gap handling)
- VIX / sector rotation filters
- Earnings calendar integration
- ETF-specific contract handling
- Daily PnL tracking (vs intraday)

---

## PROJECT STRUCTURE

```
C:\Helio\
├── repo\
│   ├── helio\
│   │   ├── runner.py            # Main runner (4hr evaluation cycle)
│   │   ├── strategy.py          # Trend + pullback + entry logic
│   │   ├── risk.py              # Position sizing, portfolio heat
│   │   ├── indicators.py        # EMA, ATR, volume profile, structure
│   │   ├── state.py             # Position state, trade tracking
│   │   ├── schemas.py           # Trade/signal CSV schemas
│   │   ├── configs/             # Per-instrument configs
│   │   ├── backtest/
│   │   │   └── engine.py        # Daily/4hr replay engine
│   │   ├── ops/
│   │   │   ├── dashboard.py     # Swing-specific dashboard
│   │   │   ├── data_download.py # IBKR historical bar fetcher
│   │   │   └── promotion.py     # Promotion gates (adapted)
│   │   ├── data/                # Historical bars
│   │   └── logs/                # Runtime artifacts
│   ├── HELIO_MAP.md
│   └── requirements.txt
└── .venv\
```

---

## TIMELINE

### Week 1: Foundation
- [ ] Set up Helio repo structure
- [ ] Download 2yr daily + 1hr bars for all 10 instruments from IBKR
- [ ] Build 4hr bar aggregation from 1hr data
- [ ] Implement basic trend detection (EMA slope + structure)
- [ ] Run proof-of-concept: simple "buy above 20 EMA, sell below" on SPY daily

### Week 2: Strategy
- [ ] Build pullback entry detector
- [ ] Add ATR-scaled stops and trailing logic
- [ ] Implement multi-day position management
- [ ] Run full backtest on SPY + QQQ (2yr, daily bars)
- [ ] If PF > 1.3 → continue. If not → rethink entry logic.

### Week 3: Expansion
- [ ] Backtest all 10 instruments
- [ ] Add VIX filter and earnings calendar
- [ ] Build sector correlation guard
- [ ] Rank instruments by backtest PF
- [ ] Select top 5 for paper trading

### Week 4: Paper Trading
- [ ] Deploy paper runner on IBKR
- [ ] Dashboard with swing-specific metrics
- [ ] Daily PnL tracking + trade journal
- [ ] Let it run for 2-4 weeks collecting trades

### Month 2-3: Validation
- [ ] Accumulate 30+ swing trades
- [ ] Walk-forward validation
- [ ] Execution quality analysis
- [ ] If proven → first real capital allocation

---

## KEY DIFFERENCES FROM ARGUS

| Aspect | Argus (Intraday FX) | Helio (Swing Equities) |
|--------|--------------------|-----------------------|
| Timeframe | 1-minute bars, 45-75 min holds | 4hr bars, 1-5 day holds |
| Instruments | FX pairs (EURUSD, AUDJPY...) | Indices + ETFs (SPY, QQQ...) |
| Edge source | Session range expansion | Trend continuation on pullback |
| Timeout rate | 67% (by design) | Should be <30% (trailing stop) |
| Trades/day | 1-3 | 0-1 (might go days without trading) |
| Overnight risk | None (closes intraday) | Real (gaps at open) |
| Volume importance | Low (FX volume is indicative) | High (real volume confirms) |
| Capital efficiency | High (leverage, many trades) | Lower (larger stops, fewer trades) |
| Time to proof | 2-4 months (60 trades) | 2-3 months (30 swing trades) |

---

## RISK CONSIDERATIONS

1. **Overnight gaps**: SPY can gap 1-3% on surprise news. Stop placement must account for this.
2. **Earnings season**: Individual stocks gap 5-20% on earnings. ETFs are safer but still gap.
3. **FOMC/macro events**: Indices can reverse violently on rate decisions. Calendar filter essential.
4. **Correlation**: SPY + QQQ + IWM are 80%+ correlated. Max 2 index positions simultaneously.
5. **PDT rule**: If account < $25K, limited to 3 day trades per 5 days. Swing trades (held overnight) avoid this.
6. **Capital requirement**: $10K minimum for meaningful position sizing with proper risk management.

---

## DECISION GATES

Before building Helio:
- [x] Argus infrastructure proven and running
- [ ] Quick POC backtest on SPY daily bars shows PF > 1.3
- [ ] User confirms capital allocation plan (how much for Helio vs Argus vs SPY)
- [ ] IBKR account supports equity trading (likely already does)

Before deploying real capital:
- [ ] 30+ paper swing trades with PF > 1.3
- [ ] Max drawdown < 10% in paper
- [ ] Execution quality acceptable (slippage < 0.1% on ETF entries)
- [ ] Walk-forward validation across bull + bear + sideways regimes
