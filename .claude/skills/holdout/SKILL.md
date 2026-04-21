---
name: holdout
description: Run OOS holdout evaluation for the 8 frozen scope_down subsets. Shows verdict per strategy (HOLDING / DEGRADED / OOS_NEGATIVE / INSUFFICIENT_OOS / WAITING). Uses canonical_fills since frozen_at as out-of-sample truth.
allowed-tools: Bash
---

Run the prospective holdout evaluation on all frozen validated subsets.

Each `strategy_confidence/*_validated.json` or `*_ym.json` has a `holdout_freeze` block capturing the in-sample PF/WR/n at the time the scope_down filter was identified. This skill compares OOS fills (canonical_fills.jsonl after frozen_at, excluding backfill) against the frozen claim.

1. Run the evaluator:
   ```bash
   cd C:/Argus/repo && C:/Argus/.venv/Scripts/python.exe -m helio.holdout_eval
   ```

2. Read the generated reports under `strategy_confidence/_holdout/*_oos.json` and summarize:
   - `frozen_at` date
   - `window_open` flag (whether `oos_eval_earliest` has arrived yet)
   - `oos_stats.n` (count) and `oos_stats.pf`
   - `in_sample_claim.pf` for comparison
   - `verdict` string (HOLDING / DEGRADED / OOS_NEGATIVE / INSUFFICIENT_OOS / WAITING)

3. Present as a clean table:
   | Strategy | Frozen | Window | n OOS | In-sample PF | OOS PF | Verdict |

4. Flag anything with verdict DEGRADED or OOS_NEGATIVE as actionable — those are kill candidates.

5. If window hasn't opened yet (WAITING), report days remaining until `oos_eval_earliest`.

Context: scope_down subsets are found by in-sample selection — the reported backtest PF is inflated. Honest evaluation requires frozen hypotheses + post-freeze data. Windows open ~2026-05-21 (30 days after freeze date).
