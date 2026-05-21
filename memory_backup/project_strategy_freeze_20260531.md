---
name: Strategy freeze date — 2026-05-31
description: Hard cutoff for new strategy creation. After 5/31, no new strategies until system goes live. Vet existing fleet by then; focus on confirmed winners only.
type: project
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
## The rule

**After 2026-05-31:** the research surface is FROZEN. Period.

The freeze covers more than "no new strategies." Freezing only strategy-creation while allowing instrument additions, parameter tweaks, or "small" tuning recreates the same explore-loop trap with different surface. Tightened scope:

### What the freeze BLOCKS (after 5/31)
- **No new strategy logic** — no new signal generators, no new pattern detectors
- **No new instruments** — frozen strategies operate on their frozen instrument list. tom_international stays at 6 ETFs; multi_orb stays at SPY/QQQ/IWM/GLD; etc.
- **No new overlays** — LLM regime classifier, AI voter additions, new sentiment feeds — all queued for post-go-live
- **No parameter hunting** — no "let's tune RSI from 30 to 28 to see if it helps." Parameter changes mean re-running the full evidence ladder.
- **No "small" tweaks disguised as fixes** — the phrase "this small change doesn't count" is the exact trap. It always counts.
- **No clever exceptions** — if a path opens for one strategy, it opens for all; either lock it down or document why it's globally allowed.
- **No 4th Week-2 strategy** — even if one of the 3 approved (zn_momentum, vix_short, spy_tlt_pair) fails to build. Cap is firm.

### What the freeze ALLOWS (after 5/31)
- Bug fixes (broken signal logic that's not generating expected signals per its design)
- Reconciliation fixes (RECON_DRIFT recovery, fill audit issues)
- Observability fixes (dashboard panels, log structure, audit reports)
- Risk-control fixes (kill switch, circuit breakers, position caps)
- Documentation updates (memory files, runbooks, failure modes)
- Operational hardening (auto-restart logic, daemon resilience)
- Capital decision actions per the promotion ladder (KEEP-PAPER → WINNER-CANDIDATE, etc.)

If a new idea surfaces between now and 5/31, it can be added — but each addition consumes attention budget that should be on the cull. After 5/31, ideas get queued for "post-go-live" — written down in `project_post_freeze_idea_queue.md` (create on demand), not built.

Goal of the freeze: stop the open-ended explore loop. Force the focus shift from "build more" → "vet what's built" → "kill what doesn't work" → "scale what does." Then "deploy capital to one proven thing."

## Why

Per user 2026-04-26: "lets finalize the strategies and try not to implement new once after end of may.. if we find an idea, we can test it after we are live but not work on it until later on.. and lets try to vet out all strategies by end of may to focus on our winners only."

The fleet has 22+ strategies. 8 are firing trades (5 net-positive, 3 net-negative). 15 are silent. Adding more before existing ones are vetted dilutes attention. The freeze creates the conditions to actually decide.

## What "vet by end of May" means

For each strategy, by 5/31 there should be a written verdict:
- **WINNER** — accumulating positive PnL with statistical sample, scale up risk_pct
- **REWORK** — has structural issue (data, gate, sizing, instrument), document fix or kill
- **KILL** — confirmed no edge after fair test, shelve

Silent strategies that won't fire even after gate-loosening = effectively KILL.

## Hard kill triggers (auto-shelve, no ceremony)

By 5/31, any strategy meeting any of these gets shelved:
- Operational maturity DEGRADED for 30+ consecutive days
- PF < 1.1 over n ≥ 50 valid trades
- Zero trades fired in 60+ days despite gate-loosening attempts
- Active execution path broken (REAL_ENTRY FAILED ratio > 50% over 20 attempts)

## What survives the freeze

Whatever is left is the v1.0 fleet — the production set going into real-money phase.

## How to apply this memory

**Why:** Without a hard date, exploration eats all the runway. This is the explicit termination criterion.

**How to apply:**
- If user proposes new strategy work between now and 5/31: OK, but flag the date.
- If user proposes new strategy work after 5/31: queue it, don't build it. Reference this memory.
- If a strategy looks dead by ~5/15: surface it, don't wait until last minute.
- 5/31 itself: hold the cull conversation — concrete winner/rework/kill verdict per strategy.
