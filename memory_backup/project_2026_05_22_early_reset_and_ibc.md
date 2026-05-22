---
name: 2026-05-22 early reset cutover + IBC build (weekend scope)
description: Brought the 5/31 reset cutover forward by 9 days to 2026-05-22. Batched with $250K paper reset, AI overlay disable, drift re-anchor, dispatcher fix all from the same session. Plus IBC 3.23.0 download/configure for TWS auto-restart pain-point fix. Activated 5-survivor cohort (gld_pm_long, nq_overnight, pead, xs_momentum, spy_trend_follower) at 0.5x. Subscribed to Massive.com (Polygon rebrand) Basic tiers (free, 5 plans) for the upcoming backtest factory. Memory created so the next session knows this happened.
type: project
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
## Why it happened today instead of 5/31

The activation exercise the last 2 days surfaced 9 production bugs
including the 30-day-silent canonical_fills dispatcher gate that
caused all argus paper fills to be silently dropped. With operator
agreement, the operational direction shifted from "harden current
strategies" to "pivot to offense via backtest factory + alt-data."

The 5/31 reset's purpose was to start a clean evidence epoch after
draining contaminated pre-fix data. Since we're now ALREADY doing
"clean state" operations (paper reset to $250K, dispatcher fix,
drift re-anchor, AI overlay off), batching the epoch_reset into
the same window means:

- The 30-day post-reset clean evidence window starts NOW (5/22) not
  5/31 — 9 extra days of clean data
- All operationally-significant changes are batched in one boundary,
  not spread across two dates
- The survivor cohort starts trading at 0.5x with the dispatcher fix
  ALREADY landed, so canonical_fills writes from day 1 will work

## What ran (8-phase runbook executed)

| Phase | Action | Result |
|---|---|---|
| 1 | Stop all runners | 34 processes stopped |
| 2 | Pre-reset snapshot | 799 files archived to argus_flow/logs/_archive/pre_reset_snapshots/20260522_1813 |
| 3 | epoch_reset --execute --target 20260522 | canonical_fills emptied, state counter resets, archive manifest written |
| 4 | Advance evidence_epoch | current = post_reset_20260522, is_clean=True |
| 5 | Activate roster | gld_pm_long + nq_overnight + pead + xs_momentum + spy_trend_follower = 0.5x; argus_usdjpy reverted to 0.0 |
| 6 | Launch via start_post_reset_runners.ps1 | 6 modules launched; xs_momentum exited cleanly (--evaluate is one-shot for monthly rebalance, correct) |
| 7 | Dashboard restart + smoke | /api/gateway_status: 200 OK |
| 8 | Final verification + log | sunset_roster + killed_strategy_invariant tests green; logged to reset_history.jsonl |

## Survivor cohort allocations (post-reset.v11)

```
forge_gld_pm_long:        0.5
forge_nq_overnight:       0.5
forge_pead:               0.5   (post-reset PAPER_CANDIDATE; Apollo-curated universe caveat)
forge_xs_momentum:        0.5   (half-size per memory; 9y backtest +16% CAGR / 37% DD)
forge_spy_trend_follower: 0.5   (passive beta benchmark, regime-gated)

argus_usdjpy:             0.0   (returned from 0.5 exercise window; sunset confirmed)
argus_gbpusd, argus_cadjpy: 0.0 (sunset)
19 other archived strategies: 0.0 (sunset, no_restart=True)
```

## IBC build (Interactive Brokers Controller)

Downloaded IBC 3.23.0 (https://github.com/IbcAlpha/IBC) to `C:\IBC`.
Configured `config.ini` for paper mode + auto-accept + daily restart
at 23:55. Two ops scripts added to repo:

- `ops/start_tws_via_ibc.ps1` — launcher with pre-flight checks (TWS
  present, IBC present, credentials not PLACEHOLDER, no existing
  tws.exe) + post-launch port-7497 verification
- `ops/IBC_SETUP.md` — operator handoff doc with credentials step,
  first-run test, scheduled task setup, daily ops, revert path

Operator's two remaining manual steps:

1. Edit `C:\IBC\config.ini` to set `IbLoginId` + `IbPassword` to the
   paper-account credentials (currently PLACEHOLDER values).
2. Optionally register the IBC scheduled task for boot-time recovery.

After these, the manual TWS babysitting cycle ends:

- Disclaimer auto-accepted on every restart (no more 5/22 morning pain)
- Port 7497 auto-listens after machine reboot
- Daily 23:55 restart handles IBKR's nightly server reset
- Reconnects on connection drop (Error 1100 cycle no longer
  human-attended)

Java 1.8 (IBKR-bundled) already present — no separate JDK install needed.

## Massive.com (Polygon.io rebrand) subscription

Operator subscribed to 5 Basic plans on Massive.com — all $0/mo:
Stocks, Currencies, Futures, Indices, Options. This covers the
backtest factory v1 needs without spending anything.

API credentials saved to `secrets/massive.env` (gitignored).

$99/mo expansions deferred per the "buy only when a specific
strategy needs it" rule. Likely candidates if/when needed:

- Benzinga News (for news-sentiment strategies)
- Benzinga Earnings (for PEAD generalization)
- ETF Global Fund Flows (for xs_momentum enhancement)

## State at end of session

```
Paper account:        $283,177 NetLiq (cash $250K + stocks)
Drift detector:       clean, anchor=$282,410, divergence=0.0%
Evidence epoch:       post_reset_20260522 (is_clean=True)
Active runners (5):   argus_flow.runner_unified + 3 forge survivors
                      (xs_momentum --evaluate exited cleanly; monthly rebalance)
canonical_fills:      empty (clean baseline; dispatcher fix loaded)
TWS:                  still running (current session); IBC swap is tomorrow's task
AI overlay:           DISABLED via ARGUS_DISABLE_AI_OVERLAY env var
Multiplier:           argus_usdjpy 0.5 / argus_cadjpy 0.7 ACTIVE but allocation=0
                      (multiplier inert until argus pairs re-activated, which is
                      not planned — they were sunset per 5/20 decisions)
```

## What didn't get done this session

- Backtest factory v1 (deferred to next session — weekend scope was
  reset + IBC; factory is week 1 post-reset work)
- Polygon/Massive data layer integration (deferred; account ready,
  integration not started)
- Forward Russell-reconstitution or insider-buy strategy candidates
  (deferred; backtest factory is the prerequisite)

## Next session's first action

Verify the post-reset cohort is producing trades + canonical_fills
rows. If the dispatcher fix worked AND any strategy fires, we should
see argus rows in canonical_fills.jsonl for the first time since
2026-04-23. If still no rows after 24-48 hours of cohort runtime, dig
into the forge-side fill writers (forge runners use a different code
path than argus).

## Bigger context

This session was the strategic pivot point from "build safety nets"
to "build offense." The 4-agent debate consensus said "stabilize
30 days before more building" — we respected the spirit (no new
infrastructure builds tonight) while batching the operational
changes that had to happen anyway. The next 4-6 weeks are for
backtest factory + alt-data integration + strategy generation, NOT
more execution-layer fixes.

Real-money timeline conversation deferred entirely — comes back only
when evidence supports it.
