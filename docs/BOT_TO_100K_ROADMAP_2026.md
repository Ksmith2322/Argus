# Bot To 100K Roadmap

Created: 2026-05-12  
Purpose: make the bot earn capital through evidence, not optimism.

## Thesis

The bot becomes the better route only when it proves live, repeatable edge above SPY across multiple uncorrelated clusters while the operations layer stays boring. Until then, the system is an alpha lab with strict capital controls.

## Non-Negotiable Rules

- Missing evidence blocks promotion.
- Paper-only evidence cannot approve real capital by itself.
- No strategy can scale because the framework is good; only strategy-level live edge counts.
- Capital moves by ladder stage only: 0, 5K, 10K, 25K, 50K, 100K.
- Any phantom fill, silent failure, margin alert, or TWS cascade resets the relevant clean-ops clock.
- Full 100K allocation requires 3+ live strategies, 2+ clusters, 90+ days, 75+ fills per strategy, fleet IR >= 1.0, and bounded drawdown.

## Timeline

### 2026-05-12 to 2026-05-31: Existential Gate

Goal: identify whether there is anything worth funding.

Required by 2026-05-31:

- At least 3 strategies with post-reset evidence.
- Each candidate has 50+ real-fill or paper-fill trades from clean instrumentation.
- Each candidate beats SPY on active-window comparison with IR >= 1.0 before live scaling.
- No new strategy sprawl unless it belongs to a missing cluster.
- All failures get kill, shelve, or repair disposition.

If zero strategies clear this, the bot stays at RESEARCH_FREEZE.

### 2026-06-01 to 2026-08-31: 90-Day Clean Evidence Window

Goal: graduate 1 to 2 strategies into small live capital.

Targets:

- LIVE_SMOKE_5K after 1 strategy has 10 live fills, 14 clean days, IR >= 0.3.
- LIVE_10K after 1 strategy has 50 live fills, 30 clean days, IR >= 0.7, paper/live IR delta <= 0.3.
- Kill immediately if live IR diverges from paper by more than 0.3 after minimum sample.

### 2026-09-01 to 2026-10-31: Diversification Gate

Goal: add an uncorrelated second cluster.

Targets:

- At least 2 qualified strategies.
- At least 2 clusters.
- Fleet IR >= 0.8.
- Worst single day <= 3%.
- Max pairwise correlation <= 0.65.
- LIVE_25K only after the second cluster is proven, not merely launched.

### 2026-11-01 to 2026-12-31: Year-End Capital Decision

Goal: decide whether the bot deserves half the account.

Targets:

- 3 to 5 qualified strategies.
- 2+ clusters.
- 60+ live days per strategy.
- 50+ fills per strategy.
- Strategy IR >= 1.0.
- Fleet IR >= 1.0.
- 45 clean ops days.
- Capacity test survives 50K.

Expected year-end posture if successful: 50K bot, 50K DIY/index.

## Full 100K Gate

The bot gets the full account only after LIVE_100K passes:

- 3+ qualified strategies.
- 2+ clusters.
- 75+ fills per strategy.
- 90+ live days per strategy.
- Strategy IR >= 1.2.
- Fleet IR >= 1.0.
- Paper/live IR delta <= 0.2.
- Max strategy drawdown <= 8%.
- Fleet max drawdown <= 10%.
- Worst single day <= 2.5%.
- 60 clean ops days.
- 45 broker-reconciled days.
- Capacity test at 100K.

## Implementation Map

- Policy config: `argus_flow/configs/capital_ladder.json`
- Manual evidence template: `argus_flow/configs/capital_ladder_evidence.example.json`
- Evaluator: `helio/capital_ladder.py`
- Ops evidence generator: `helio/ops_reliability.py`
- Phantom resolution ledger: `argus_flow/configs/phantom_trade_resolutions.json`
- Report output: `argus_flow/logs/capital_ladder_report.json`
- Ops report output: `argus_flow/logs/ops_reliability_report.json`
- Real-money hook: `helio/real_money.py`
- Kill-switch drill evidence writer: `ops/drill_harness.ps1 -Drill Kill`
- Commands:
  - `python -m helio.ops_reliability`
  - `python -m helio.capital_ladder`
  - `powershell -ExecutionPolicy Bypass -File ops/run_capital_ladder_gate.ps1`

The answer to "bot or traditional?" becomes mechanical: run the ladder. If approved capital is below 100K, the bot has not yet earned the full route.
