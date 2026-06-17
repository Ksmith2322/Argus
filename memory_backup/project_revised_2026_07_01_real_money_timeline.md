---
name: Revised real-money timeline (5/18 update)
description: Operator decision 2026-05-18 — extend validation window by 30 days post-5/31-reset. Real-money go/no-go pushed from 6/30 to 7/1+. IBC auto-restart for TWS gets built before reset. Reason: 22 days pre-fix data was contaminated by silent bugs only diagnosed this past weekend.
type: project
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
# Revised real-money timeline (operator decision 2026-05-18)

## What changed

Previous plan (per `project_5_week_roadmap_20260424_to_20260531.md`): 6/30 real-money go/no-go decision.

Revised plan: **6/30 real-money decision becomes 7/30+** to give us a clean 30-day evidence window post-5/31 reset.

## Why the revision

The 24 days of "live" data from 4/24 → 5/18 contained multiple silent bugs:
- MYM/CBOT routing bug (cuebanks/mamba/tori had 0 fills for 22 days; fixed 5/16)
- Front-month qualification (Error 321 on multi-expiry futures; fixed 5/18)
- argus_gbpusd EXIT FAILED cascade (recurring 5/13/5/15/5/18; forensics deployed 5/18, fix pending)
- Two stuck broker positions (cleared 5/16-5/17)
- gdx_gld 23h overnight death (5/16-5/17; fix shipped 5/18)
- TWS client_id slot stuck (5/13, 5/15; connect_with_retry shipped 5/18)

The strategies that fired during this period traded under degraded conditions. Their P&L is not a clean signal of edge. **30 days of post-fix evidence is what the real-money decision should be based on.**

## Revised timeline

| Date | Milestone |
|---|---|
| **5/18 → 5/31 (13 days)** | Continue current monitoring + cull audit refinement as new data lands. Per-strategy verdicts finalized 5/31. |
| **Weekend of 5/24-25 or 5/31** | **Build IBC** (Interactive Brokers Controller) for TWS auto-restart + disclaimer accept. Eliminates manual TWS babysitting. ~2-3 hour build + 1 week monitoring. |
| **5/31 EOD** | Run `ops/maintenance/epoch_reset.py --target 20260531 --execute`. Archives all pre-reset evidence. Resets balance to $30K paper. Clears dashboard. Preserves: code, memory, configs, allocation_factors, kill verdicts. |
| **6/1 - 6/30 (30 days)** | Clean evidence window. Strategies graded only on post-fix, post-reset data. No new strategy additions per freeze. |
| **6/30 - 7/1** | Mid-window review. Are the survivors performing as projected? |
| **7/1 onward** | If 30-day evidence is clean (no new silent bugs, strategies earn ~projected), proceed to real-money $10K SMOKE deployment. If contaminated, extend window another 30 days. |

## What survives the 5/31 reset (per existing reset plan)

Preserved:
- All code, all tests, all memory files
- `allocation_factors.json` (kill verdicts stay locked)
- `real_money_allowlist.json` (still empty until 7/1)
- `shadow_strategies.json`
- All configs (fleet_sizing.json, etc.)

Archived to `_archive/pre_reset_20260531/`:
- canonical_fills.jsonl
- All per-strategy state.json files
- All trades.csv files
- Pre-reset broker snapshots

Reset:
- Paper account balance back to $30K (manual in Client Portal)
- Per-strategy counters that drifted COUNTER_AHEAD
- Heartbeats refresh on next runner cycle

## Post-5/31 build queue (UNCHANGED — still locked)

The 9-item queue in todos stays the same. The revision only affects the real-money TIMING, not the BUILD SEQUENCE. Build order:
1. Dynamic Sharpe-based allocation (2-3 days)
2. Multi-asset trend SPY/TLT/GLD/USO/DBC (3-5 days)
3. SPY put-write (1-2 weeks) — the biggest missing edge
4. Live shadow logger (1 hour)
5. VIX term structure carry (1 week)
6+. Intraday gap-and-go, multi-factor stacking, news classification, statistical pairs

These ship in Aug/Sep at the earliest, contributing to year-2 returns, not year-1.

## Honest read on this revision

**It's the right call.** The bot has gone through more change in the past 5 days than the previous 5 weeks combined. The strategies that "worked" pre-fix had structurally different conditions than the strategies that "work" post-fix. Mixing the two samples gives a misleading picture.

A 30-day clean window is the SHORTEST defensible evidence period for a real-money go/no-go. Some practitioners use 90 days. We're already aggressive at 30. Anything less than 30 risks deploying real money on noise.

## Risk of this revision

- **Opportunity cost:** 30 extra days of paper trading = 30 days of NOT earning real money. At projected +40% annual, that's ~$1,200 of real-money returns deferred. Small price for confidence.
- **Strategy decay:** if an edge exists, it could weaken during the extended paper window. But that's actually a useful filter — fragile edges shouldn't get real money anyway.
- **Operator fatigue:** 30 more days of manual TWS babysitting. THIS is why IBC needs to be built first.

## What this means for daily operations

Until 5/31: same as today. Watch monday_watch. Restart on red. Cull as data lands.

5/31 EOD: ceremony day. Run the reset. Validate the reset worked. Take a screenshot of broker $30K confirmation.

6/1+: New evidence epoch. Treat each strategy as if it had zero history. Grade on the post-reset trades only.

7/1+: First real-money candidate selection based on 30-day clean evidence.
