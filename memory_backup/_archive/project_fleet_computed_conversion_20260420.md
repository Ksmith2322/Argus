---
name: Fleet confidence — computed-conversion complete 2026-04-20
description: All 13 strategy rows now show computed confidence except Themis (signal-only, no PnL). Key per-strategy honest reads.
type: project
originSessionId: 468782c5-c83c-4fc9-8b1b-60051a2f4e99
---
On 2026-04-20 the fleet moved from 5 computed + 8 hardcoded to 12 computed + 1 hardcoded.

**Why:** hardcoded confidences are just placeholders. They inflate the fleet headline and hide honest negatives. Every strategy with any backtest source was converted to a real artifact.

**How to apply:** when asked about fleet confidence, trust the per-strategy artifact reads below, NOT the hardcoded numbers from the prior memory snapshot. Themis remains the only intentionally-hardcoded row.

## Per-strategy honest reads (2026-04-20 artifacts)

| Strategy | PF | n | Notable |
|---|---|---|---|
| Argus | live | — | canonical_fills stream, live evidence only |
| Titan | 1.52 | 298 | Edge on PLTR / GDX / MRNA; broad but modest |
| Ares | 2.70 | 12 | ETF rotation, sanity bar only |
| Hermes | 1.11 | 175 | Gap-fill; edge concentrated at score 80+ |
| Apollo | **0.69** | 205 | **Net negative on `post_er` subset** — union negative, edge exists in MU/ORCL/UPS only |
| GDX/GLD | live | — | Canonical-fills computed, sanity sample |
| Mamba | **1.00** | 25 | After daily-cap bug fix, ruin fraction 95% — NOT edge yet |
| Cue Banks | 1.34 | — | S/D zones flipped PF from 0.89 |
| Tori | 2.0+ | — | Angled trend-line trail post-fix |
| VIX Revert | 2.40 | 16 | Sanity bar only (VIX>30 rare); 75% WR; top-3 sensitivity thins to PF 1.24 |
| Sector Rot | 1.55 | 74 mo | **Underperforms SPY buy-and-hold by -70% despite PF>1** — monthly rebalance distortion |
| Index Rebal | 2.08 | — | Per-action split: ADD vs DELETE |
| Themis | hardcoded | — | Signal-only (Congressional trades); no trading PnL to replay |

## Warnings / non-obvious findings

- **Apollo `post_er`**: Writer uses `post_er_*.csv` because runner's `should_enter_post_er` is the live path. `drift_*` is hindsight-biased, `honest_bt_*` uses a pre-ER entry the runner doesn't do, `runup_play` is a separate strategy. Don't union them — risk profiles differ.
- **Sector Rot PF>1 is misleading**: per-month win rate 59% with PF 1.55, but cumulative return underperforms SPY by 70 percentage points over 2020-2026. The sample_warning flags monthly-not-tradelet framing.
- **VIX Revert thin-tail**: pf 2.40 collapses to 1.24 when top 3 trades removed. Real but fragile edge.
- **Mamba ruin 95%**: After external-audit daily-cap fix, n dropped 29→25. Current state is NOT a tradable edge — needs regime split or more sample.

## Where things live

- Writers: `titan/confidence_writer.py`, `apollo/confidence_writer.py`, `hermes/confidence_writer.py`, `ares/confidence_writer.py`, `forge/vix_revert_confidence_writer.py`, `forge/sector_rot_confidence_writer.py`, `forge/index_rebalance_confidence_writer.py`, `forge/tori/confidence_writer.py`, `forge/mamba/confidence_writer.py`, `forge/cuebanks/confidence_writer.py`
- Macro cache: `forge/data/vix_revert/backtest_trades.csv`, `forge/data/sector_rot/backtest_trades.csv` — rerun via `python -m forge.run_macro_backtests --refresh`
- Artifacts: `strategy_confidence/*.json`
- Dashboard row: `/api/strategy_performance` — every row except Themis shows `confidence_source=computed`
- Portfolio correlation endpoint: `/api/portfolio_correlation?source={backfill,live}` — helper at `helio.fleet_state._portfolio_correlation`
