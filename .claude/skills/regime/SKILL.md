---
name: regime
description: Show current market regime for all active pairs and which strategy family is prioritized.
allowed-tools: Bash Read
---

Show current market regime across all instruments:

1. **Read Helio heartbeats** from `helio/logs/*/heartbeat.json` — extract regime and regime_confidence
2. **Read Argus features** from `argus_flow/logs/*/heartbeat.json` — extract regime info if available
3. **Run regime router** if possible: `python -c "from helio.regime_router import classify; ..."`

For each instrument show: Symbol | Regime | Confidence | Priority Strategy | Current Position

Regime types: TRENDING_UP, TRENDING_DOWN, RANGING, BREAKOUT, VOLATILE
Priority mapping: TRENDING -> Helio, RANGING -> Apollo, BREAKOUT -> Hermes, VOLATILE -> reduce exposure
