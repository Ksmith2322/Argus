---
name: Cluster Exposure Model — correlation governor with hard caps
description: Defines macro/correlation clusters and per-cluster exposure caps. Prevents the "many strategies secretly all the same bet" failure mode. Pre-5/31 BLOCKS-FLEET work.
type: project
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
## Why this exists

A "diversified" multi-strategy fleet can secretly stack the same macro bet. Examples from current fleet:

- argus_usdjpy long + jpy_pm_short = both USD/JPY exposed (different directions, but same instrument cluster)
- multi_orb on SPY/QQQ/IWM + spy_mean_rev + vix_intraday = all equity-beta exposure
- gld_pm_long + gdx_gld pair (long GDX side) = both metals
- ZN momentum (incoming) + bond ETFs (none yet, but could happen) = duration

A regime change (USD shock, equity selloff, vol spike, rate move) takes them all out together. **Without cluster caps, the fleet looks diversified but trades like one big macro position.**

This is on the BLOCKS-FLEET liability list. Cluster cap implementation is required before any real-money go-live (per `project_real_money_readiness_gate_20260531.md` items 13-14).

## Cluster definitions

Each open position belongs to one or more clusters with a weight (0-1).

| Cluster | Includes | Notes |
|---|---|---|
| **FX_USD_LONG** | any USD long leg (USDJPY long, EURUSD short, GBPUSD short, etc.) | weight 1.0 for direct USD trades |
| **FX_USD_SHORT** | any USD short leg | weight 1.0 |
| **FX_JPY** | any JPY trade | weight 1.0 (USD/JPY counts in both FX_USD and FX_JPY) |
| **EQUITY_BETA** | SPY, QQQ, IWM, sector ETFs (long), individual large-cap longs (titan, hermes, apollo trades), short-vol setups | weight by net delta |
| **EQUITY_BETA_SHORT** | SPY/QQQ/IWM shorts, long-vol setups (vix_intraday long) | weight 1.0 |
| **SHORT_VOL** | UVXY short, VIXY short, vix_short strategy | weight 1.0; **special hard cap, see below** |
| **LONG_VOL** | UVXY long, vix_intraday long entries | weight 0.5 (some hedging value) |
| **DURATION** | ZN, ZF, TLT, IEF, bond ETFs, anything rate-sensitive | weight 1.0 long, -1.0 short |
| **METALS** | GLD, GDX, SLV, gold/silver miners | weight 1.0 |
| **ENERGY** | CL, USO, XLE, oil-related | weight 1.0 (none yet in fleet) |
| **INTERNATIONAL_EQ** | EEM, EWJ, VGK, EFA, FXI, INDA (tom_international) | weight 1.0 each, sums |

A single trade can populate multiple clusters. Example: USDJPY long → FX_USD_LONG (1.0) + FX_JPY (1.0).

## Exposure caps (per cluster, as multiple of broker_equity)

| Cluster | Cap (× anchor) | Rationale |
|---|---|---|
| Single instrument | 1.5× | Catches multi_orb + spy_mean_rev + vix_intraday all opening SPY |
| FX_USD_LONG or FX_USD_SHORT | 8× | FX leverage acceptable but USD bias dangerous |
| FX_JPY | 5× | JPY pair concentration limit |
| EQUITY_BETA | 1.5× | Most strategies are equity-beta; cap forces real diversification |
| EQUITY_BETA_SHORT | 1.0× | Lower cap because short-vol blowups are asymmetric |
| **SHORT_VOL** | **0.5×** | **Hard cap — short-vol convexity tail risk** |
| LONG_VOL | 1.0× | Long-vol bleeds in calm markets; cap prevents bleed concentration |
| DURATION | 2.0× | Bond futures + ETFs combined |
| METALS | 1.5× | gld_pm_long + gdx_gld can both fire |
| INTERNATIONAL_EQ | 2.0× | tom_international × 6 ETFs + any other intl |
| **Total open notional** | **6× anchor** | Catch-all prevents fleet maxing 8 clusters at once |

These caps are starting points. Tighten if real-money operation surfaces issues.

## Enforcement model

**Pre-trade check:** before submitting an entry, the executor computes projected post-fill cluster exposures. If any cap would be breached, **the entry is rejected** with reason `CLUSTER_CAP_BREACH`. The strategy can retry on next signal.

**Implementation location:** `helio/cluster_exposure.py` (new module). Called by:
- `helio/ibkr_execution.py:submit_bracket()` (forge runners)
- `helio/ibkr_executor.py:submit_signal()` (Greek family)
- `argus_flow/runner_unified.py` entry path

**Continuous check:** every 5 min, the daemon recomputes cluster exposure and writes to `argus_flow/logs/cluster_exposure.json`. If a cluster is over cap (e.g. due to mid-day price moves expanding notional), all FURTHER entries to that cluster are blocked until exposure drops back under cap.

**Discord alert:** when any cluster crosses 80% of cap, alert. When any cluster is at 100%, alert urgently.

## Cluster vs evidence — they fight sometimes

The capital allocator wants to allocate to top-scoring strategies. The cluster model wants to limit any single cluster. These will conflict.

**Resolution:** cluster wins. If vix_intraday (long-vol) is the highest-scoring strategy but LONG_VOL is at cap, vix_intraday gets throttled, not the cap. The user's stated principle was "loyalty to evidence, not instruments" — but at the FLEET level, that means evidence about *which clusters survive*, not "always allocate to the top strategy."

If a top strategy is repeatedly throttled by cluster caps: that's signal the cluster mix is wrong (too many strategies of the same type). Cull, don't relax the cap.

## Special handling — SHORT_VOL

Per `project_pre_freeze_coverage_gaps_20260426.md`, vix_short (incoming Week 2) gets stricter treatment:
- SHORT_VOL hard cap is 0.5× anchor (already above)
- Per-strategy position cap halved (also above; reinforced here)
- vix_short cannot enter SHORT_VOL cluster real-money before 90 days paper proof
- Force-close all SHORT_VOL positions when VIX prints > 30 — fleet-wide kill, not per-strategy

## Pre-5/31 implementation work

The model + caps live here (memory). Code implementation lands in Week 3:
- Mon 5/11: build `helio/cluster_exposure.py` module + cluster mapping table
- Tue 5/12: wire into all 3 executor paths (ibkr_execution, ibkr_executor, runner_unified)
- Wed 5/13: drill — manually inject a borderline scenario, verify cap blocks the entry
- Thu 5/14: continuous monitor + Discord alerts
- Fri 5/15: full audit pass to verify cluster_exposure.json updates correctly

This is part of the Week 3 risk hardening sprint, not new strategy work — falls within freeze rules.

## How to apply this memory

**Why:** the failure mode the reviewer flagged ("many strategies secretly the same bet") is a real fleet-killer. Cluster caps are the structural defense.

**How to apply:**
- Code implementation Week 3 is mandatory; this is on the BLOCKS-FLEET liability list.
- When adding a new strategy (pre-5/31): assign cluster memberships at strategy spec time. Update this memo with new cluster definitions if needed.
- Cluster caps tighten if real-money surfaces issues. Don't relax them unless evidence specifically supports relaxation.
- A repeatedly-throttled strategy = signal to cull, not signal to widen cap.
