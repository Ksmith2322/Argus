---
name: Project Roadmap & Status (updated 2026-03-25)
description: Comprehensive status snapshot — completed phases, active work, remaining steps. Full infra build-out 2026-03-24.
type: project
---

## Current Status (as of 2026-03-24)

**Coinbase/crypto route KILLED** — 60bps round-trip fees destroy any edge. No further crypto spot iteration.

**BTC spot research CLOSED** — Two clean negatives on Kraken BTC spot (Cascade + displacement precursors). No further iteration.

**Argus Cascade CLOSED** — Structural kill. Signal real but payoff non-existent on spot. Infrastructure preserved for future use.

**IBKR FX is the active architecture** — TWS port 7496, account U24860535, Read-Only API. Unified single-process runner (`runner_unified.py`) managing 3 FX pairs.

**Cohort validation ACTIVE** — GBP/USD, EUR/USD, EUR/JPY running. Need 30 valid trades per pair for promotion gate. First valid trade completed (GBP/USD SHORT, -4.65 pips, timeout).

---

## COMPLETED

| Phase | What | Date |
|-------|------|------|
| 1-6 | Core engine, strategy, indicators, candles, confluence | pre-2026-03 |
| 7 | Multi-TF confluence (1m/5m/1h) | 2026-03-09 |
| 8 | Execution layer (PaperAdapter, fills, recovery) | 2026-03-10 |
| 9 | Trade lifecycle journal (17 fields + 10 optional) | 2026-03-10 |
| 10 | Adversarial kill-point acceptance harness | 2026-03-10 |
| 11-16 | Regime, structure, liquidity, session, risk, ops infra | 2026-03-11 |
| ML-1 | ML Governor (train, integrate, GATE mode 0.38) | 2026-03-17 |
| Multi-coin | Parallel runner infra (ETH + BTC, SOL disabled) | 2026-03-17 |
| Config sweep | 34-run sweep: score88, trendlines ON, structure OFF, 60min hold, 5% compound | 2026-03-17 |
| 20 | Order Book Imbalance — REST-based (AT product_book) | 2026-03-18 |
| Cross-coin lag | BTC 60s delta -> ETH score overlay | 2026-03-18 |
| Auto-rotation | ops/coin_rotation.py, hourly metrics update | 2026-03-18 |
| Safety hooks | .claude/hooks block live state edits, warn on code changes | 2026-03-18 |
| Crypto kill | Coinbase route killed (60bps fees, no edge). BTC spot closed. Cascade closed. | 2026-03-22 |
| IBKR FX integration | runner_unified.py, feed_ibkr.py, ibkr_adapter.py, TWS port 7496 | 2026-03-23 |
| schemas.py | FX trade/signal schema definitions | 2026-03-24 |
| Broker reconciliation gate | Built and operational | 2026-03-24 |
| Cohort compliance report | daily_report.py — per-pair stats for promotion decisions | 2026-03-24 |
| Position monitor rewrite | Rewritten for IBKR FX architecture | 2026-03-24 |
| Dashboard cleanup | 3 active FX pairs only, crypto artifacts removed | 2026-03-24 |
| SessionStart hook | Runner status reporting on startup | 2026-03-24 |
| v1.2 FX Infra | Divergence guard fix, dashboard cohort widgets, FX governor retrain, pyramiding, FX tests, .env cleanup | 2026-03-24 |
| v1.3 Ops Automation | Nightly cohort pipeline, weekly digest, autostart update, correlation guard, kill discipline, promotion gate, micro-live config gen | 2026-03-24 |
| v1.4 Hardening | Dashboard SSE+kill+promotion panels, artifact divergence, fleet launcher, watchdog, task registration, FX backtest harness, signal analyzer | 2026-03-24 |
| v1.5 Full Infra | Phase 22D chaos tests (10 suites), Phase 22B risk oversight, Phase 22C dual-model, Class B onboarding, equity analytics, experiment framework, IBKR data downloader, portfolio P&L, alert escalation, DR runbook | 2026-03-24 |

## ACTIVE

- **Cohort validation** — GBP/USD, EUR/USD, EUR/JPY running via `runner_unified.py` (single process, 3 pairs)
- **Collecting valid trades** — Need 30 valid trades per pair for promotion gate. 1 completed so far (GBP/USD SHORT, -4.65 pips, timeout)
- **Do NOT touch trade logic** — Current priority is data collection, not tuning

---

## REMAINING STEPS TO COMPLETE ARGUS

### Critical Path (in order)

1. **Collect 30 valid trades per pair** (CURRENT PRIORITY)
   - Let runner_unified.py run undisturbed
   - Do not modify trade logic, scoring, or entry criteria
   - Monitor via daily_report.py cohort compliance output
   - Pairs: GBP/USD, EUR/USD, EUR/JPY

2. **Cohort gate review**
   - After 30 trades per pair: evaluate WR, PF, expectancy, drawdown
   - Kill underperforming pairs, promote winners
   - Decision: which pairs advance to micro-live

3. **Paper -> micro-live decision for top pairs**
   - Promote pairs that pass cohort gate to real money at minimum size
   - Start with micro lots (1K units) on IBKR

4. **Governor retrain with FX data**
   - Current governor trained on crypto data (ETH 1m candles)
   - Retrain once enough FX trades accumulate (100+ per pair)
   - Must use single-config dataset (mixed-config retrain fails)

5. **Add more FX pairs (Class B candidates)**
   - USD/JPY, AUD/USD, GBP/JPY, CAD/JPY, AUD/JPY
   - Run through same cohort validation pipeline (30 trades each)

6. **Futures re-evaluation with wider stops**
   - Class C (quarantined): MNQ, MES, MYM etc
   - Current stops too tight for futures volatility
   - Revisit with wider stop-loss params after FX pairs are profitable

7. **Phase 21: Real money execution**
   - Graduate from paper to live on validated pairs
   - Scale position sizes based on proven edge
   - Capital scaling plan: $500 -> $100K target

### Nice-to-Have — ALL DONE 2026-03-24

- ~~Pyramiding~~ — config-driven scale-in, defaults OFF
- ~~Dashboard FX widgets~~ — cohort progress, divergence, kill discipline, promotion gate, equity analytics panels
- ~~Governor GATE tuning~~ — retrain pipeline built (`retrain_governor_fx.py`), waiting for 100+ FX trades
- ~~.env cleanup~~ — Kraken keys cleared, crypto sections marked LEGACY
- ~~Test coverage~~ — 29 tests (14 FX system + 5 fault injection + 10 chaos suites)
- ~~Archive crypto code~~ — already organized in archive/

### Infrastructure — ALL DONE 2026-03-24

- Phase 22B risk oversight agent (cross-strategy portfolio monitor)
- Phase 22C dual-model analysis framework (prompt generator)
- Phase 22D chaos testing (10 adversarial data suites, all pass)
- Class B pair onboarding pipeline
- Config experiment framework (Phase C A/B testing)
- IBKR historical data downloader
- Portfolio P&L aggregation
- Alert escalation (consolidated Discord alerts)
- Fleet launcher + watchdog + task registration
- FX backtest harness + signal quality analyzer
- DR runbook for IBKR FX

---

## Architecture Status

- **runner_unified.py** — Single-process runner managing 3 FX pairs (GBP/USD, EUR/USD, EUR/JPY)
- **IBKR connection** — TWS port 7496, account U24860535, Read-Only API
- **schemas.py** — FX trade/signal schema definitions
- **daily_report.py** — Cohort compliance report (per-pair trade counts, metrics)
- **Broker reconciliation gate** — Validates fills against IBKR account state
- **Position monitor** — Rewritten for IBKR FX architecture
- **Dashboard** — Cleaned up, 3 active FX pairs only, cohort progress bars + divergence guard widget
- **SessionStart hook** — Reports runner status on startup
- **Divergence Guard** — Monitors EUR/USD, GBP/USD, EUR/JPY (updated 2026-03-24, was tracking MNQ)
- **FX Governor retrain** — `argus_flow/ops/retrain_governor_fx.py` (ready, waiting for 100+ valid trades)
- **Pyramiding** — Config-driven scale-in in runner_unified.py (defaults OFF, opt-in via `pyramid` config block)
- **FX test suite** — `argus_flow/tests/test_fx_system.py` (14 tests, all passing)

## Pair Classification

| Class | Pairs | Status |
|-------|-------|--------|
| A (active) | GBP/USD, EUR/USD, EUR/JPY | Cohort validation running |
| B (candidates) | USD/JPY, AUD/USD, GBP/JPY, CAD/JPY, AUD/JPY | Pending — add after Class A validated |
| C (quarantined) | MNQ, MES, MYM (futures) | Need wider stops — revisit later |

## Key Config State (2026-03-24)

- Broker: IBKR TWS, port 7496, account U24860535, Read-Only API
- Runner: runner_unified.py (single process, 3 pairs)
- Pairs: GBP/USD, EUR/USD, EUR/JPY
- Cohort gate: 30 valid trades per pair required for promotion
- ML_GOVERNOR_MODE=GATE (trained on crypto data, retrain pending FX data)
