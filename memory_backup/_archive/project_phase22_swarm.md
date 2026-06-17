---
name: Phase 22 — Multi-Agent Intelligence & Adversarial Testing
description: Swarm intelligence, dual-model collaboration (Claude+OpenAI), external agent oversight, chaos engineering for trading bots
type: project
---

Phase 22: Multi-Agent Intelligence & Adversarial Testing (future, post Cascade validation)

## 22A — Swarm Intelligence Confluence (internal)

Inspired by MiroFish (github.com/666ghj/MiroFish) — simulate different "trader personas" as agents:
- Momentum chaser, mean-reversion, breakout trader, scalper, etc.
- Each agent has independent memory, bias, and strategy logic
- Agents vote on entry/exit signals → produce weighted consensus score
- Could replace or augment the current static confluence weighting

## 22B — External Risk Oversight Agent

A separate always-running process that sits ABOVE all bots:
- Monitors all bot logs (Argus Spot, Cascade, IBKR)
- Enforces portfolio-level risk limits across all systems
- Correlated drawdown detection (all bots losing = market-wide event → pause everything)
- Daily P&L aggregation across all strategies
- Kill switch that no individual bot can override

## 22C — Dual-Model Adversarial Collaboration (Claude + OpenAI)

Use Claude and GPT-4 as collaborating analysts, not competing ones:
- Same data, different analytical approaches
- **Strategy design decisions**: two models arguing with evidence
- **Backtest interpretation**: independent reads on "is this edge real or overfit?"
- **Architecture tradeoffs**: one catches failure modes the other misses
- **Parameter tuning**: adversarial review prevents confirmation bias

Architecture: shared file handoff or Streamlit/Gradio local GUI with parallel panels.
Both models respond to same prompt, can read each other's analysis, synthesize recommendations.

**When to use**: difficult/ambiguous decisions only. Not routine tasks.

**GUI options** (ranked by practicality):
1. Streamlit app — local web UI, both models respond in parallel panels (best fit)
2. Custom GPT with shared folder access
3. Web dashboard with dual model integration

## 22D — Chaos Engineering / Adversarial Data Testing

Inject poisoned/adversarial data to test bot resilience:

**Data injection tests:**
- Stale/delayed price feeds (feed freezes, replays old data)
- Flash crash injection (sudden 10-20% spike/crash)
- Manipulated order book data (fake walls, spoofing simulation)
- Missing data gaps (candles stop arriving for N minutes)
- Contradictory signals (all indicators disagree simultaneously)
- Timestamp manipulation (out-of-order, duplicate, future-dated)
- Volume anomalies (sudden 100x volume spike with no price move)

**Behavioral tests:**
- Does the bot halt gracefully or keep trading on stale data?
- Does it detect impossible price moves and reject them?
- Does it handle feed reconnection without duplicate orders?
- Does the kill switch work under all failure modes?
- Can it recover state after crash during any lifecycle phase?

**Goal:** Prove the bot fails SAFELY — not that it never fails.

## Priority / Sequencing

- 22D (chaos testing) can start earliest — doesn't need multi-agent infra
- 22B (risk oversight) needed once 2+ live strategies running
- 22C (dual-model) experiment when facing difficult design decisions
- 22A (swarm) last — most complex, needs proven base strategies first

## Data sources for external agents

- **CoinGlass** (free): funding rates, OI, liquidation data → Cascade flow confirmation
- **Fear & Greed Index** (free): simple regime overlay for Argus Spot
- **LunarCrush / Santiment**: social sentiment (paid, evaluate later)
- **TradingView webhooks**: alert on technical conditions (free tier)

## MiroFish reference
Python + Vue.js, OASIS simulation framework (CAMEL-AI), AGPL-3.0 licensed. Uses LLM for agent reasoning + Zep Cloud for agent memory. Agent simulation architecture is the valuable concept, not their specific implementation.
