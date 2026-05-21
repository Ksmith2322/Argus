---
name: Unfilled order forensics log
description: Every unfilled IBKR order dumps a full JSON record to argus_flow/logs/unfilled_orders.jsonl. Grep this when a strategy goes quiet without obvious reason.
type: reference
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
# Unfilled order forensics

Location: `argus_flow/logs/unfilled_orders.jsonl`

Added 2026-05-16 in `helio/ibkr_execution._dump_unfilled_forensics()` after the MYM/CBOT routing bug stayed invisible for 22 days because the live logger only captured the last 3 `trade.log` entries (which were empty for that bug class).

Each unfilled order writes ONE JSON line with:

```json
{
  "ts": "ISO8601",
  "order_id": "...",
  "contract": {
    "symbol", "secType", "exchange", "primaryExchange",
    "currency", "localSymbol", "lastTradeDateOrContractMonth", "conId"
  },
  "order": {
    "action", "orderType", "totalQuantity",
    "lmtPrice", "auxPrice", "tif", "outsideRth", "account"
  },
  "orderStatus": {
    "status", "filled", "remaining", "avgFillPrice",
    "permId", "parentId", "lastFillPrice", "clientId",
    "whyHeld", "mktCapPrice"
  },
  "log": [
    {"time", "status", "message", "errorCode"},
    ...  // FULL trade.log, not just last 3
  ]
}
```

## How to use

When a strategy goes quiet OR has high rejection rate:
1. Find the last fill or last attempted entry in `<strategy>/runner.log`
2. Note the timestamp + symbol
3. `grep '"symbol": "MYM"' argus_flow/logs/unfilled_orders.jsonl | tail -5` (etc.)
4. Inspect `contract.exchange` vs symbol — wrong routing = silent cancel
5. Inspect `orderStatus.whyHeld` — sometimes only field with the real reason
6. Inspect `log[]` — TradeLogEntry sequence shows PendingSubmit → Submitted → Cancelled or any error code

## What this caught us up on

Built specifically because the MYM bug was invisible — `orderStatus.status="Cancelled"`, `whyHeld=""`, `errorCode=0`, no message. Only the `contract.exchange` field would have surfaced the bug. The forensics dump captures every field, every time. Future silent-failure classes surface in minutes once we know which strategy is having trouble.
