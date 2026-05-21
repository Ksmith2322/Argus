---
name: MISSED_BUY_NO_CASH misclassification
description: 309/310 MISSED_BUY_NO_CASH events are actually WATCH-gated entries zeroed by CONFLUENCE_SIZE_MULT_WHEN_NOT_TRADE=0, not real cash shortages
type: feedback
---

When CONFLUENCE_SIZE_MULT_WHEN_NOT_TRADE=0, WATCH-gated entries (score < CONFLUENCE_MIN_SCORE) pass conf_ok check, get valid qty from ledger (reason="OK"), then are zeroed by the multiplier. The _missed_buy_event_from_qty_reason() default fallback maps unrecognized "OK" reason to MISSED_BUY_NO_CASH.

**Why:** Misleading backtest metrics — looks like cash is the bottleneck when it's actually confluence gating working correctly.

**How to apply:** When reviewing backtest results with high MISSED_BUY_NO_CASH counts, check if CONFLUENCE_SIZE_MULT_WHEN_NOT_TRADE=0 is set. If so, these are correctly blocked low-confidence entries, not cash issues. Optional fix: add MISSED_BUY_CONFLUENCE_MULT event type in engine.py line ~1464.
