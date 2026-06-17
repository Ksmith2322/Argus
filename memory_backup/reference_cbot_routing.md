---
name: CME vs CBOT futures routing
description: CME Group splits products across two exchanges. NQ/MNQ/ES/MES route to CME; YM/MYM/RTY/M2K route to CBOT. Wrong routing = silent order cancel.
type: reference
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
# CME vs CBOT futures routing

CME Group lists futures products across multiple sub-exchanges. The IBKR `Future` contract requires the correct `exchange` field or TWS silently cancels orders with NO error code, NO whyHeld message, and NO useful trade.log entry. The cancel looks like a clean execution failure but is actually a routing rejection.

## The routing map

| Product family | Symbol(s) | Exchange |
|---|---|---|
| Nasdaq-100 (full) | NQ | **CME** |
| Nasdaq-100 (micro) | MNQ | **CME** |
| S&P 500 (full) | ES | **CME** |
| S&P 500 (micro) | MES | **CME** |
| Dow Jones (full) | YM | **CBOT** |
| Dow Jones (micro) | MYM | **CBOT** |
| Russell 2000 (full) | RTY | **CBOT** |
| Russell 2000 (micro) | M2K | **CBOT** |

**Fixed in helio/ibkr_execution.make_contract() on 2026-05-16.** Unit tests in `argus_flow/tests/test_make_contract_routing.py` lock the behavior.

## How the bug presented

For 22 days (2026-04-24 → 2026-05-16), three strategies targeting MYM (forge_tori, forge_cuebanks, forge_mamba) had ZERO fills. Every signal:
1. Passed all upstream guards (cluster cap, real_money boundary, oversize check)
2. Made it to `ib.placeOrder()` 
3. Came back with `orderStatus.status = "Cancelled"` within ~1 second
4. `whyHeld` was empty, `trade.log` had no error code, no actionable message
5. Logger recorded only `ENTRY NOT FILLED: SELL 1 MYM reason=Cancelled`

The strategies looked alive in heartbeats (the wake-and-sleep loop kept ticking) but never actually executed.

## Diagnosis pattern

If a futures-trading strategy has 0 fills over a long period but the runner is otherwise healthy:
1. Check `argus_flow/logs/unfilled_orders.jsonl` (the forensics dump added 2026-05-16) for the contract details and full trade.log
2. Specifically check `contract.exchange` against the symbol's correct exchange
3. ib_insync's `qualifyContracts()` does NOT reliably correct CME→CBOT routing in real submission paths even when it qualifies cleanly

## Related: nq_overnight still uses CME for MNQ
Working as expected — MNQ IS on CME. Don't "fix" what isn't broken.

## 2026-05-18 follow-up: Error 321 + qualify_front_month_future()

After the CBOT routing fix shipped Saturday, the FIRST Monday MYM signals (cuebanks/mamba/tori at 06:58 local) routed correctly to CBOT but still got cancelled — this time with **IBKR Error 321: "Error validating request. Please enter a local symbol or an expiry"**.

Root cause: `ib.qualifyContracts(Future("MYM", "CBOT"))` returns multiple ContractDetails (Jun/Sep/Dec quarterlies). ib_insync sees ambiguity, silently fails to mutate the contract. Order goes downstream with empty `lastTradeDateOrContractMonth` / `localSymbol` / `conId=0`. TWS rejects.

**Fix:** `helio/ibkr_execution.qualify_front_month_future(ib, symbol)` — uses `reqContractDetails`, filters by `min_days_to_expiry=7`, sorts by expiry, returns the front contract with conId/localSymbol/expiry all populated.

**Rule for any new futures-trading strategy:** call `qualify_front_month_future()` not `make_contract() + qualifyContracts()`. Stocks/ETFs/FX path unchanged.

`helio/signal_executor` already uses the new helper. Strategies going through it (cuebanks/mamba/tori) get the fix automatically. Any new direct caller of submit_bracket on futures needs to use the helper too.

**Forensics logger payoff:** the 22-day MYM bug took 22 days to find because trade.log was empty. The Error 321 bug took ~30 minutes because forensics captured `errorCode=321` + the full TWS message in `argus_flow/logs/unfilled_orders.jsonl`.
