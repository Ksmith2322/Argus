# Atlas --- Global Macro Event Intelligence

**Created:** 2026-04-11
**Status:** Design complete, ready for Phase 1 build
**Role:** Fleet governor + situational awareness layer (NOT a standalone trading system)

## TL;DR

Atlas watches the world and tells the fleet what's coming. It detects macro events, classifies them, maps their cascade through asset classes in waves, and publishes a regime state that other systems read before trading. It is a **filter and risk governor**, not a trader.

Three agents debated this (macro economist, quant strategist, data engineer). All agreed:

- **Atlas should NOT be a standalone trading strategy.** Retail has no speed edge on macro events.
- **Atlas should BE a risk governor** that reduces drawdowns by telling other systems when to be aggressive vs defensive.
- **The cascade model (Wave 1-4) is real** and the most unique part of this system.
- **Build the event database + impact matrix first.** Everything else depends on this foundation.
- **Free data is sufficient.** RSS + GDELT + FRED + yfinance = $0-9/month total.
- **Start simple (keyword classifier), get smarter later (FinBERT, Claude API).**

---

## What Atlas Does

```
World Event --> Detect --> Classify --> Cascade Forecast --> Fleet Signal
                 |            |              |                    |
              RSS/GDELT    Keyword/NLP   Impact Matrix     macro_regime.json
              2-min poll   Severity 0-1   Wave 1/2/3/4     Other systems read
```

### The Cascade Model

Every macro event ripples through markets in waves:

```
Event: "OPEC announces 2M barrel/day production cut"

  Wave 1 (minutes-hours) -- Direct, liquid instruments
    USO +3-8%, Brent futures, VIX up
    
  Wave 2 (hours-days) -- Related assets, institutional repositioning  
    XLE +2-5%, Airlines -3-5%, CAD/NOK strengthen
    
  Wave 3 (days-weeks) -- Second-order effects, narrative shift
    CPI expectations reprice, TLT sells off, consumer discretionary weakens
    
  Wave 4 (weeks-months) -- Structural, real economy catches up
    Fed policy response, earnings estimate revisions, sector rotation
```

**Why waves exist:** Different participants process information at different speeds.
- Wave 1: Algos and HFT (milliseconds). We can't compete here.
- Wave 2: Institutional PMs (hours-days, need meetings/approvals). This is where we start.
- Wave 3: Analysts revise models, implications become clear. This is our sweet spot.
- Wave 4: Real economy adjusts. Too slow for active trading, but informs regime.

**Atlas targets Wave 2-3.** Wave 1 is for HFT. Wave 4 is too slow.

---

## How Atlas Governs the Fleet

Atlas publishes `forge/data/macro_regime.json` that other systems read:

```json
{
  "timestamp": "2026-04-11T14:30:00Z",
  "regime": {
    "overall": "RISK_OFF",
    "rates": "TIGHTENING",
    "vol": "HIGH",
    "growth": "SLOWING",
    "liquidity": "CONTRACTING"
  },
  "alert_level": "elevated",
  "active_events": [
    {
      "type": "TARIFF_ESCALATION",
      "severity": 0.7,
      "detected_at": "2026-04-11T14:00:00Z",
      "cascade_status": "wave_2_active"
    }
  ],
  "fleet_guidance": {
    "position_size_modifier": 0.5,
    "sectors_avoid": ["XLI", "EEM"],
    "sectors_favor": ["XLU", "GLD"],
    "fx_bias": "USD_STRONG"
  }
}
```

**Integration points (future, NOT during burn-in):**
- **Argus FX:** "USD strength event --> pause GBPUSD shorts, favor USDJPY longs"
- **Titan stocks:** "Tariff escalation --> avoid industrials, favor domestic"
- **Apollo earnings:** "High VIX regime --> widen stops, reduce size"
- **GDX/GLD pairs:** "Gold shock --> pause pairs trade until Wave 1 settles"
- **All systems:** position_size_modifier scales risk globally

---

## Event Taxonomy (10 Categories)

### 1. Monetary Policy
| Event | Wave 1 | Wave 2 | Wave 3 | Magnitude | Frequency |
|-------|--------|--------|--------|-----------|-----------|
| FOMC surprise (rate) | 2Y yield, EUR/USD, SPY | XLF, TLT, QQQ | Sector rotation, EM flows | Medium-Large | 8/yr |
| Fed Chair tone shift | Same delayed 30min | Same | Same | Small-Medium | 8/yr |
| Dot plot shift | 2Y/10Y spread, TLT | QQQ, XLU, XLF | Mortgage rates, housing | Medium | 4/yr |
| Emergency cut/hike | Everything simultaneously | Credit markets, bank stocks | Global CB response | Extreme | ~1/decade |
| QE/QT change | TLT, MBS | SPY, QQQ | Credit spreads, EM bonds | Large | Rare |
| ECB rate surprise | EUR/USD, Bund yields | European banks, EuroStoxx | DXY inverse, EEM | Medium | 6-8/yr |
| BOJ YCC/rate change | USD/JPY (2-4%), JGB | Nikkei, global bonds | AUD/JPY carry unwind | Large-Extreme | Rare |
| PBOC RRR/MLF cut | USD/CNH, FXI | Copper, AUD/USD | EEM, XLI | Medium | 4-6/yr |

### 2. Oil / Energy / Commodities
| Event | Wave 1 | Wave 2 | Wave 3 | Magnitude |
|-------|--------|--------|--------|-----------|
| OPEC surprise cut | USO +3-8% | XLE +2-5%, airlines down | CPI repricing, TLT down | Large |
| Middle East supply disruption | USO +5-15%, VIX spike | XLE up, defense up, airlines crushed | SPY down if sustained, gold up | Large-Extreme |
| Natural gas shock | UNG +5-20% | XLU, European gas | Fertilizer, agriculture | Medium-Extreme |
| Crop failure | DBA, specific soft commodities | Food companies, EM food importers | Inflation expectations | Small-Medium |

### 3. Trade Policy / Tariffs
| Event | Wave 1 | Wave 2 | Wave 3 | Magnitude |
|-------|--------|--------|--------|-----------|
| Tariff announcement (US-China) | FXI -3-8%, USD/CNH | SPY/QQQ, semis, supply chain | EEM, commodities, bond rally | Medium-Large |
| Tariff de-escalation | FXI +3-8%, risk-on | SPY rally, EEM rally | VIX crush, TLT sells off | Medium-Large |
| Comprehensive sanctions | Target country assets collapse | Target commodity spikes | Trade flow rerouting | Extreme for target |
| Export controls (tech) | Targeted companies (ASML, NVDA) | Semiconductor index | Capex cycles | Medium |

### 4. Geopolitical / War
| Event | Wave 1 | Wave 2 | Wave 3 | Magnitude |
|-------|--------|--------|--------|-----------|
| Major conflict onset | USO +5-20%, gold +2-5%, VIX +30-80% | Defense up, SPY -3-10%, TLT up | Sanctions cascade, EM food crisis | Large-Extreme |
| Terror attack (major) | VIX extreme, SPY circuit breaker, gold up | Airlines, insurance destroyed; defense up | Fed emergency response | Extreme |
| Taiwan crisis | TSMC/semis collapse, FXI collapse, VIX extreme | Every equity market globally | Complete supply chain rerouting | Extreme |

### 5. Elections / Political
| Event | Wave 1 | Wave 2 | Wave 3 | Magnitude |
|-------|--------|--------|--------|-----------|
| US presidential surprise | SPY +/-2-5%, VIX crush | Sector rotation (XLE, XLV, XLF) | Tax/fiscal policy repricing | Medium |
| Foreign election surprise | Target currency, equities | Trade partners | Limited global | Small-Medium |

### 6. Pandemic / Black Swan
| Event | Wave 1 | Wave 2 | Wave 3 | Magnitude |
|-------|--------|--------|--------|-----------|
| Pandemic declaration | VIX 40-80, SPY -20-35% | TLT up then down, HYG collapse | Fed emergency QE, fiscal stimulus | Extreme |
| Major natural disaster | Target country assets | Supply chain disruption | Reconstruction spending | Medium |

### 7. Currency Crises
| Event | Wave 1 | Wave 2 | Wave 3 | Magnitude |
|-------|--------|--------|--------|-----------|
| EM currency collapse | Target -10-50% | EEM selloff, carry unwind | DXY up, gold up | Large for target |
| Major peg break | Target pair +/-30% | FX broker stress, carry unwind | Broader risk-off | Extreme for target |

### 8. Banking / Financial
| Event | Wave 1 | Wave 2 | Wave 3 | Magnitude |
|-------|--------|--------|--------|-----------|
| Major bank failure | XLF -5-15%, KRE -20-30% | TLT up, HYG widens, SPY -3-5% | Fed response, credit tightening | Large |
| Credit market freeze | HYG collapse, money markets break | Everything sells (even TLT initially) | Fed emergency facilities | Extreme |

### 9. Tech / Regulatory
| Event | Wave 1 | Wave 2 | Wave 3 | Magnitude |
|-------|--------|--------|--------|-----------|
| Antitrust vs Big Tech | Target -3-10% | QQQ, XLK | Competitor benefit | Medium |
| AI regulation | AI stocks (NVDA, MSFT) | QQQ if significant | Capex changes | Small-Medium |

### 10. Earnings Season (Aggregate)
| Event | Wave 1 | Wave 2 | Wave 3 | Magnitude |
|-------|--------|--------|--------|-----------|
| Broad beat/miss rate | SPY/QQQ repricing | Sector rotation | Credit repricing | Small-Medium |
| Mega-cap surprise | Target +/-5-15%, QQQ +/-1-3% | Sector peers, supply chain | Broad index | Medium-Large |

---

## The Connection Map (What Impacts What)

### Key Relationships (Tier 1 -- High Confidence, Well-Documented)

| Relationship | Direction | Strength | Sample Size | Tradeable? |
|-------------|-----------|----------|-------------|------------|
| Fed surprise component --> rates, FX, equities | Directional | Very Strong | 200+ FOMC meetings | Wave 2-3 only |
| VIX >30 mean reversion (SPY positive 1-3mo) | Long SPY | ~85% hit rate | 50+ spikes since 1990 | **YES -- best standalone** |
| Credit (HYG) leads equity by 1-5 days | Early warning | ~70% | Continuous | As filter, not trade |
| DXY up = EEM down | Inverse | -0.60 to -0.75 corr | Continuous | YES as hedge |
| Yield curve steepening --> XLF outperforms | Long XLF vs SPY | ~75% when >50bp move | 30+ episodes | YES |
| Oil supply shock --> USO/XLE momentum 2-4 weeks | Long energy | ~70% | 20+ shocks | YES |
| Post-election VIX crush | Short vol | 8/8 presidential elections | Small but perfect | YES, seasonal |

### Key Relationships (Tier 2 -- Reliable but Harder)

| Relationship | Notes |
|-------------|-------|
| Tariff escalation --> FXI directional | Reliable but timing impossible for retail |
| BOJ surprise --> USD/JPY | Large moves but rare (2-3/decade) |
| Earnings season aggregate momentum | Small edge (1-2% over 5 weeks) |

### What Looks Real But Is Noise

| Myth | Reality |
|------|---------|
| "Gold is an inflation hedge" | Gold responds to REAL yields, not CPI. In 2022, CPI=9% and gold went DOWN |
| "War is bad for stocks" | Initial drop real, but V-recovery within weeks in most cases |
| "Strong dollar is bad for US stocks" | Weak, regime-dependent correlation |
| "Copper predicts the economy" | Since ~2010, copper mostly tells you about Chinese construction |
| "Election party predicts market direction" | 1-year SPY returns are indistinguishable by party |

---

## Quantitative Framework

### Statistical Methodology

**Event study approach:**
1. Estimate "normal" returns from [-250, -10] days before event (market model)
2. Compute abnormal return: AR(t) = R(t) - E[R(t)]
3. Cumulate: CAR(t1, t2) = sum of AR over window
4. Test significance: bootstrap test preferred (no distributional assumptions)

**Key time windows:**
| Window | What It Captures | Tradeable? |
|--------|-----------------|------------|
| CAR(-1, 0) | Leak + immediate reaction | No -- too fast |
| CAR(0, 0) | Day-of reaction | No -- too fast |
| CAR(1, 5) | Post-reaction drift | **YES -- our target** |
| CAR(1, 20) | Multi-week cascade | **YES -- secondary** |
| CAR(0, 5) | Full first-week impact | Measurement only |

**Critical insight:** CAR(1,5) and CAR(1,20) are where retail has any chance. The day-of move (Wave 1) is captured by HFT. The drift (Wave 2-3) is where analytical edge matters more than speed.

### Minimum Sample Sizes

| Effect Size | Required N | Example |
|------------|-----------|---------|
| 0.8 (huge -- 9/11 scale) | 26 | Won't have enough events |
| 0.5 (large -- FOMC surprise) | 64 | FOMC: yes. War: no |
| 0.3 (medium -- tariff) | 176 | Need 20+ years |
| 0.2 (small -- routine data) | 394 | Basically impossible |

**Rule: <30 events in a category = exploratory only, not tradeable.**

### Avoiding Self-Deception (Non-Negotiable)

| Risk | Severity | Mitigation |
|------|----------|------------|
| Hindsight bias in event classification | HIGH | Pre-define categories BEFORE looking at outcomes. Use objective definitions ("Fed cuts >25bp" not "Fed makes dovish move") |
| Survivorship bias in event selection | HIGH | Use systematic sources (every FOMC, every NFP, not just dramatic ones). Include non-events as negative samples |
| Multiple comparisons (25 ETFs x 10 types x 5 windows = 1250 tests) | CRITICAL | Apply Benjamini-Hochberg FDR at 5%. Pre-register primary hypotheses |
| Regime change | MEDIUM-HIGH | Rolling 10-year window. Only trade signals stable across 3+ sub-periods |
| Lookahead bias | CRITICAL | Three-way split: Train 2000-2015, Validate 2016-2018, Test 2019-2026. ONE look at test set |
| Publication bias | MEDIUM | Replicate any academic finding on your own data before trusting it |

### Model Approach (Build Order)

1. **Lookup table first** (Week 1) -- Event type --> hardcoded expected impacts. Simple, transparent, auditable. This is the baseline.
2. **Bayesian updating** (Week 3+) -- Initialize priors from lookup table, update as new events occur. Handles regime adaptation naturally.
3. **Regression** (Later) -- Add severity/surprise weighting. Keep features low (5-8 max).
4. **NLP** (Much later, if ever) -- Automate event detection. Not a modeling priority.

The lookup table captures 80% of whatever edge exists. Don't over-engineer.

### Backtesting Framework

```
Train (2000-2015): Build event-->impact map
Validate (2016-2018): Tune parameters, ONE iteration
Test (2019-2026): Final evaluation, touch ONCE
Paper trade (2026+): True out-of-sample
```

**Walk-forward is non-negotiable.** At each event, use ONLY data before that event. Predictions are append-only (never edit past predictions).

**Kill criteria:**
| Metric | Threshold |
|--------|-----------|
| Directional accuracy | >55% |
| High-confidence subset accuracy | >60% |
| Profit factor | >1.2 |
| OOS Sharpe (annualized) | >0.5 |

---

## Data Sources (Free/$0-9/month)

### Event Detection

| Source | Cost | Latency | Coverage | Priority |
|--------|------|---------|----------|----------|
| RSS feeds (Reuters, CNBC, MarketWatch, BBC, FT) | Free | 1-5 min | Major events | **Phase 1** |
| Google News RSS (keyword feeds) | Free | 5-15 min | Broad | **Phase 1** |
| GDELT Project (global event database) | Free | 15 min | Everything worldwide | **Phase 2** |
| Reddit (r/worldnews, r/economics) | Free | Minutes | Sentiment/attention | Phase 2 |
| Wikipedia pageview spikes | Free | Hours | Crisis attention indicator | Phase 2 |
| Google Trends | Free | Hours | Rising search interest | Phase 2 |
| IFTTT Twitter relay (5-10 key accounts) | $3.49/mo | 1-15 min | @DeItaone, @FirstSquawk | Phase 2 |

### Economic Calendar / Scheduled Events

| Source | Cost | Notes |
|--------|------|-------|
| FRED API | Free | Best for US economic data, rate decisions |
| Fed website | Free | FOMC dates, minutes, dot plots |
| Investing.com economic calendar | Free (scrape) | Consensus estimates for CPI, NFP, etc. |
| ECB/BOJ/BOE websites | Free | Meeting dates |

### Market Data (Impact Measurement)

| Source | Cost | Notes |
|--------|------|-------|
| yfinance | Free | Daily OHLCV for all ETFs. Rate limit: ~2000 calls/hour |
| FRED | Free | Yield curves, credit spreads, VIX, economic indicators |
| Alpha Vantage | Free (500/day) | Backup for yfinance |

### Twitter/X Assessment

| Tier | Cost | Verdict |
|------|------|---------|
| Free | $0 | Useless for reading/monitoring |
| Basic | $100/mo | 10K reads/month. Enough for 20 accounts polled every 15 min |
| Pro | $5,000/mo | Absurd for personal project |

**Recommendation:** Skip Twitter API for MVP. Use IFTTT ($3.49/mo) to relay @DeItaone and @FirstSquawk to a local webhook in Phase 2. Build on RSS + GDELT first.

**Total cost: $0/month for Phase 1. $3-9/month for Phase 2+.**

---

## Architecture

### File Structure

```
forge/atlas/
    __init__.py
    runner.py                  # Main loop (entry point)
    sources/
        __init__.py
        rss_poller.py          # RSS feed polling
        gdelt_poller.py        # GDELT API (Phase 2)
        calendar_loader.py     # Economic calendar
        fred_loader.py         # FRED data
    classify/
        __init__.py
        keyword_rules.py       # Keyword-based classifier
        severity.py            # Severity scoring
    cascade/
        __init__.py
        templates.py           # Event --> asset cascade definitions
        tracker.py             # Active cascade tracking
    regime/
        __init__.py
        state.py               # Macro regime state machine
    impact/
        __init__.py
        measurement.py         # Historical impact computation
        matrix.py              # Impact matrix build/query
    db/
        __init__.py
        schema.py              # SQLite schema
        queries.py             # Common queries
```

### Storage

**SQLite** -- one file, zero infrastructure. Tables:
- `events` -- event_id, timestamp, type, category, severity, surprise, description, source
- `impacts` -- event_id, asset, window, raw_return, abnormal_return, standardized_ar
- `cascade_predictions` -- event_id, asset, wave, predicted_direction, predicted_car, actual_car

### Regime State Machine

Four dimensions, discrete states:

| Dimension | States | Primary Indicator |
|-----------|--------|-------------------|
| Rates | EASING / NEUTRAL / TIGHTENING | Fed funds futures vs current rate |
| Volatility | LOW (<15) / NORMAL (15-25) / HIGH (25-35) / CRISIS (>35) | VIX level |
| Growth | RECESSION / SLOWING / EXPANDING | ISM Manufacturing, yield curve |
| Liquidity | CONTRACTING / NEUTRAL / EXPANDING | Credit spreads (HYG-TLT), M2 |

Other systems read the regime and adjust:
- CRISIS vol + TIGHTENING rates = reduce all position sizes 50%
- LOW vol + EXPANDING growth = full size, favor risk assets
- SLOWING growth + EASING rates = favor bonds, defensive sectors

### Fleet Integration

- Heartbeat: `forge/logs/atlas/heartbeat.json` (same format as GDX/GLD)
- Fleet monitor: `forge_atlas` entry in SYSTEMS dict (auto-restart on stale)
- Paper monitor: Shows under "Forge (Paper)" section
- Output: `forge/data/macro_regime.json` read by other systems (future)
- Discord: Webhook alerts on severity >= 3 events

---

## Build Order

### Phase 1: MVP (Days 1-5) -- Prove the pipeline works

**Day 1-2: Data Ingestion + Storage**
- SQLite database with events/impacts schema
- RSS poller for 4-5 feeds (CNBC, Reuters, MarketWatch, BBC, FT)
- Google News RSS keyword feeds (FOMC, CPI, OPEC, tariff, war)
- Basic deduplication (title hash)
- Store raw headlines in events table

**Day 3: Classification + Regime**
- Keyword classifier (~200 lines, 20-30 rules mapping keywords to event types)
- Severity scorer (keyword-based: "war" = 0.8, "concern" = 0.3)
- Macro regime state from live data (VIX from yfinance, yield curve from FRED)
- Write macro_regime.json

**Day 4: Alerting + Cascade Templates**
- Discord webhook for severity >= 0.6 events
- Cascade templates for 5 core event types (Fed rate, CPI surprise, OPEC, tariff, geopolitical)
- When event classified, look up cascade template and include forecast in alert

**Day 5: Integration + Deploy**
- Heartbeat.json for fleet monitor
- Register in fleet_monitor.py and paper_monitor_status.py
- --loop mode (poll every 2 min)
- Scheduled events JSON (manually populate next 30 days)
- Deploy as background process

**After Day 5 you have:** A system that polls RSS every 2 minutes, classifies headlines, scores severity, writes regime state, alerts on Discord with cascade forecasts, and integrates with fleet monitoring. Cost: $0/month.

### Phase 2: Intelligence (Week 2) -- Historical foundation

- GDELT poller (15-min cycle, global coverage)
- Historical event backfill (manually curate 150-200 events from 2000-2026)
- Impact measurement pipeline (for each historical event, compute CARs across 25 ETFs)
- Build the impact matrix from measured data
- Validate cascade templates against actual historical data
- FinBERT sentiment scoring (local model, free)
- Google Trends keyword monitoring

### Phase 3: Hardening (Week 3) -- Make it robust

- Walk-forward backtest (train 2000-2015, validate 2016-2018, test 2019-2026)
- Kill/keep decision on each event-asset pair (FDR-corrected significance tests)
- Active cascade tracker (predicted vs actual, running scorecard)
- Bayesian updating model (priors from impact matrix, updates on new events)
- Reddit sentiment poller
- IFTTT Twitter integration for @DeItaone, @FirstSquawk ($3.49/mo)

### Phase 4: Fleet Governor (After burn-in) -- Wire it up

- Other systems read macro_regime.json before entering trades
- Position size modifier based on regime
- Sector filter for Titan
- Event calendar avoidance (reduce exposure 24h before FOMC/CPI)
- Paper trade Atlas's standalone signals (VIX mean-reversion after spikes)

---

## Where Retail Has Edge (Honest)

| Advantage | Why It Matters |
|-----------|---------------|
| Time horizon | No quarterly redemptions. Can hold Wave 3-4 trades through drawdowns that force funds to exit |
| No benchmark | Can sit in cash for months. Funds can't -- tracking error and career risk |
| Size irrelevance | $10K positions are invisible. Funds can't trade niche instruments without market impact |
| **As a filter** | The REAL edge: making Argus/Titan/Apollo smarter about WHEN to be aggressive vs defensive |

| Disadvantage | Accept It |
|-------------|-----------|
| Speed | Wave 1 is unwinnable. Don't try. |
| Data | Free data has lag. Accept 5-15 min latency. |
| Positioning info | CFTC reports are weekly with 3-day lag. Can't see real-time flows. |
| Execution | Wider spreads, worse fills. Size trades accordingly. |

---

## What Atlas is NOT

- NOT a high-frequency macro trading bot
- NOT competing with Bridgewater or Citadel
- NOT trying to predict every market move
- NOT replacing human judgment on geopolitical assessment
- NOT active during burn-in (will not influence live fleet until proven)

---

## Kill Criteria

If after Phase 3 backtest:
- Directional accuracy < 55% on high-confidence signals --> Kill standalone trades, keep as regime filter only
- No event-asset pairs survive FDR correction --> Simplify to VIX-based regime only
- Paper trading for 3 months shows no value-add to fleet P&L --> Demote to monitoring-only tool

---

## The Honest Tradeoff

The macro economist was blunt: "Most event-asset pairs will not show statistically significant post-day-1 drift after multiple comparison correction."

Atlas's real value is NOT in trading events. It's in telling your other systems: "The world just changed. Adjust accordingly." If it reduces fleet drawdowns by 20-40% during regime shifts, that's worth more than any individual macro trade.

The quant strategist added: "The lookup table captures 80% of whatever edge exists. Don't over-engineer."

The data engineer said: "Build the pipeline for $0/month. If it works, add Twitter for $3.49. If not, you learned something."

That's the plan. Start simple, measure everything, kill what doesn't work.
