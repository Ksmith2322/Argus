---
name: Kill / Pause Engine — 5-layer loss containment
description: Defines stop-trigger rules at trade / strategy / instrument / cluster / fleet levels. Trade stops alone don't protect against bad strategy logic, correlated losses, or stale data. Implementation Week 3.
type: project
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
## Why this exists

Trade-level stop-losses only protect against single bad trades. They don't catch:
- A strategy slowly bleeding through bad fills + commissions
- Multiple "different" strategies stacking the same correlated loss
- Stale data driving false signals
- Broken broker reconciliation hiding the truth
- Regime mismatch where a strategy's edge has structurally evaporated
- Overtrading where the strategy fires too often vs replay expectation

Each of those is a different failure layer. Each needs a kill or pause rule.

## The 5 layers

### Layer 1 — Trade kill (already exists)

| Trigger | Action |
|---|---|
| Stop-loss hit | Close at market |
| Time stop hit (per `hold_bars`) | Close at next bar |
| Invalidation flag (manual or detected) | Close + mark invalid |

Status: implemented in every strategy. Standard, no changes needed.

### Layer 2 — Strategy kill / pause (NEW for Week 3)

Each strategy gets per-strategy circuit breakers, separate from fleet-level rules.

| Trigger | Action | Auto/Manual |
|---|---|---|
| 3 consecutive losing trades | Discord WARN, no halt | Auto-flag |
| -2% strategy daily loss (vs strategy's allocation, not fleet) | Pause strategy 24h | Auto |
| -5% strategy drawdown peak-to-trough | Pause + investigate | Auto pause, manual resume |
| `slippage_score < 0.5` for 5+ trades | Throttle (halve risk_pct) | Auto |
| 2+ REAL_ENTRY FAILED in 24h | Pause + investigate IBKR connection | Auto |
| Operational maturity DEGRADED 3+ days | Demote tier per allocator | Auto |
| Replay-vs-live signal frequency < 20% | Strategy has gate-mismatch issue → flag for REWORK | Auto-flag |

Implementation: each runner checks `strategy_kill_state.json` at top of each cycle. If paused, skip evaluation + log. Resume requires explicit user action OR auto-resume timer expiry (24h for daily-loss pause).

### Layer 3 — Instrument kill (NEW)

When a specific instrument's market state is unsafe, halt all strategies trading it.

| Trigger | Action | Scope |
|---|---|---|
| Spread > 3× normal (per pair) | Block new entries on that instrument | Cross-strategy |
| Bid/ask data stale > 60s during session | Block new entries on that instrument | Cross-strategy |
| Volatility spike > 4σ from 20d distribution | Block new entries (potential disorderly market) | Cross-strategy |
| Broker disconnect for that contract specifically | Halt instrument until reconnect + reconcile | Cross-strategy |

Example: if UVXY has a spread blowout (spread > 3× normal), all of vix_intraday + vix_short + vix_revert + multi_orb's UVXY exposure halts simultaneously. Resumes when spread normalizes.

### Layer 4 — Cluster kill (NEW)

Per `project_cluster_exposure_model.md` — when a cluster cap is breached or pattern of cluster losses appears, halt the cluster.

| Trigger | Action |
|---|---|
| Cluster exposure > 100% of cap | Block all new entries in cluster |
| Cluster cumulative daily loss > 1.5% equity | Halt cluster 24h |
| **VIX prints > 30** | Force-close all SHORT_VOL cluster positions immediately (vol-of-vol regime change) |
| 3+ strategies in same cluster all DEGRADED simultaneously | Cluster pause until investigation |

VIX > 30 force-close is a hard fleet-level rule — not configurable, not optional. Short-vol convexity tail event is the one that destroys retail accounts.

### Layer 5 — Fleet kill (NEW + existing partial)

**Real account vs paper account distinction matters.** Paper is the QA environment — drawdowns there are testing artifacts and don't trigger panic. Real account drawdowns are what matter.

| Trigger (REAL account) | Action |
|---|---|
| Real account daily loss > -2% | Pause real-money entries 24h |
| Real account daily loss > -5% | WARN, Discord alert, review required |
| **Real account drawdown > -10%** (LOCKED 4/26 user panic threshold) | **HARD STOP all real-money entries, flat all real positions at next valid boundary, manual reassessment required before restart** |
| Reconciliation drift > 1% equity for 60+ min | Halt real-money fleet, manual reconcile |
| Broker disconnect > 5 min during market hours | Pause real-money entries (existing positions managed by IBKR-side stops) |
| KILL_SWITCH file present | Halt fleet entries within 60s |

| Trigger (PAPER account) | Action |
|---|---|
| Paper drawdown of any size | NO automatic action — paper is QA. Logged for reference but doesn't halt anything. |
| Paper exhibiting fleet-wide pattern of losses | Used as SIGNAL for strategy review, not as kill trigger |

The -10% real-account threshold is **locked from user decision 2026-04-26**. Tighter than the original default; reflects the user's "real losses should be small" principle. Built into the engine, not configurable per-strategy.

When the -10% real stop fires:
1. All real-money entries halted
2. Existing real positions held (their IBKR stops still apply); no auto-flatten unless drawdown deepens
3. Discord URGENT alert
4. Review trigger: was it one strategy or fleet-wide? Strategy or fleet bug? Regime change?
5. Restart only after root cause is identified + documented in `capital_promotion_ledger.jsonl`

## State file structure

`argus_flow/logs/kill_pause_state.json`:

```json
{
  "ts": "2026-05-15T18:30:00Z",
  "fleet": {
    "halt": false,
    "pause_entries": false,
    "reason": null,
    "auto_resume_at": null
  },
  "clusters": {
    "SHORT_VOL": {"halt": false, "reason": null},
    "EQUITY_BETA": {"halt": false, "reason": null}
  },
  "instruments": {
    "USDJPY": {"halt": false, "reason": null},
    "UVXY": {"halt": false, "reason": null}
  },
  "strategies": {
    "forge_vix_intraday": {
      "halt": false,
      "pause_until": null,
      "consecutive_losses": 0,
      "daily_pnl_pct": 0.4,
      "slippage_5trade_avg": 0.85
    }
  }
}
```

Every runner reads this at cycle start. Daemon writes at 5-min interval.

## Auto vs manual escalation

| Trigger severity | Action |
|---|---|
| Yellow (single layer warn) | Discord notify, no halt |
| Orange (layer kill, auto-resume on timer) | Discord alert, auto-resume after timer |
| Red (multi-layer or fleet-level) | Discord URGENT, manual resume only |
| Black (panic drawdown -20%+) | Discord URGENT + flatten positions, manual reset required |

Resume from Red/Black always requires:
1. Root cause identified + documented
2. Fix applied (if applicable)
3. Reconciliation clean
4. User explicit sign-off via removing `MANUAL_RESUME_REQUIRED` lock file

## Implementation work — Week 3

| Day | Work | Output |
|---|---|---|
| Mon 5/11 | Build `helio/kill_pause_engine.py` with state model | module exists |
| Tue 5/12 | Wire trade-kill (existing) + strategy-kill (new) into all 22 runners | per-strategy circuits live |
| Wed 5/13 | Wire instrument-kill + cluster-kill | cross-strategy halts work |
| Thu 5/14 | Drill: inject -10% drawdown synthetically, verify fleet pause + Discord alert | drill passes |
| Fri 5/15 | Drill: VIX > 30 simulation, verify SHORT_VOL force-close | drill passes |
| Sat 5/16 | Documentation + audit pass | all layers covered |

This is part of Week 3 risk hardening. Falls within freeze rules (risk-control fix, not new strategy).

## Anti-patterns

- **"Just relax the threshold this once"** — defeats the purpose. Either the threshold is wrong (change it deliberately with a memo) or it should fire as designed.
- **"It paused but I'll override and let it trade"** — overriding a pause without root cause = the system has a hole. Either the pause was correct (don't override) or the rule is wrong (fix the rule).
- **"Add a 6th layer"** — 5 layers is enough. More layers = more confusion about which fired and why.

## How to apply this memory

**Why:** trade stops are not enough. Real-money survival depends on multi-layer kill logic.

**How to apply:**
- Week 3 implementation is mandatory; on the BLOCKS-FLEET liability list.
- Locked thresholds (-10 pause, -20 halt, -25 escalation) are not configurable per-strategy.
- VIX > 30 SHORT_VOL force-close is hard-coded; do not bypass.
- Resume from Red/Black always requires manual action; the engine cannot self-heal from these.
