---
name: Capital Promotion Ledger — canonical governance audit trail
description: Every capital decision (paper tier change, real-money allocation, pause, demote, kill) gets one entry in capital_promotion_ledger.jsonl. Source of truth for "why is strategy X at allocation Y?"
type: project
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
## Why this exists

Verdicts (`verdict_<date>.json`) capture review-day decisions. Strategy specs capture the intended logic. Heartbeats capture runtime state. None of these answer "why is `forge_vix_intraday` at $1500 real-money allocation as of 6/15? Who approved? What was the evidence?"

That's a capital decision audit gap. The Capital Promotion Ledger fills it.

Without it: future-you (or future-claude) forgets why a strategy is at a particular state. Decisions drift. Real-money allocations creep up without rigor. This is the failure mode the reviewer flagged.

## File location + format

```
argus_flow/logs/capital_promotion_ledger.jsonl
```

JSON Lines, one entry per decision. Append-only. Never edited; if a decision is reversed, write a NEW entry that supersedes.

## Schema

```json
{
  "ts": "2026-05-01T20:00:00Z",
  "strategy": "forge_vix_intraday",
  "decision": "verdict",
  "previous_state": {
    "stage": "Paper-Live Valid",
    "verdict": "OBSERVE",
    "allocation_usd": 0,
    "risk_pct": 0.005
  },
  "new_state": {
    "stage": "WINNER-CANDIDATE",
    "verdict": "WINNER-CANDIDATE",
    "allocation_usd": 0,
    "risk_pct": 0.01
  },
  "evidence_used": {
    "trade_count": 18,
    "trade_count_window_days": 30,
    "profit_factor": 1.45,
    "expectancy_R": 0.31,
    "max_drawdown_R": 1.2,
    "p_expectancy_positive": 0.78,
    "runtime_clean_days": 21,
    "recon_status": "clean"
  },
  "waivers": [],
  "approver": "ksmith2322",
  "next_review_date": "2026-05-15",
  "decision_record_path": "argus_flow/logs/verdict_20260501.json",
  "notes": "Clearest signal in fleet. Bumped from unproven (0.5%) to candidate (1%) tier within paper. Not real-money eligible until n>=30 and ladder advances to REAL-CANDIDATE."
}
```

## Decision types

| `decision` value | When to use |
|---|---|
| `verdict` | Periodic review (5/1, 5/15, 5/31, etc.) — labels updated, tier may change |
| `tier_bump` | Paper risk_pct increase (within paper bounds) |
| `tier_demote` | Paper risk_pct decrease |
| `quarantine` | Move to QUARANTINE due to implementation issue |
| `kill` | Stop the strategy (terminal state — but new entries can re-add later if redesigned) |
| `real_pilot_start` | First real-money allocation |
| `real_allocation_change` | Real-money allocation changed |
| `real_pause` | Real-money trading paused (NOT killed — can resume) |
| `real_resume` | Real-money trading resumed |
| `real_demote_to_paper` | Removed from real, kept on paper |
| `restructure` | Strategy logic materially changed (counts as new strategy effectively) |

## Required fields per decision type

| Field | All | verdict | tier_bump | real_pilot_start |
|---|---|---|---|---|
| ts, strategy, decision | ✓ | ✓ | ✓ | ✓ |
| previous_state, new_state | ✓ | ✓ | ✓ | ✓ |
| evidence_used | ✓ | ✓ | ✓ | ✓ |
| approver | ✓ | ✓ | ✓ | ✓ |
| next_review_date | ✓ | ✓ | ✓ | ✓ |
| waivers | optional | optional | optional | **REQUIRED** |
| decision_record_path |  | ✓ |  | ✓ (link to readiness gate) |
| risk_drill_results |  |  |  | **REQUIRED** |
| capital_source |  |  |  | **REQUIRED** (must reference real-money gate sign-off) |

## Real-money entries are extra-rigorous

For `real_pilot_start`, `real_allocation_change`, etc., these fields are required:

```json
{
  "real_money_specific": {
    "readiness_gate_signed_off_at": "2026-05-28T14:30:00Z",
    "readiness_gate_path": "argus_flow/logs/readiness_gate_20260528.json",
    "waivers_active": [],
    "drill_results": {
      "kill_switch_drill_passed_at": "2026-05-11T15:30:00Z",
      "daily_loss_drill_passed_at": "2026-05-12T16:00:00Z",
      "manual_override_drill_passed_at": "2026-05-15T17:00:00Z"
    },
    "real_pf_30d": null,
    "max_real_drawdown_pct": null
  }
}
```

`real_pf_30d` and `max_real_drawdown_pct` are null at pilot start; populated retroactively in subsequent entries.

## Append rules

- **Append-only.** Never edit an entry. If wrong, write a corrective entry with a note.
- **Every capital decision = one entry.** If you bump a tier and update a verdict in the same review, that's TWO entries.
- **Approvals must be signed.** `approver` field is the user's identity (e.g., `ksmith2322`). For automated decisions (e.g., kill rule auto-fired), approver is the rule name (e.g., `auto_kill_rule:pf_below_1_at_n_50`).
- **Waivers tracked.** If any 20-point gate item is waived, list it in `waivers`. The waiver count cap (3 per `project_real_money_readiness_gate_20260531.md`) is enforced from this ledger.

## Querying the ledger

Common questions and their queries:

**"Why is X at its current state?"**
```bash
grep '"strategy": "forge_vix_intraday"' argus_flow/logs/capital_promotion_ledger.jsonl | tail -5
```

**"All real-money decisions ever made:"**
```bash
grep -E '"decision": "real_' argus_flow/logs/capital_promotion_ledger.jsonl
```

**"Strategies that were demoted in last 30 days:"**
```bash
# (requires jq)
jq 'select(.decision=="tier_demote" and .ts > "2026-05-01")' argus_flow/logs/capital_promotion_ledger.jsonl
```

## Implementation: when to start writing entries

The ledger is **prospective** — start logging from now (2026-04-26) forward. Backfilling historical decisions is optional. Most useful entries will be:

- 5/1 review verdicts (each strategy gets a `verdict` entry)
- Week 2 cull actions (`tier_demote`, `kill`, `quarantine` entries)
- Week 3 risk drill completions (referenced in subsequent `real_pilot_start` entries)
- 5/31 final verdicts
- 5/31+ real-money decisions

By 6/30, the ledger should have ~50-100 entries reflecting every capital-relevant decision since 4/26.

## How to apply this memory

**Why:** without this, decision drift is guaranteed. The reviewer's critique was specifically that "future you will forget why a strategy was promoted, killed, paused, or allowed real-money exposure."

**How to apply:**
- For every verdict / tier change / real-money decision: append to the ledger.
- Before any "where are we with X" question: read the ledger entries for X first.
- The ledger is the source of truth for governance. Verdicts and allocations stored elsewhere must agree with the ledger; if they disagree, ledger wins.
- Real-money entries get extra fields. Don't skip them — those fields exist for audit reasons.
