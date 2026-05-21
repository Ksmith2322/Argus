---
name: Kill-or-rework decisions 2026-04-20
description: Three strategies ruled kill/shelve/research_only based on newly-computed artifact evidence. Dispositions now encoded in each artifact + dashboard row.
type: project
originSessionId: 468782c5-c83c-4fc9-8b1b-60051a2f4e99
---
Following the 2026-04-20 computed-conversion, three strategies failed the P(expectancy>0)>=0.90 bar or had misleading headline PFs. Each is now carrying a structured `disposition` in its strategy_confidence artifact and the dashboard row.

**Why:** without a formal kill/rework record, a negative-edge strategy with a flattering top-line PF (e.g. Sector Rot at 1.55) can drift toward promotion. The disposition field is the persistent "do not promote" signal.

**How to apply:** before suggesting any runner go live or advance stages, check the artifact's `disposition.status`. Anything with `status in {kill, shelve, scope_down}` is not promotable until the disposition is reviewed and either lifted or the underlying issue is resolved.

## Decisions

### Apollo — `research_only` (until 2026-07-20)
- Union PF 0.69, P(exp>0) = 0.012, MC ruin 82% on post_er subset (n=205)
- 34 of 97 symbols profitable; top-6 (MU/ORCL/UPS/PLUG/SNOW/GOOGL) carry +$937 while 87 others net-bleed
- Subset edge too thin (n=2-3 per symbol) to promote in isolation
- Rework path: narrow universe to validated subset, collect n>=10 per symbol before advancing

### Sector Rot — `kill`
- Per-month PF 1.55 looks fine but strategy return +60.4% underperforms SPY +130.6% (-70pp alpha)
- The positive PF reflects long-equity beta during 2020-2026 bull run, not rotation alpha
- If revisited, must be re-specced to compute SPY-relative (alpha) returns, not absolute monthly

### Mamba — `shelve`
- Union PF 1.00, P(exp>0) = 0.47, MC ruin 95% (n=25 after daily-cap fix)
- Subset "YM=F only PF 2.88 on n=12" is below sanity bar for a subset claim
- Currently on synthetic 1m (resampled from 5m)
- Unblock gates: (1) real 1m bar source plumbed, (2) YM=F subset validated with n>=30 on real data

## Where the decisions live

- Schema: `helio/strategy_confidence.py` — `Disposition` model + `disposition` field on `StrategyConfidenceArtifact`
- Writers: `apollo/confidence_writer.py`, `forge/sector_rot_confidence_writer.py`, `forge/mamba/confidence_writer.py` emit dispositions at the end of `build_*_artifact`
- Dashboard: `/api/strategy_performance` row includes a `disposition` block when present
