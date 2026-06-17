---
name: Strategy Scorecard — objective ranking format
description: Standard scorecard every strategy is graded against. Replaces narrative verdicts with computed scores. Fields cover edge, execution, risk, regime fit, correlation, sample. Weekly cadence; output drives capital decisions.
type: project
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
## Why this exists

Without an objective scorecard, verdicts drift toward narrative ("this one feels good"). The scorecard makes ranking computable. Output is a single `final_score` per strategy that maps to a `recommended_action` from the 6-tier verdict label set.

Pairs with `project_evidence_promotion_ladder.md` (which defines stages) and `project_capital_allocator_policy.md` (which translates score → allocation).

## Output format — `argus_flow/logs/strategy_scorecard.jsonl`

One entry per strategy per scoring cycle (weekly). Append-only.

```json
{
  "ts": "2026-05-04T20:00:00Z",
  "strategy": "forge_vix_intraday",
  "instrument": "UVXY",
  "ladder_stage": "Paper-Live Valid",
  "trade_count_30d": 18,
  "trade_count_90d": 42,
  "expectancy_R": 0.31,
  "profit_factor_30d": 1.45,
  "profit_factor_90d": 1.38,
  "win_rate_30d": 0.55,
  "avg_win": 22.5,
  "avg_loss": -14.8,
  "max_drawdown_R": 1.2,
  "recent_10_trade_expectancy_R": 0.28,
  "recent_30_trade_expectancy_R": 0.31,
  "slippage_score": 0.85,
  "latency_score": 0.92,
  "fill_quality_score": 0.88,
  "runtime_cleanliness_days": 21,
  "reconciliation_status": "clean",
  "correlation_penalty": 0.05,
  "regime_fit_score": 0.78,
  "execution_penalty": 0.12,
  "sample_confidence": 0.62,
  "final_score": 67.4,
  "recommended_action": "WINNER-CANDIDATE",
  "current_action": "WINNER-CANDIDATE",
  "delta": "no change",
  "notes": "Cleanest signal in fleet. Approaching REAL-CANDIDATE if n hits 30 with PF holding."
}
```

## Field definitions

**Edge fields** (the trading core):
- `trade_count_30d` / `trade_count_90d` — valid trades only (excludes invalidity-flagged trades)
- `expectancy_R` — average R per trade. R = profit/risk_at_entry
- `profit_factor` — gross profit / gross loss
- `win_rate` — fraction of winning trades
- `avg_win` / `avg_loss` — in dollars or R
- `max_drawdown_R` — worst peak-to-trough drawdown in R units
- `recent_10/30_trade_expectancy_R` — recency-weighted; catches degradation

**Execution fields** (penalties for sloppy fills):
- `slippage_score` — 1.0 = paper-perfect, 0.5 = 50% of expected R lost to slippage. Computed: `1 - (slippage_R / expected_R)`
- `latency_score` — 1.0 = sub-100ms, scales down. Real-money critical.
- `fill_quality_score` — 1.0 = no rejections / partials / missed exits. Each issue subtracts.

**Operational fields** (system health):
- `runtime_cleanliness_days` — consecutive days without DEGRADED/DOWN status
- `reconciliation_status` — `clean` / `drift` / `unverified`

**Risk-context fields**:
- `correlation_penalty` — 0.0 = orthogonal, 1.0 = fully correlated to a higher-priority strategy. Penalty for stacking same bet.
- `regime_fit_score` — 0.0 = current regime mismatches strategy's tagged conditions, 1.0 = perfect match
- `sample_confidence` — derived from trade_count + variance; floors final_score when sample is thin

**Alpha attribution fields** (CRITICAL — separates real edge from beta):
- `benchmark_label` — what the strategy is being measured against (e.g., `passive_UVXY` for vix_intraday, `passive_SPY` for spy_mean_rev, `passive_GLD` for gld_pm_long, `random_entry_session_breakout` for FX)
- `benchmark_return_window` — same window as the strategy, same instrument(s), same fees applied
- `alpha_R` — strategy_return_R minus benchmark_return_R over the comparison window
- `beats_benchmark` — boolean, `alpha_R > 0`
- `alpha_t_stat` — significance of alpha vs zero (rough proxy: `alpha_R / (volatility / sqrt(n))`)

A strategy that doesn't beat its dumb benchmark with positive `alpha_R` over n ≥ 30 trades **is not edge — it's beta riding fees**. The scorecard floors `final_score` to ≤ 50 (KEEP-PAPER ceiling) for any strategy with `beats_benchmark = false` regardless of other field values.

Per-strategy benchmarks (initial assignments):
| Strategy | Benchmark |
|---|---|
| forge_vix_intraday | passive UVXY hold over same intraday windows |
| forge_spy_mean_rev | passive SPY buy-at-bar-close, exit-at-target-time |
| forge_gld_pm_long | passive GLD entry-at-bar-close, hold N bars |
| forge_multi_orb | passive equal-weight basket of its tickers |
| forge_gdx_gld | passive GDX-GLD spread (no signal) |
| argus_usdjpy / gbpusd / cadjpy | session breakout no-signal baseline |
| forge_jpy_pm_short | random short-bias session entries |
| forge_nq_overnight / nq_london_close | passive MNQ session-close-to-open |
| forge_aud_asian_breakout | passive AUDUSD Tokyo session range |
| forge_wick_gbpusd | random GBPUSD daily entries |
| forge_fomc_drift | passive SPY hold across FOMC days |
| forge_tom_international | passive equal-weight ETF basket on entry days |
| forge_vix_revert | passive SPY long after VIX > 25 |
| forge_mamba / tori / cuebanks | passive MYM session-close baseline |
| apollo | passive ER stock equal-weight basket |
| hermes | passive gap-fill equal-weight |
| titan | passive ranked top-N equal-weight |

Compute alpha attribution **weekly** when running the scorecard. Output goes into `argus_flow/logs/alpha_attribution_<date>.json` as supporting evidence for each scorecard entry.

**Composite**:
- `execution_penalty` — `1 - (slippage_score × latency_score × fill_quality_score)`
- `final_score` — weighted composite (see formula below; floored at 50 if `beats_benchmark=false`)
- `recommended_action` — mapped from final_score to verdict label

## Scoring formula (initial weights)

```
final_score = sample_confidence × (
    0.35 × normalize(expectancy_R) +
    0.20 × normalize(profit_factor) +
    0.10 × (1 - max_drawdown_R / 5) +
    0.15 × (1 - execution_penalty) +
    0.10 × runtime_cleanliness_factor +
    0.05 × regime_fit_score +
    0.05 × (1 - correlation_penalty)
) × 100
```

`normalize` clamps inputs to [0, 1]. `runtime_cleanliness_factor` = `min(runtime_cleanliness_days / 30, 1.0)`.

Weights are TUNABLE — but tuning them changes the scorecard's identity. Don't adjust mid-evaluation. Adjust at quarter boundaries with explicit memo-of-record.

## Score → action mapping

| final_score | recommended_action |
|---|---|
| ≥ 80 | REAL-CANDIDATE (eligible for real-money gate) |
| 65-79 | WINNER-CANDIDATE |
| 50-64 | KEEP-PAPER |
| 35-49 | OBSERVE (more sample needed) or REWORK if structural issue named |
| 20-34 | THROTTLE (reduce risk_pct, watch closely) |
| < 20 | KILL or QUARANTINE |

Score ≥ 80 is **necessary but not sufficient** for REAL-CANDIDATE. Real-money requires the full readiness gate (per `project_real_money_readiness_gate_20260531.md`), not just a high score.

## When to run

- **Weekly** during exploration phase (now → 5/31). Fridays after market close. Output saved with date-stamped filename.
- **Daily** during real-money phase (post-5/31). Fast feedback loop matters when capital is live.

## Anti-patterns

- **"Tune weights to match what we already think"** — defeats the purpose. The scorecard is supposed to surprise you.
- **"Score one strategy higher than its evidence stage"** — final_score is gated by sample_confidence; thin samples produce low scores even with great expectancy. Don't override.
- **"Use score for promotion without ladder check"** — score answers "how good?", ladder answers "how proven?". Both required.

## How to apply this memory

**Why:** the scorecard is the bridge from "data" to "verdict." Without it, 5/1 + 5/31 reviews stay subjective.

**How to apply:**
- 5/1 review uses scorecard output to anchor each strategy's verdict.
- 5/31 freeze: only strategies with score ≥ 65 sustained for 21+ days are eligible for the WINNER-CANDIDATE / REAL-CANDIDATE tiers.
- Real-money allocation decisions reference the scorecard entry that justified them in the capital_promotion_ledger.
