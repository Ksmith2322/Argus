---
name: Risk indicator dashboard concept — future design
description: User floated idea 2026-04-23 for news/insider/event aggregation + risk heatmap + strategy suggestions. Sequenced as Tier-1 visualization of atlas/themis data, Tier-2 validation, Tier-3 gating (= event_trader layer).
type: project
originSessionId: 81afc97b-10bf-49fd-ba79-e5a89a7f400b
---
User concept 2026-04-23 (end of cleanup session).

## User's ask (verbatim framing)

"we should break the news and info we gather and have a risk indicator as we are gathering news, insider trades, global events, etc. just a way to see how we are scaling and tracking risk and trade strategy suggestion... a topic for all major movements and then a total overall markets/world or even country risk due to conflict"

## Clarification 2026-04-23 (IMPORTANT framing)

User clarified: **"not like a doomsday clock but health metric"** — positive, continuous framing. And: **"might not tie it to anything first just build it and see how it performs"** — pure observation mode, no trade-gating wire-up initially. Just a scoreboard.

**Why this matters**: "health metric" is symmetric (good days + bad days both register) and trend-watched. "Risk indicator" is threshold-triggered / negative-asymmetric. The UX and psychology are totally different — people watch health naturally, they dread a doomsday clock.

**How to apply**: When building, lead with "Market Health" as the primary gauge name. Make it green-heavy (green center, fades yellow/red at extremes). Don't show ratings like CRISIS / RISK_OFF unless user specifies — show functional terms like "Calm" / "Normal" / "Elevated" / "Unsettled" / "Stormy."

**Observation-first approach confirmed**: Build Tier 1 (visualization), skip Tiers 2-3 for now. Let it run 60-90 days minimum. If patterns emerge and correlate with fleet PnL, revisit gating. If not, it's still useful visualization — doesn't become dead weight.

## Social aspect (placeholder — user will specify later)

User noted a "social aspect" to revisit. Could mean several things:
- Twitter/X sentiment relay (atlas master plan referenced IFTTT → @DeItaone / @FirstSquawk)
- StockTwits / Reddit momentum feeds
- Discord community alpha channels
- Collaborative signal-sharing (user ↔ others)
- Sentiment analysis over multiple social sources

When user raises this again, ask: is this an ADDITIONAL data source feeding the health metric, or a SEPARATE panel / community feature?

## The reframe I pushed back with

**Two different things, not one:**

| Post-event (defensive) | Pre-event (offensive) |
|---|---|
| atlas news classifications (sev score, topic) | themis Congressional trades |
| geopolitical tension, country tags | Form 4 insider cluster buys |
| macro regime (RISK_OFF/ON) | options flow (unusual activity) |
| VIX term structure | dark-pool prints |
| **Use:** size down, gate entries | **Use:** initiate ahead of catalysts |

Different use cases, different panels, different data sources. User's original framing conflated them — the "risk dashboard" is really two dashboards.

## Honest obstacles flagged to user

1. **Lag problem**: news-based scores are reactive. Atlas RISK_OFF usually follows drawdowns, not leads them.
2. **Aggregation is subjective**: weighting 5 topic severities into one score is a guess requiring its own backtest.
3. **Country risk ≠ tradeable signal**: "Taiwan risk high" → "short TSM, long defense" requires domain research + backtest per rule.
4. **News often priced in**: retail news-gating strategies historically underperform.

## Proposed three-tier rollout

**Tier 1 — Observation (1-2 weeks build)**
- Market Risk Temperature gauge (0-100) at top
- Topic panels: Tariffs · Geopolitics · Monetary · Supply chain · Corporate catalysts
- Country heat map (atlas geo-tags)
- Pre-event + post-event split panels
- Historical chart: risk score vs fleet PnL daily
- **No gating** — visibility only

**Tier 2 — Validation (30-60 days post-Tier-1 data)**
- Correlation analysis: did high-risk days actually produce drawdowns?
- Did pre-event signals (themis, form 4) lead to alpha?
- **Gate decisions are data-driven, not gut-driven**

**Tier 3 — Selective gating** (= the event_trader layer already designed)
- Wire validated signals into execution via event map
- Per-rule backtesting before activation

## UX notes

- Single risk score MUST show sample size + confidence alongside. A "Risk: 73/100" with no "n=14 since 4/1" is worse than no gauge.
- Same rigor we apply to strategy confidence applies to risk confidence.

## Dependencies / prerequisites

1. Atlas validation analysis (queued for 5/1) — if atlas regime doesn't correlate with fleet drawdowns, the risk dashboard is decoration. Must resolve first.
2. Themis execution plumbing — currently observation-only (no trades.csv). Tier 2 needs this wired up.
3. Options flow / Form 4 runners are dormant research — see `project_dormant_strategies_inventory_20260422.md`.

## How to apply when user raises this again

- Do NOT build before 5/1 atlas validation.
- Tier 1 first — cheap visibility win, doesn't commit to any gating logic.
- If Tier 1 shows atlas IS predictive → proceed to Tier 2.
- If atlas is noise → kill the concept, don't waste engineering on UI around a non-signal.
- Link to `project_event_trader_architecture_20260422.md` — Tier 3 IS the event_trader layer.
