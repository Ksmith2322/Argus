---
name: Dashboard upgrades — capital safety + decision support
description: Backlog of dashboard improvements to evolve from "operational health monitor" to "capital allocation control surface." Top-10 priority list with rationale per item. Week 3+ work, after the kill/pause + cluster exposure code lands.
type: project
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
## The core gap

Current dashboard answers: *Is the bot running? Did it trade? Did it make money?*

It needs to also answer: *Is the bot safe? Is the trade valid? Is the sizing sane? Is this strategy proving alpha? Is this exposure duplicated elsewhere? Should this strategy get more or less capital?*

Operational visibility is a B+. Capital safety visibility is a D+. That is the gap to close.

## Top 10 additions (priority order)

| # | Addition | Why |
|---|---|---|
| 1 | **Risk % per trade / per open position** | Currently shows notional ("Buy-in $") which is misleading. AUDUSD $116K vs $11.7K equity LOOKS catastrophic but is actually 0.5% risk. Label clearly. |
| 2 | **Gross notional × equity** at top of page | Surface dangerous size at a glance |
| 3 | **Margin used estimate** | Track real-money margin headroom before going live |
| 4 | **Decision state per strategy** (BLOCKED / OBSERVE / KEEP-PAPER / WINNER-CANDIDATE / REWORK / QUARANTINE / KILL_CANDIDATE / KILL / REAL-CANDIDATE) — separate from runtime state | Prevents weak strategies from looking green just because process is running |
| 5 | **Valid / Dirty / Invalid trade counts** per strategy | Promotion uses VALID only. Right now everything is lumped as "live trades." |
| 6 | **Post-reset sample tracking** | After config changes / RESEARCH_ONLY flips / state resets, old data is diagnostic only. Need to see new baseline trade count separately. |
| 7 | **Blocked-signal reason counts** | For silent strategies — show *why* they didn't trade. Top block reason. Last scan time. Expected frequency vs actual. |
| 8 | **Alpha attribution panel** | Strategy PnL vs dumb benchmark over same windows. Flag PASS/FAIL. Without this, every PnL story is unattributed. |
| 9 | **Cluster exposure panel** (FX_USD, EQUITY_BETA, SHORT_VOL, DURATION, METALS, etc.) | The whole point of the cluster model is to surface this — currently invisible on the dashboard. |
| 10 | **Recommended action panel** | "KILL_CANDIDATE: multi_orb (n=48, -$70, no fix)" / "BLOCKED_REVIEW: tori (no live trades, verify gates)" / etc. Turns dashboard from monitor into decision tool. |

If only 5 land before 5/31: **#1 risk %, #2 notional × equity, #4 decision state, #7 blocked-signal reasons, #8 alpha attribution.**

## Capital Safety Bar (top-of-page banner)

Add a second status bar directly under the existing gateway status:

```
CAPITAL SAFETY
Open Risk: $X / Y%
Gross Notional: $X / Yx equity
Margin Used: Z%
Max Cluster: <CLUSTER> Yx
Real Enabled: NO / YES
Sizing Mode: PAPER_TEST / REAL_READY / BLOCKED
```

Color: green ≤50% caps / yellow 50-80% / orange 80-100% / red over cap.

## Items to remove or change

- **"Expected annual 40-75% HARDCODED"** — biases decisions; not validated. Replace with `Projected Annual: DISABLED until 30+ valid trades on top strategy` OR clearly label `MODEL ESTIMATE — NOT VALIDATED`.
- **Single "Fleet Confidence 62%"** — too abstract. Decompose into: Operational / Evidence / Execution / Risk / Attribution. A fleet at 92% operational + 0% attribution should not show as 62% blended; that hides the gap.
- **"OK" status as catch-all per strategy** — should split into PROC_OK / EDGE_OK / EXEC_OK / RISK_OK / PROMOTION_OK. A strategy passes only if all are clean.

## Promotion progress bars need gates

Currently promotion percent looks trade-count-based. multi_orb at "84%" with negative PF is misleading. Promotion display should show:

- BLOCKED if any critical gate fails (negative expectancy, PF gap, dirty execution, recon drift)
- ACTIVE % only if all gates currently passing
- Reason text under the bar when BLOCKED

Example:
```
multi_orb     Promotion: BLOCKED — negative expectancy (PF 0.85 live)
spy_mean_rev  Promotion: BLOCKED — v1 negative; v2 pending
vix_intraday  Promotion: ACTIVE 60% — need +12 valid trades, attribution report
```

## Per-strategy "Sizing Sanity" column

For each recent trade, show:
- Risk %
- Notional × equity
- Sizing status: OK / CHECK_MULTIPLIER / OVER_RISK_CAP / OVER_NOTIONAL_CAP

Example flagging multiplier bugs:
```
AUDUSD  Risk: 0.50%  Notional: 9.97×  Sizing: CHECK_FX_MULTIPLIER
MNQ     Risk: 0.78%  Notional: 23.47× Sizing: CHECK_FUTURES_MULTIPLIER
```

This catches the kind of bug that would be catastrophic in real money.

## Visual severity hierarchy

Currently everything is visually loud (cyber/dark theme). Eye should go to severity, not novelty:

- **Green** — safe, passed, healthy
- **Yellow** — attention needed
- **Orange** — degraded, blocked, near-cap
- **Red** — action required, real-money relevant
- **Purple/gray** — inactive, event-waiting (vix_revert, fomc_drift between events)

User's first glance should land on: open risk → broken sizing → recon drift → dirty trades → kill candidates → real-money status. Not the prettiest chart.

## Where this fits in the roadmap

This is **Week 3-4 work**, layered on top of:
- Kill/pause engine code (provides per-strategy decision state)
- Cluster exposure code (provides cluster panel data)
- Strategy scorecard generator (provides decision recommendations)
- Alpha attribution generator (provides alpha panel data)
- Reset ledger (provides post-reset sample data)
- Blocked-signal logging (provides blocked-reason data)

The dashboard is the LAST layer — it surfaces data the underlying engines produce. Don't build the dashboard pieces before the engines exist; that produces empty panels.

## Page architecture (added 2026-04-27)

The current dashboard mixes three jobs at equal weight: cool system theater + runtime health/NOC + trading decision support. They should be SEPARATE pages.

### 5-page stack

| Page | Purpose | What it answers in <5s |
|---|---|---|
| **1. NOC / Command Center** | Primary operator console | Am I safe? What's broken? What needs action NOW? |
| **2. Fleet Ops** | Per-strategy scorecard / cull / promotion | Which strategies deserve capital? Which to kill? |
| **3. Pipeline** | Neural Core + decision-path diagnostics | Why didn't a strategy trade? Where did the signal die? |
| **4. Risk** | Exposure / notional / margin / cluster caps / sizing anomalies | Where is hidden correlated risk? Sizing bugs? |
| **5. Research** | Legacy data, benchmarks, walk-forward, replay, attribution, reset epochs | Historical analysis (kept OUT of live ops) |

The current "Neural Core" view stays — but as page 3 (Pipeline diagnostics), not as the primary screen. Cool factor is good for diagnosis, wrong for incident response.

### Three separate state dimensions per strategy

A strategy needs 3 simultaneous state labels, not one:

| Dimension | Possible values |
|---|---|
| **Runtime state** | UP / STALE / DOWN / DEGRADED / BLOCKED |
| **Trading state** | FLAT / IN_TRADE / WAITING / NO_TRIGGER / SIGNAL_BLOCKED / RISK_BLOCKED |
| **Decision state** | OBSERVE / KEEP_PAPER / WINNER_CANDIDATE / REWORK / QUARANTINE / KILL_CANDIDATE / KILL |

Example display:
```
forge_multi_orb
  Runtime: UP
  Trading: FLAT
  Decision: KILL_CANDIDATE
```

A single "OK" tile hides the killer fact that decision state = KILL_CANDIDATE while runtime is fine.

### NOC page layout (top-6 panels)

The primary operator page should be exactly 6 panels:

1. **Broker / Mode / Equity / Open Risk** — top header band
2. **Critical Alerts** — what needs action now
3. **Open Positions** — current exposure with risk %
4. **Cluster Exposure** — FX_USD / EQUITY_BETA / SHORT_VOL / DURATION / METALS / etc.
5. **Runner / Heartbeat Health** — fleet operational status
6. **Recommended Actions** — kill candidates, sizing anomalies, blocked-signal patterns

Operator's eye should land in this order: Am I safe? → What's broken? → What's over-risked? → What's stale? → Open positions? → Required actions? → Broader system?

### Data provenance labels (mandatory on every panel)

After resets, every panel must label its data:

```
Source: live paper / legacy archive / real-money
Window: 24h / 7d / 30d / 90d
Epoch: post-reset / pre-reset
Last refresh: <timestamp>
```

Without these, legacy data ghosts contaminate operational decisions.

### Specific changes to existing visuals

- **"UNKNOWN" chip in identity bar** → replace with `SYSTEM STATE: HEALTHY/DEGRADED/CRITICAL`. UNKNOWN creates ambiguity without telling the operator what to do.
- **Single "Fleet Confidence 62%"** → decompose into Operational / Evidence / Execution / Risk / Attribution sub-scores. Blended hides the gaps.
- **"Expected annual 40-75% HARDCODED"** → either remove, or label `MODEL ESTIMATE — NOT VALIDATED`. Front-and-center projected returns bias decisions before evidence supports them.
- **System Status cards** → expand to show all 3 state dimensions (runtime/trading/decision), last heartbeat, signal count, blocked count, position.
- **Promotion progress bars** → replace % with BLOCKED + reason when any critical gate fails. multi_orb at "84%" with negative PF is misleading.
- **Recent Trades** → add risk $, risk %, notional × equity, cluster, validity (clean/dirty/invalid).

### Visual severity hierarchy

The eye should go to severity, not novelty:

- **Red** — immediate action required (recon drift, real-money issue, sizing breach)
- **Orange** — degraded / blocked / stale / needs review
- **Yellow** — caution / thin sample / low confidence
- **Green** — passed / healthy
- **Blue/gray** — informational / inactive / waiting event (vix_revert, fomc_drift between events)

Currently too many things look calm that should look attention-required.

## How to apply this memory

**Why:** the dashboard work is enough to be its own focused workstream. Capturing here keeps it from being lost between architecture memos and code work.

**How to apply:**
- Don't start dashboard upgrades before the underlying capital allocator code exists
- Items 1-5 from the top-10 are paper-relevant; 6-10 are real-money-blocking
- The Capital Safety Bar is priority #1 — surfaces sizing bugs immediately
- The 5-page stack split is the eventual target architecture; the NOC page is the highest-leverage single build (top-6 panels)
- Three-state-dimension labels per strategy are mandatory; single "OK" hides too much
- Data provenance labels are non-negotiable once legacy data exists
- After 5/31, this memo merges into a "dashboard v2" build sprint
