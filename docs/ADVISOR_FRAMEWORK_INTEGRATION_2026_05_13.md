# Advisor Framework Integration — Scoping Decision

**Date:** 2026-05-13
**Context:** External advisor proposed 10 audit framework additions on top of the v2 per-strategy audit. This document classifies each item as ALREADY-COVERED, PARTIAL, or NEW; ranks the genuinely new items by build effort vs decision value; and identifies what to integrate now vs defer to post-5/31 freeze.

---

## Coverage matrix

| Advisor proposal | Status | Where covered |
|---|---|---|
| Causality / leakage audit | PARTIAL | `helio/roi_proof_core.py` has SPY-benchmark IR but no formal leakage check (e.g. "would this signal be visible at decision time given data lag?") |
| Implementation parity (live vs backtest) | PARTIAL | `paper_live_ir_delta_abs` metric in ROI proof. Not yet per-strategy automatic |
| Opportunity ledger | YES | `signals.csv` per strategy (every evaluation logged); `argus_flow/ops/block_outcome_v2.py` evaluates counterfactual P&L on blocked signals |
| Negative-control tests (randomized entry, direction flip) | **NEW** | Not present |
| Ablation studies (drop one filter, measure delta) | **NEW** | Not present |
| Failure-mode replay (replay past incidents to verify guards) | **NEW** | Not present |
| 10-layer audit (PASS/WARN/FAIL/UNKNOWN) | PARTIAL | `project_real_money_readiness_gate_20260531.md` has 20-point checklist + ≤3 waiver budget. Not formalized as a runnable engine |
| Machine-generated Strategy Vetting Report (`ops/audit/run_strategy_vetting.py`) | **NEW** | Need to build — aggregator over existing artifacts |
| `decision_usefulness` field per strategy | **NEW** | Not present (would be a metadata field on each strategy) |
| Triage-first audit (deep-audit only top 3 + currently-live) | PROCESS, not code | Adopt in next audit cycle |

---

## Build-effort ranking of genuinely new items

| Item | Build effort | Decision value | Status |
|---|---|---|---|
| **Machine-generated vetting report** | 1-2 days (aggregator over existing JSON/CSV) | HIGH (operationalizes audit; cheaper future audits) | Build this week |
| **`decision_usefulness` field** | 0.5 day (add to scorecard JSON + readiness gate) | MEDIUM (forces explicit "would this strategy's signal change a real decision?" question per strategy) | Build this week |
| **Negative-control tests** | 1-3 days per strategy (randomized entry, sign flip, etc.) | HIGH for strategies with marginal edge (e.g. argus_cadjpy PF 1.01 if it ever gets live trades) | Run AFTER strategy has ≥30 live trades, not before |
| **Ablation studies** | 0.5-1 day per strategy | MEDIUM (confirms each filter is pulling weight) | Run on top-3 candidates only |
| **Failure-mode replay** | 2-3 days for the engine, 0.5 day per replay | LOW-MEDIUM (defensive only; doesn't change edge) | Deferred to post-5/31; build only if a fresh failure motivates |

---

## Decision

**Do this week (before 5/31 freeze):**
1. Build `ops/audit/run_strategy_vetting.py` — aggregates capital ladder state, ROI proof verdict, ops_reliability streaks, allocation_factor, scorecard, and produces a per-strategy PASS/WARN/FAIL/UNKNOWN summary. Operationalizes the readiness gate.
2. Add `decision_usefulness` to the scorecard JSON — operator-set, three values: `BLIND` (signal would not change any decision), `CONFIRMS_OTHER` (overlaps with another strategy's signal), `STANDALONE_USEFUL` (signal would drive a decision no other strategy provides).

**Defer past 5/31 freeze:**
- Negative-control tests: meaningless before strategy has live sample. Run as part of the post-5/31 promotion gate, not as a pre-freeze filter.
- Ablation studies: same reason. Only useful AFTER a strategy looks like a winner and we need to confirm WHICH inputs are pulling weight.
- Failure-mode replay engine: defensive build. Schedule a session if a fresh failure motivates it.

**Process change (adopt immediately):**
- Triage-first audit: future audits do a shallow pass over the whole fleet (5 min/strategy) to identify the top-3 + currently-live, then deep-dive only those. The v1+v2 "audit every strategy equally" pass was the wrong allocation of audit budget.

---

## What this completes vs the advisor's note

The advisor's strongest strategic recommendation was "do not audit all 17 equally first." That's adopted — future audits start with triage, deep-dive selectively.

The most leveraged new build is the machine-generated vetting report (item 4) — it makes EVERY future audit cheaper. The `decision_usefulness` field (item 5) is the cheapest forcing function for clarity on whether a strategy is actually contributing to fleet-level decisions or just generating noise.

The advisor's negative-control / ablation / failure-replay items are correct in principle but premature in timing — apply them when we have a winner candidate, not when filtering 17 strategies most of which will die in the freeze.
