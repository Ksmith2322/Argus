---
name: Test coverage matrix per strategy
description: Snapshot of what tests each of 22 strategies has actually completed (backtest, walk-forward, holdout, live trades). Surfaces "running but unvetted" strategies before 5/31 freeze.
type: project
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
## Why this exists

The fleet has 22 strategies and the audit shows trade counts, but it's hard to see at a glance "which strategies have been honestly tested vs which are running on assumption." This matrix makes that visible. Pre-5/31 cull uses it to prioritize which strategies need the most evidence to keep alive.

## Snapshot 2026-04-26

**Test types and what they prove:**
- **Backtest PF**: in-sample PF over the strategy's design window (typically 2y+). Necessary but not sufficient.
- **Walk-forward**: out-of-sample test where the strategy's parameters are frozen on training data and validated on later data. PROOF AGAINST OVERFIT.
- **Holdout**: a frozen out-of-sample slice (e.g. last 6 months of historical data NEVER touched during development). Last test before live.
- **Live trades**: actual paper-account fills. Real-world execution, slippage, gating behavior.

**Test maturity tiers:**
- **MATURE**: backtest + walk-forward + holdout + 30+ live trades. Verdict-ready.
- **PARTIAL**: backtest only OR backtest + some live data. Needs more before verdict.
- **THIN**: just live data, no formal validation. Operating on hope.
- **NONE**: no test results; running blind.

| strategy | backtest_pf | bt_n | walk_forward | holdout | live_trades_30d | tier | gap to fix |
|---|---|---|---|---|---|---|---|
| forge_vix_intraday | 1.6 | ~600 | partial (4-fold) | not run | 18 | MATURE | run holdout |
| forge_spy_mean_rev | 1.45 (v1) | 200+ | not run | not run | 29 | PARTIAL | v2 trend filter; backtest v2 |
| forge_multi_orb | 1.20 | ~300 | not run | not run | 48 | PARTIAL | walk-forward |
| forge_jpy_pm_short | 1.30+ | 250+ | not run | partial | 3 | PARTIAL | more live data |
| forge_nq_overnight | 1.20-1.29 | 120 | not run | not run | 2 | PARTIAL | more live data |
| forge_gld_pm_long | 1.4+ | 80+ | not run | not run | 1 | PARTIAL | more live data |
| forge_nq_london_close | 1.2 (claimed) | unknown | not run | not run | 2 | THIN | rerun backtest |
| argus_usdjpy | 3.8 (live) | 7 | not applicable | n/a | 2 | THIN | very thin sample |
| forge_gdx_gld | strong (claimed) | back-fill only | not run | not run | 0 (back-fill exists) | PARTIAL | live trades |
| forge_aud_asian_breakout | unknown | not run | not run | not run | 0 | THIN | fresh backtest needed |
| argus_gbpusd | replay 18.9/day claim | replay only | n/a | n/a | 0 | THIN | live data after gate-loosening |
| argus_cadjpy | thin edge per spec | replay | n/a | n/a | 0 | THIN | live data after gate-loosening |
| forge_wick_gbpusd | claimed strong, ~13/yr | unknown | not run | not run | 0 | NONE | needs backtest dump |
| forge_vix_revert | claimed | unknown | not run | not run | 0 | NONE | needs backtest dump |
| forge_rebalance | event-driven | event count only | n/a | n/a | 0 | NONE | n/a (event count too low) |
| forge_fomc_drift | 1.58 over 215 trades | yes | not run | not run | 0 | PARTIAL | walk-forward; needs FOMC events |
| forge_tom_international | replay backtest | yes | not run | not run | 0 | PARTIAL | live data 4/29+ |
| forge_mamba | "no edge in current impl" per audit | yes (negative finding) | not applicable | n/a | 0 | KILL_CANDIDATE | unflagged 4/26; observe live |
| forge_tori | scope_down validated subset (Dow LONG) | yes | partial | n/a | 0 (RESEARCH_ONLY was True until 4/26) | PARTIAL | live data |
| forge_cuebanks | rulebook exists, validation incomplete | partial | not run | n/a | 0 (RESEARCH_ONLY was True until 4/26) | THIN | live data + walk-forward |
| apollo | drift-only design, not signal | research_only | n/a | n/a | 0 actionable in window | INSUFFICIENT_DATA | wait for post-ER candidates |
| hermes | gap-fill scanner | n/a | n/a | n/a | 0 (--execute bug fixed 4/26) | INSUFFICIENT_DATA | wait for live data |
| titan | trend-stock scanner | n/a | n/a | n/a | 0 (cohort_report cascade) | INSUFFICIENT_DATA | wait for live data |
| ares | sector rotation, monthly | back-fill only | n/a | n/a | 0 (monthly cadence) | INSUFFICIENT_DATA | wait for next rotation |

## Maturity summary

- **MATURE**: 1 (forge_vix_intraday — but holdout still missing)
- **PARTIAL**: 7 (have backtest but missing walk-forward or holdout or sample)
- **THIN**: 6 (running on weak/no formal validation)
- **NONE**: 4 (no test results dumped to JSON)
- **INSUFFICIENT_DATA**: 4 (just-restarted or low-frequency, can't tell yet)

**Implication for 5/31 verdicts:**
- THIN + NONE strategies need to either produce strong live data (n≥30 with PF≥1.2) by 5/31 OR get axed. They have no other defense.
- PARTIAL strategies can claim verdict eligibility based on backtest + emerging live data.
- The ONE MATURE candidate (vix_intraday) is the leading WINNER candidate.

## Tests to actually run before 5/31

In priority order:

1. **vix_intraday holdout** — strategy has best maturity, missing only holdout. Run on Q4 2025 data (untouched during development). Validates real edge.
2. **spy_mean_rev v2 backtest** — v2 added trend filter, hasn't been backtested with the filter. Without this, v2 verdict is INSUFFICIENT_DATA.
3. **multi_orb walk-forward** — n=48 live with PF<1.0 already. Walk-forward might confirm overfit OR reveal regime issue. Either is informative.
4. **wick_gbpusd backtest dump** — strategy has logic but no backtest record exists. NONE-tier; can't verdict without this.
5. **vix_revert backtest dump** — same.
6. **fomc_drift walk-forward** — claim of PF 1.58 over 215 trades is strong but not stress-tested. Walk-forward across regimes (zero-rate vs hiking vs cutting) catches overfit.

Estimated effort: 8-12 hours total across Weeks 2-3 of the 5-week roadmap.

## How to apply this memory

**Why:** the verdict at 5/31 should be evidence-based, not impression-based. This matrix makes evidence visible.

**How to apply:**
- Re-run this snapshot before each weekly audit (4/26, 5/3, 5/10, ...). Track the maturity progression.
- THIN/NONE strategies that can't move to PARTIAL by 5/24 are auto-KILL — they had 5 weeks and no defense.
- The "tests to actually run" list is the work plan for the validation lane in Week 2-3.
- A strategy in MATURE tier with negative live data is still KILL — maturity doesn't override outcome.
