# Argus deployment decision — 2026-06-30

**Status**: SKELETON (written 2026-05-25 evening, decision target 2026-06-30)
**Owner**: Operator
**Re-evaluable**: Yes, by re-running the criteria checks listed in §4
**Pre-commit rule**: The criteria + actions in §3 are committed *before*
the evidence arrives. Do not edit §3 between now and 6/30 in response to
the evidence — that's the whole point of pre-commitment. Edits are
allowed only to fix factual errors / undocumented blockers.

---

## 1. Purpose of this document

Force a real go/no-go decision on real-money deployment of Argus by a
specific date, against pre-committed criteria. Without this doc, the
default pattern has been "another 30-day evidence window will tell us"
on repeat — see the 5/22 epoch reset → "30-day clean window" → 5/25
power-cycle finding that no live evidence was actually produced.

The decision space is **5 options**:

| ID | Option | One-line |
|---|---|---|
| **A** | DEPLOY_SMOKE_5K | Move xs_momentum baseline to real-money at $5K cap, learn operational truth |
| **B** | DEPLOY_SMOKE_5K_AFTER_CONSOLIDATION | Consolidate variants first (see §6), then deploy survivor at $5K |
| **C** | FREEZE_ARGUS_RUN_PASSIVE | Operate Argus as research artifact only; deploy live capital in MTUM + GLD + 70/30 SPY/TLT |
| **D** | SUNSET_ARGUS_BUY_PASSIVE | Stop operating Argus entirely; full capital to passive |
| **E** | EXTEND_30_MORE_DAYS | Pre-committed only ONCE; if invoked, this doc auto-promotes to 2026-07-30 |

The doc's job is to make the operator pick exactly one of A-E on 6/30
based on the §3 criteria, not based on whatever post-hoc story feels
compelling that day.

---

## 2. State of the truth surface (as of 2026-05-25 evening)

| Item | Status | Note |
|---|---|---|
| xs_momentum CAGR | **9.96%** (corrected from 18.04%) | See `OPERATOR_HANDOFF.md §0.0`. Bug fix in commit `3c8507f`. |
| xs_momentum MaxDD (20y) | 23.86% | Stable across data refresh. |
| xs_momentum factor alpha (Ken French 4-factor) | p=0.22, R²=0.76 | Strategy is TAA beta, not alpha. See `xs_momentum_factor_decomp_summary.md`. |
| Benchmark vs static portfolios | **Methodology-dependent** | See `static_vs_argus_benchmark.md`. Conservative read: xs_momentum wins on Sharpe/DD/Calmar. Aggressive risk-parity read: xs_momentum loses Sharpe by ~10%. Both defensible. |
| Strategy roles taxonomy | Already exists (`helio/strategy_roles.py`) | OFFENSE / DEFENSE / HEDGE / RESEARCH with PF floors 1.20 / 1.05 / 0.80 / inf. **GAP**: no BETA_TIMING role; xs_momentum is currently OFFENSE but factor decomp says beta. |
| Deployment gate | Already exists (`helio/real_money_preflight.py`) | 13-check role-aware gate. ChatGPT's "two-gate refactor" is largely cosmetic vs what's there. |
| Live evidence since 5/22 reset | 0 fills | canonical_fills.jsonl = 0 bytes for 72h+. Fleet was not running until the 5/26 morning recovery. |
| Replay-bridge | Warming until 2026-06-26 | Activated as decision-input only after that date. |
| Real-money allowlist | `global_enabled=false`, `strategies=[]` | The administrative gate hasn't moved. |
| IBKR live account | Not funded | `REAL_ACCOUNT_ID=None`. |

---

## 3. Pre-committed criteria (DECIDE BEFORE 6/30)

Each option is gated by an explicit `must_be_true` set. Operator on 6/30
walks through the list and picks the option whose gates are met.

### Option A — DEPLOY_SMOKE_5K (xs_momentum baseline only)

All of the following must be true:
- [ ] At least **3 monthly rebalance cycles** with `trade_source=="live"` rows in `canonical_fills.jsonl` (so we have live evidence the dispatcher works end-to-end, not just paper assumption)
- [ ] Live PF over the post-reset window is within `±0.5 PF` of the backtest baseline (currently PF 3.30) — i.e., `1.65 ≤ live_PF ≤ 4.95`
- [ ] `real_money_preflight forge_xs_momentum` returns all GREEN, no RED
- [ ] `replay_health.json` for `forge_xs_momentum` shows `status="ok"` (warming ended 6/26)
- [ ] IBKR live account funded and `REAL_ACCOUNT_ID` populated in env
- [ ] `real_money_allowlist.json` updated with operator signature + ledger_entry_id for `forge_xs_momentum` only
- [ ] `global_enabled=true` flipped *after* all above
- [ ] Capital ladder shows `eligible_stage="SMOKE_5K"` (not LIVE_10K — that requires 75 live fills which we won't have)
- [ ] Operator personally re-reads the 9.96% CAGR finding and accepts that real-money expected gross is **~$500/yr** at the SMOKE_5K cap

**Action if all true**: Operator runs `python -m ops.real_money_preflight forge_xs_momentum --apply` (must be added; doesn't exist yet) and confirms the trade in Discord before the first live order.

### Option B — DEPLOY_SMOKE_5K_AFTER_CONSOLIDATION

All of Option A's criteria PLUS:
- [ ] Variant consolidation completed (see §6 — kill `legacy15` and `style`, retain 5 variants)
- [ ] xs_momentum_consensus shadow runner has logged ≥4 monthly evaluations and the consensus picks match at least 3 of the 5 individual variants on average (validates the consolidation thesis)
- [ ] `static_vs_argus_benchmark.md` re-run with the consolidated roster shows Sharpe ≥ 0.50 on the conservative methodology (sum-capped weights, daily-rf-subtracted Sharpe)

**Action if all true**: Same as Option A, but on the consolidated roster.

### Option C — FREEZE_ARGUS_RUN_PASSIVE

Pick this if **any** of the following:
- [ ] Live PF over post-reset window is `< 1.20` (the OFFENSE floor) — strategy is underperforming its own gate
- [ ] `replay_health.json` shows `status="drift"` with ≥3 blocking mismatches — live behavior diverged from backtest
- [ ] `real_money_preflight forge_xs_momentum` has any unfixable RED check
- [ ] Operator does not want to fund / approve the IBKR live account by 6/30
- [ ] Operator concludes the $500/yr SMOKE_5K expected gross doesn't justify the maintenance time

**Action if picked**:
1. Set `allocation_factor` to 0.0 for all xs_momentum variants in `allocation_factors.json` with kill_log entry citing this doc
2. Argus remains operationally running as a research/proof platform (paper only; useful for ongoing strategy hunt)
3. Operator transfers actual capital to: 50% MTUM, 25% GLD, 17.5% SPY, 7.5% TLT (rough proxy for the role Argus plays). Use IBKR live account or any brokerage.
4. Schedule a re-evaluation 2026-12-30 (6 months later) to consider whether to revive

### Option D — SUNSET_ARGUS_BUY_PASSIVE

Pick this if **any** of the following:
- [ ] Operator decides the research-platform value of Argus doesn't justify the operational maintenance (electricity, scheduled tasks, IBKR connectivity babysitting, repo updates, etc.)
- [ ] More than 2 power-outage / recovery incidents have happened since 5/25 (operational fragility is the real cost)
- [ ] No new edge candidate has passed the disciplined gate in 60 days (edge pipeline is dry)
- [ ] Operator wants the headspace back

**Action if picked**:
1. All scheduled tasks disabled (one-time `ops/decommission_argus.ps1` — must be added)
2. Repo archived as-is on `audit/argus-system-review`
3. Memory + handoff snapshotted into a single `ARGUS_FINAL_STATE.md` for future reference
4. Capital fully into the static portfolio from Option C
5. Optional: post-mortem written within 30 days

### Option E — EXTEND_30_MORE_DAYS

**This option can be used at most ONCE.** If invoked on 6/30:
- This doc auto-promotes to 2026-07-30 with the same criteria
- Operator must write 2-3 sentences justifying the extension (what specifically will change in 30 days?)
- If 7/30 still doesn't meet Option A/B criteria, fallback is **automatically** Option C (FREEZE), no further extension

The point: don't let "another 30 days" become an infinite loop.

---

## 4. Inputs to the 6/30 check (re-runnable)

On the morning of 6/30, run these in order and capture the output:

```powershell
$env:PYTHONPATH = "c:\Argus\repo"
$env:PYTHONIOENCODING = "utf-8"
$py = "C:\Argus\.venv\Scripts\python.exe"

# 1. Live evidence count
& $py -X utf8 -c "import json; d=json.loads(open('argus_flow/logs/canonical_fills.jsonl').read().splitlines()[-1] if open('argus_flow/logs/canonical_fills.jsonl').read().strip() else '{}'); print(d)"
# (or just measure file size + last write time)

# 2. Replay-bridge health
& $py -X utf8 -m ops.audit.run_replay_health_check

# 3. Preflight verdict
& $py -X utf8 -m ops.real_money_preflight forge_xs_momentum

# 4. Fresh benchmark vs static portfolios
& $py -X utf8 -m ops.audit.run_static_vs_argus_benchmark

# 5. Fleet snapshot
& $py -X utf8 -m ops.audit.run_fleet_snapshot --skip-yfinance

# 6. Roster state
& $py -X utf8 -m ops.audit.run_roster_state

# 7. Capacity stress
& $py -X utf8 -m ops.audit.run_capacity_stress
```

Capture all outputs to `docs/decisions/2026_06_30_inputs/` so the
decision audit trail is complete.

---

## 5. The dollar-edge math (operator-time honesty)

At a $250K paper anchor, the corrected 9.96% CAGR projects to:

| Stage | Capital cap | Expected gross/yr | After tax + drag | Operator-time-equivalent (1h/wk @ $150/h retail) |
|---|---:|---:|---:|---:|
| SMOKE_5K | $5,000 | ~$500 | ~$300 | -$7,500 |
| LIVE_10K (requires 90d + n=75) | $10,000 | ~$1,000 | ~$600 | -$7,200 |
| LIVE_25K | $25,000 | ~$2,500 | ~$1,500 | -$6,300 |
| LIVE_100K (full ladder; 6.25 years per strategy at monthly cadence) | $100,000 | ~$10,000 | ~$6,000 | -$1,800 |

**Implication**: the breakeven point against operator time (assuming 1h/week
maintenance, which is optimistic given recent power-cycle recoveries)
is around the LIVE_100K stage, which requires 6.25 years of monthly
fills to qualify per the capital_ladder gate. The honest path to
real-money-justifies-operator-time is multi-year.

This isn't a reason to pick Option D (sunset) — research-platform value
is real and the cost is mostly capital-marginal, not time-marginal —
but it IS a reason to be ruthless about Option A's $500/yr expected
gross when deciding whether SMOKE_5K is worth the IBKR account funding.

---

## 6. Variant consolidation (input to Option B)

Per the 5/25 cluster-correlation agent finding (not the original
"7→1" thesis, which was empirically wrong):

| Variant | Current alloc | Action | Reason |
|---|---:|---|---|
| forge_xs_momentum (broad_8) | 1.0× | KEEP | proven baseline, full disciplined-gate pass |
| forge_xs_momentum_sectors | 0.25× | KEEP | low correlation (0.07 to baseline) |
| forge_xs_momentum_style | 0.5× | **KILL** | dominated by style_top3 (same universe, top-3 has higher Sharpe + CAGR) |
| forge_xs_momentum_legacy15 | 0.25× | **KILL** | duplicated by legacy15_regime (same universe + better DD via regime gate) |
| forge_xs_momentum_style_top3 | 0.5× | KEEP | dominates `style` |
| forge_xs_momentum_legacy15_regime | 0.25× | KEEP | dominates `legacy15` |
| forge_xs_momentum_global47 | 0.25× | KEEP | walk-forward STRENGTHENED OOS |
| forge_tail_hedge | 0.1× | KEEP | only HEDGE-role strategy |
| forge_gld_pm_long | 0.5× | KEEP | DEFENSE role; metals diversifier |
| forge_tom_spy | 0.3× | KEEP | low correlation (0.03 to xs_momentum) |
| forge_nov_spy | 0.2× | KEEP | calendar effect, 1 trade/year |

Net: 11 → 9 strategies, sum 4.10× → 3.35×.

Kill status code: `CONSOLIDATED_REDUNDANT_CLUSTER_MEMBER` (added to
`helio/roi_filter.py` as part of this decision doc batch).

Re-run `static_vs_argus_benchmark.md` after consolidation to update
the Sharpe/DD comparison. The 9-strategy consolidated roster may
slightly improve incremental Sharpe by reducing intra-cluster
correlation overlap.

---

## 7. What this doc does NOT decide

- **New strategy candidates** (PEAD reformulation, options sleeves, etc.)
  — separate decision, separate doc when ready
- **Universe expansion** beyond the current 11-strategy roster
  — defer until at least one of A/B is invoked and operates cleanly for 60 days
- **Infrastructure refactors** (purpose enum rename, two-gate split)
  — already 90% in `strategy_roles.py` + `real_money_preflight.py`. Tune
  thresholds if needed; don't rebuild.
- **CAGR re-bug check** — covered by the regression tests
  `test_calendar_walk_includes_every_month_after_warmup` +
  `test_backtest_cagr_matches_calendar_walk_compound` +
  `test_monthly_returns_field_uses_calendar_walk`. Run tests as
  part of step §4 if doubt arises.

---

## 8. Why pre-commit matters

The 5/22 reset's implicit promise was "this 30-day clean window will
tell us." Five weeks later (today, 5/25), the window has produced
0 fills (fleet wasn't running) and we're 3 days into a *second*
30-day window. Without pre-committed criteria + a hard fallback to
Option C/D after one EXTEND_30, the pattern is to keep deferring.

This doc's value is not the criteria themselves — they're approximate.
The value is that they're committed *now*, before the evidence arrives.
The 6/30 decision then becomes mechanical: check the boxes, pick the
option whose gates are met, execute.

If you want to change criteria, change them now. Not on 6/30.

---

**End of skeleton.** Decision doc is pre-committed; criteria are
checkable; operator path on 6/30 is mechanical. Add/edit criteria
before 6/30 if needed; do not edit between now and 6/30 in response
to live evidence.
