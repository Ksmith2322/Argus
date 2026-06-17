---
name: Helio Bot Fleet Roadmap
description: Full bot fleet plan under Helio umbrella — Argus, Hermes, IBKR bots, penny stock scanner, TQQQ revenge trade, Polymarket, and more. Prioritized by ROI and deployment speed.
type: project
---

## Helio Bot Fleet — Master Plan

Parent brand: **Helio**
Goal: Multiple income streams across uncorrelated markets. Not one bot — a fleet.
User target: $500 → $100K in <1 year via compounding + diversification.

### CORRECTED PRIORITY (2026-03-19)
Primary focus: **Argus Cascade** (BTC/ETH perp forced-flow engine). See [project_argus_cascade.md](project_argus_cascade.md) for full spec.
Argus Spot runs passively — no new features. Cascade gets primary attention.
Other bots (IBKR FX, Hermes, etc.) are DEFERRED — low-friction deployment ≠ high-value deployment.
Only deploy what increases expected return per unit of attention.

---

## TIER 1 — DEPLOY FIRST (Argus infra reuse, weeks not months)

### 1. Argus (Crypto Spot — ETH/BTC)
- **Status:** RUNNING, paper trading, waiting for fills
- **Edge:** Confluence scoring + ML governor + OB imbalance + BTC lag
- **Priority:** Already deployed — validate, then go live
- **Expected ROI:** 2-5% monthly if PF ≥ 1.20 holds
- **Next:** Wait for 100+ paper trades, validate, then real money ($500 start)

### 2. Hermes (Crypto Fast Scalper)
- **Status:** Idea phase
- **Edge:** Sub-5min entries, WebSocket feed, momentum + OB imbalance, fast exit on reversal
- **What it needs:**
  - WebSocket feed (feed_ws.py already written, needs DNS fix or alternative)
  - New feature set tuned for 1-5 min holds (tick momentum, micro-regime, spread dynamics)
  - Separate ML model trained on short-duration outcomes
  - Tighter stop-losses (0.3-0.5% vs Argus's wider bands)
- **Reuses from Argus:** Config system, state management, recovery, journaling, reconciliation, dashboard, OB imbalance
- **Risk:** Fees eat edge on small moves — needs >55% WR at 60bps round-trip
- **Priority:** Phase 25 — after Argus is proven profitable. Don't split focus before then.
- **Deploy time:** 2-3 weeks once started (heavy Argus reuse)

### 3. IBKR FX Bot (Forex via Interactive Brokers)
- **Status:** Code BUILT (feed_ibkr.py + ibkr_adapter.py), waiting on account approval (DUP472829)
- **Edge:** Macro regime + session timing (London/NY overlap), carry trades, mean reversion on majors
- **Pairs to start:** EUR/USD, GBP/USD (most liquid, tightest spreads)
- **What it needs:** Account approval → IB Gateway → test connectivity → paper trade
- **Reuses from Argus:** Entire engine (regime, confluence, governor, journaling)
- **Priority:** HIGH — account could approve any day. Different market = real diversification.
- **Deploy time:** Days once account approved (code is ready)

### 4. IBKR Short Seller Bot
- **Status:** Pending same IBKR account
- **Edge:** Mean-reversion shorts on overextended stocks, earnings fade plays
- **Strategy:** Scan for stocks up >20% on no news or weak catalysts → short with tight stop
- **What it needs:** Stock screener integration, borrow availability check, hard stop-losses (shorts have unlimited loss)
- **Risk:** Short squeezes — MUST have strict position limits and stops
- **Priority:** MEDIUM — after FX bot is validated. Same account, different strategy.
- **Deploy time:** 1-2 weeks after FX is running

---

## TIER 2 — HIGH POTENTIAL, MORE BUILD (1-2 months)

### 5. Penny Stock Explosion Predictor ("Apollo")
- **Status:** Idea phase — user wants to reverse-engineer WHY penny stocks explode
- **Concept:** Not day-trading pennies (PDT trap). Instead:
  - Scan SEC filings, social media buzz, insider buying, float changes, volume anomalies
  - Build ML classifier: "Will this stock 5x in the next 30-90 days?"
  - Features to reverse-engineer from historical explosions:
    - Float size (< 10M shares)
    - Sudden insider buying clusters
    - SEC filing patterns (S-1 withdrawals, 8-K catalysts)
    - Social sentiment spike (Reddit/Twitter mentions vs baseline)
    - Volume surge vs 90-day average (>5x = signal)
    - Sector momentum (biotech FDA calendar, EV hype cycles)
    - Short interest > 20% float (squeeze setup)
    - Price consolidation near 52-week low with rising volume
  - Buy small positions ($50-200) in 10-20 candidates, hold 30-90 days
  - No PDT issue — holding period is weeks/months, not intraday
- **Data sources:** SEC EDGAR API (free), Yahoo Finance, social sentiment APIs
- **Edge:** Most retail buyers chase AFTER the move. This bot identifies setups BEFORE.
- **Risk:** Penny stocks are volatile and many go to zero. Position sizing critical (1-2% of portfolio per pick).
- **Priority:** HIGH — completely uncorrelated with crypto, different time horizon
- **Deploy time:** 3-4 weeks for v1 scanner + ML model

### 6. TQQQ Momentum Bot ("Ares")
- **Status:** Idea phase — user has personal history with TQQQ, wants revenge trade
- **Concept:** TQQQ is 3x leveraged Nasdaq. It's a beast both ways.
- **Strategy:**
  - **Requires $25K+ account** (PDT rule for day trading) — deploy once capital milestone hit
  - Regime-based: ONLY long TQQQ in confirmed uptrends, SQQQ (inverse) or cash in downtrends
  - Use Nasdaq breadth, VIX level, put/call ratio, Fed calendar as regime signals
  - Hold 1-5 days (swing, not scalp) to reduce PDT trigger frequency
  - Scale in: 25% position on signal → add 25% on confirmation → trail stop
- **Edge:** Most people buy TQQQ and hold through drawdowns (decay kills them). Bot exits on regime shift.
- **Risk:** 3x leverage = 3x drawdowns. VIX spike can gap through stops.
- **Priority:** MEDIUM-HIGH — needs $25K milestone first. Build the model now, deploy when capital is there.
- **Deploy time:** 2 weeks (simple regime model, IBKR execution already built)

---

## TIER 3 — OPPORTUNISTIC (when platform access unlocks)

### 7. Polymarket Prediction Bot ("Oracle")
- **Status:** Waitlisted (#1,069,687)
- **Edge:** Probability mispricing — when market says 70% but true probability is 85%
- **Strategy:**
  - Track prediction markets for mispriced events
  - Use base rate analysis, polling data, historical patterns
  - Buy underpriced YES/NO contracts, sell as price corrects toward true probability
  - Focus on recurring event types where you can build an edge (elections, sports, crypto milestones)
- **What it needs:** Polymarket API access, event categorization, probability model
- **Priority:** LOW urgency (waitlist), HIGH potential (prediction markets are inefficient)
- **Deploy time:** 2-3 weeks once access granted

### 8. Crypto Funding Rate Arbitrage ("Atlas")
- **Status:** Idea phase
- **Edge:** When perpetual futures funding rate is extreme (>0.1% per 8h), the rate mean-reverts
  - High funding → short perps + long spot = collect funding while delta-neutral
  - Negative funding → long perps + short spot (if borrowable) = same play in reverse
- **What it needs:** Perps exchange account (Bybit, dYdX, or similar — not Coinbase)
- **Risk:** Low — delta-neutral by design. Main risk is liquidation on the perps leg during volatility.
- **Priority:** MEDIUM — steady income, low risk, but needs perps exchange account
- **Deploy time:** 1-2 weeks

### 9. Grid Bot ("Hephaestus")
- **Status:** Idea phase
- **Edge:** No prediction needed. Places buy/sell orders at fixed intervals. Profits from ANY price movement within a range.
- **Best for:** Range-bound markets (which is most of crypto most of the time — and exactly what Argus is sitting through right now doing nothing)
- **Synergy:** Could run on the same pairs Argus trades, during periods where Argus's regime detector says RANGE
- **Priority:** MEDIUM — low risk, consistent small returns, good complement to Argus
- **Deploy time:** 1 week (simple logic, Coinbase execution already built)

---

## TIER 4 — FUTURE VISION (Phase 30+)

### 10. Swarm Intelligence ("MiroFish" / Phase 22)
- Multi-agent trader persona simulation for consensus scoring
- Already documented in project_phase22_swarm.md

### 11. Commodities Bot (Gold, Oil via IBKR)
- Same engine as FX bot, different instruments
- Deploy after FX proves the IBKR pipeline works

### 12. Options Bot (after $25K capital)
- Theta decay selling (covered calls, cash-secured puts)
- Consistent income strategy, pairs with TQQQ holdings

---

## PRIORITY MATRIX

| Bot | ROI Potential | Deploy Speed | Capital Needed | Diversification Value | PRIORITY SCORE |
|-----|--------------|--------------|----------------|----------------------|----------------|
| Argus (running) | Medium | Done | $500 | Baseline | — |
| IBKR FX | High | Days | $1000 | HIGH (new market) | **#1** |
| Grid Bot | Low-Med | 1 week | $500 | Medium (same market, diff strategy) | **#2** |
| Penny Stock (Apollo) | Very High | 3-4 weeks | $500-2000 | VERY HIGH (equities) | **#3** |
| Funding Rate (Atlas) | Medium | 1-2 weeks | $1000 | Medium (crypto, diff strategy) | **#4** |
| Hermes (scalper) | Medium | 2-3 weeks | $500 | Low (same market) | **#5** |
| IBKR Shorts | High | 2 weeks | $2000 | HIGH (inverse exposure) | **#6** |
| TQQQ (Ares) | Very High | 2 weeks | $25,000 | HIGH (equities/leverage) | **#7** (capital-gated) |
| Polymarket (Oracle) | High | 2-3 weeks | $500 | VERY HIGH (prediction) | **#8** (access-gated) |

---

## Naming Convention (Greek Mythology / Helio Theme)

| Bot | Name | Why |
|-----|------|-----|
| Crypto confluence | **Argus** | All-seeing giant (many indicators) |
| Crypto scalper | **Hermes** | God of speed and commerce |
| Penny stock predictor | **Apollo** | God of prophecy and foresight |
| TQQQ momentum | **Ares** | God of war (revenge trade energy) |
| Prediction markets | **Oracle** | Self-explanatory |
| Funding rate arb | **Atlas** | Carries weight (delta-neutral, steady) |
| Grid bot | **Hephaestus** | God of craft (mechanical, no prediction) |
| FX/Shorts | Uses Argus engine via IBKR adapter | Same brain, different market |
| Swarm intelligence | **MiroFish** | Already named (Phase 22) |

---

## Key Principles
1. **Every bot must have a quantifiable edge** — no bot just for the sake of it
2. **Uncorrelated markets > more bots in same market** — crypto crash shouldn't kill everything
3. **Reuse Argus infra** — config, state, recovery, journaling, dashboard are shared
4. **Position size for survival** — no single bot should risk >5% of total capital
5. **Validate before scaling** — paper trade → small real → scale up. Every time.
