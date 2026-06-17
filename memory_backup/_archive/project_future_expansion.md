---
name: Future Expansion & Market Strategy
description: User's vision for Argus beyond crypto — watchlist onboarding, new markets (Polymarket, commodities, forex), Helio umbrella brand, and bot-as-a-service ideas
type: project
---

## Watchlist & Auto-Onboarding Pipeline (Phase 25+)

User wants a systematic coin pipeline:
1. **Screening layer** — define base criteria (volume, volatility, performance thresholds) for a coin to enter the watchlist
2. **Watchlist** — coins being passively monitored, data collected
3. **Backtest gate** — one-click backtest on watchlist coins, auto-evaluate PF/WR/drawdown against thresholds
4. **Onboarding** — if backtest passes, coin moves to live trading rotation with reserved capital ($500 default, adjustable up/down)
5. **Easy add/remove** — dashboard UI to promote/demote coins between tiers

**Why:** Scales the system beyond manually adding ETH/BTC/SOL. Makes coin selection data-driven.
**How to apply:** Design the watchlist as a JSON config with tiers (watch → monitor → trade). Build screening criteria into a reusable scorer.

## New Market Expansion Ideas

### Polymarket (Prediction Markets)
- User sees others making money with bots on Polymarket
- 24/7 market, binary outcomes, different strategy than price trading
- Would need: Polymarket API integration, event-driven strategy (not price-action), liquidity analysis
- **Priority:** User wants to "jump in the game" — explore after crypto engine is stable

### Commodities / Forex
- 24/7 or near-24/7 markets (forex Sun-Fri, crypto 24/7)
- Engine architecture (candles → indicators → confluence → decisions) transfers well
- Would need: new data feeds, different volatility profiles, adjusted parameters

### Helio Umbrella Brand
- User envisions **Helio** as the parent product/brand
- **Argus** becomes the crypto trading vertical under Helio
- Each new market gets its own vertical but shares the core engine
- Architecture: shared engine core + market-specific adapters (feed, execution, strategy tuning)

**Why:** The engine's architecture (config-driven, modular subsystems, ML governor) is market-agnostic. Expanding to new markets reuses 80%+ of the codebase.
**How to apply:** When refactoring, keep market-specific logic isolated in adapters. Design new features as market-agnostic where possible.

## Timing
All expansion is POST-Phase 20 (governor GATE + compounding proven profitable). User explicitly said "not until we are set with where we need to be."
