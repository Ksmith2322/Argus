---
name: Event-driven trader architecture (design direction)
description: User asked about turning atlas news + themis Congressional signals into actual trades, including shorts. Unified executor layer design captured for post-5/1 work.
type: project
originSessionId: 81afc97b-10bf-49fd-ba79-e5a89a7f400b
---
User conversation 2026-04-22.

## The gap user identified

Multiple signal-generator components exist but none connect to execution:
- **atlas**: classifies news events (TARIFF_ANNOUNCE, TAIWAN_TENSION, FED_HAWKISH, etc.) with severity scores. Currently in LOG_ONLY mode — doesn't affect trades.
- **themis**: pulls Congressional trade filings every 6hr, applies signal rules. Has no `trades.csv`, no `--execute`, no broker path. Pure observation.
- **Future news-driven shortlist** user asked about.

User insight (verbatim): "is there a way we can have it populate stocks or items to short in the future? even stocks we dont work on in the daily.. just think about it and future process as long as we have a set standard or entry and we are able to take advatange or it"

## Proposed architecture

One shared runner `forge.event_trader.runner --loop` that:
1. Consumes from multiple signal sources via pluggable config
2. Each source provides: event_type → (instruments, direction, conviction)
3. Applies standardized entry rules per source (risk_pct, stop, time-stop, short eligibility)
4. Writes standard trades.csv + canonical_fills entries

```json
{
  "sources": {
    "atlas":  { "event_map_file": "atlas_events.json", "min_conviction": 0.7 },
    "themis": { "signal_map_file": "themis_signals.json", "min_score": 60 }
  },
  "execution": {
    "default_risk_pct": 0.5,
    "default_stop_atr": 1.0,
    "default_time_stop_bars": 5,
    "short_enabled": true,
    "short_tier_min": "emerging"
  }
}
```

Event maps are per-source json files — domain-specific knowledge about which events move which instruments. Examples:
- `TARIFF_ANNOUNCE_CHINA` → short FXI/EEM/BABA, long DXY
- `TAIWAN_TENSION` → short TSM/EWT, long LMT/RTX
- `FED_HAWKISH` → short TLT/GLD, long DXY

## Why this matters strategically

- User wants short-side exposure for down days — current fleet is almost all long-biased
- Enables trading instruments NOT in the daily universe (e.g., TSM on Taiwan news when TSM isn't otherwise covered)
- Keeps entry rules standard/testable (not discretionary)
- Centralizes execution — one place to manage slippage, borrow availability, conviction scoring

## Prerequisites before building

1. Atlas regime calls must be validated (5/1 report: does atlas RISK_OFF correlate with fleet drawdowns?)
2. Event→instrument maps require user domain knowledge (which stocks move on which event types)
3. Backtest on historical atlas+themis signal logs to validate event_trader edge before live
4. Verify borrow availability for short candidates
5. Slippage control (news events have 10-50 bps slippage)

## How to apply when user raises this again

- Don't build before 5/1 atlas validation — premature.
- Don't start with 50 event types — prove ONE event type has edge (e.g., TARIFF_ANNOUNCE_CHINA → short FXI) before scaling.
- Shorts in general require tier-gated rollout same as longs — start unproven 0.5%, scale as data accumulates.
- Event maps are user-supplied research, not auto-generated. I can build the framework but the domain knowledge is the user's.
