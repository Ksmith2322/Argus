---
name: Phase 20 - WebSocket Feed + Fast Exit
description: Future phase to add Coinbase WebSocket feed and mid-candle fast-exit path for faster trade execution
type: project
---

Phase 20 (future): WebSocket feed + fast-exit path for faster entries/exits.

**Why:** Current system only makes decisions on 1m candle close (HTTP polling). User wants ability to quickly get in/out of positions. True HFT not feasible with Python+retail API, but fast scalping (seconds to low minutes) is achievable.

**How to apply:** Do NOT start until screening results are in and winning params are validated. Sequence: finish screening queue → pick winning params → validate with longer run → then build this.

**Scope:**
- Coinbase WebSocket real-time price feed (replace HTTP polling in feed_coinbase.py)
- Fast-exit path: lightweight bail-out check on every tick (skip full engine overhead)
- Optional shorter candle aggregation (15s/30s from WebSocket stream)
- Limit orders for better fills
- Touches: feed_coinbase.py, runner_live.py, engine loop
