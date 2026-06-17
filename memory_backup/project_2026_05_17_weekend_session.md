---
name: 2026-05-16 / 5-17 weekend session — ops cleanup + CBOT bug + cull prep
description: Two-day weekend session. Shipped CBOT routing fix (22-day silent bug on 3 strategies), forensics logger, cleared 2 stuck broker positions, restarted 7 runners, started cull audit prep.
type: project
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
# Weekend session 2026-05-16 / 5-17

## What was shipped

### Code changes (2 commits' worth)
1. **CBOT routing fix** in `helio/ibkr_execution.make_contract()`. MYM/M2K/YM/RTY now route to `exchange="CBOT"` instead of `"CME"`. 12 unit tests in `argus_flow/tests/test_make_contract_routing.py`.
2. **Unfilled-order forensics logger** in `helio/ibkr_execution._dump_unfilled_forensics()`. Every cancelled/unfilled order writes a full JSON record to `argus_flow/logs/unfilled_orders.jsonl`. See `reference_unfilled_forensics.md`.

### Runner restarts (all on Saturday)
| Strategy | New PID | What it gained |
|---|---|---|
| gld_pm_long | 42208 | 2.2× notional override (from 5/13 fix) |
| fomc_drift | 33392 | reqExecutions exit_px fallback |
| tom_international | 38436 | reqExecutions exit_px fallback |
| wick_gbpusd | 26844 | Fresh client_id (was stuck on connect failures) |
| cuebanks | 35184 | CBOT routing + forensics logger |
| mamba | 7896 | CBOT routing + forensics logger |
| tori | 39216 | CBOT routing + forensics logger |

## What was diagnosed

### MYM silent-cancel root cause
`helio/ibkr_execution.make_contract()` sent MYM with `exchange="CME"`. TWS qualified the contract cleanly but silently cancelled every order — `orderStatus.status="Cancelled"`, `whyHeld=""`, `trade.log` empty. Three strategies (cuebanks/mamba/tori) had 0 fills over 22 days because of this. Confirmed by comparing to nq_overnight (uses MNQ → genuinely on CME → 18 successful fills) and the strategies' rejection patterns.

### Stuck broker positions
- argus_gbpusd had stuck SHORT 184,856 GBP/USD from 5/15 EXIT FAILED cascade
- argus_cadjpy had stuck LONG 42,644 CAD.JPY (same cascade)
Both runners "forced local FLAT" but broker kept the positions. Both cleared on Saturday (operator + auto-resolution).

### spy_trend_follower verified healthy
0 fills in 22 days is BY DESIGN — trend regime stayed LONG, the strategy holds SPY 26sh from prior entry and waits for regime change. Not a bug.

## Where the fleet stands going into Monday 5/18

### P&L since 4/24 conversion (real, phantom-stripped)
- forge_nq_overnight: n=18, +$1,144, 61% WR (annualized projection +$19K)
- forge_gld_pm_long: n=25, +$824, 56% WR (+$14K, BUT was capped at 21 shares; now 2.2× unlocked)
- forge_jpy_pm_short: n=6, +$213, 67% WR (+$3.5K)
- argus_usdjpy: n=2, +$16 (too small to project)
- forge_aud_asian_breakout: n=3, -$36 (too small)
- All others: 0 fills, varies by reason

### Honest annual ROI projection
- Linear extrapolation (winners stay winners): **+$36K = +117%**
- Realistic (50% haircut for drawdowns): **+$18K = +58%**
- Conservative (Sharpe-anchored): **+$8-11K = +26-36%**
- Bad year: **-$3-8K = -10 to -25%**

The +26-36% range is the most defensible given the 22-day sample.

### Equity: $30,873 (was $30,983 start of weekend, net -$110)

## What's queued for next-week monitoring

- **3 newly-unblocked strategies** (cuebanks/mamba/tori) get a mid-month checkpoint, not a 5/31 verdict (need 14 days of post-CBOT-fix data)
- **gld_pm_long with 2.2× notional** — does the per-trade edge survive the larger size?
- **TWS daily disconnect window** — IBKR's mandatory daily reset around 16:00-17:00 ET, runners reconnect automatically (gdx_gld already proved this Sunday night)
- **Forensics log** at `argus_flow/logs/unfilled_orders.jsonl` — first thing to check on any "why isn't strategy X filling?" question

## What didn't happen

- No new strategies added (per 5/31 freeze discipline)
- No expansion of research scope (sub-$10 + PEAD already concluded as no-edge)
- Post-5/31 build queue (SPY put-write, multi-asset trend, etc.) documented in todo list, NOT started

## Build order for post-5/31 (locked in todos)
1. Dynamic Sharpe-based allocation layer (2-3 days)
2. Multi-asset trend following SPY/TLT/GLD/USO/DBC (3-5 days)
3. SPY put-write — the biggest missing edge, vol risk premium (1-2 weeks)
4. Live shadow logger (1 hour)
5. VIX term structure carry (1 week)
6+. Intraday gap-and-go, multi-factor stacking, news classification, statistical pairs
