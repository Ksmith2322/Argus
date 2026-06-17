---
name: Label-First / Perfect-Foresight Research Project
description: Reverse-engineer perfect historical trades to find predictive features. Multi-instrument × multi-timeframe batch of 16 cells, ~150K labeled bars total. Comprehensive plan with code stubs, label/feature catalogs, and validation methodology.
type: project
originSessionId: 3a7a2a39-5348-43c1-8feb-cb0d2b0662ae
---
# Label-First Research Project

**User's core insight (2026-04-16):** "Pin point the times to buy and sell, then figure out what is needed to capture that movement... 5min would've made $X buying here, here, here — now how to identify the buying indicator for that move, where's the confirmation, when to sell."

**Methodology:** Inverse of conventional strategy dev. Don't code a hypothesis and hope it works. Find optimal historical trades, catalog conditions at those exact moments, identify features that distinguish "perfect" from "no-trade" bars.

## Batch design: 4 instruments × 4 timeframes = 16 cells

| | ES=F | NQ=F | GBPUSD | GLD |
|---|---|---|---|---|
| Why | Deepest history, most liquid index future | Tech tilt, parallel infra | FX major, 24h, 17K hourly bars | ETF, uncorrelated, decades of daily |
| **1d** | 8,300 bars (2000+) | 6,500 | 5,200 | 5,000 |
| **1h** | 4,500 (US session) | 4,500 | 17,500 (24h) | 4,500 |
| **15m** | ~5,500 (60d) | 5,500 | 5,500 | 5,500 |
| **5m** | ~13,800 (60d) | 13,800 | 13,800 | 13,800 |

**Total ~150K labeled bars.** Statistical power: real.

Skip 1min (only 7 days yfinance) unless we extend via IBKR.

## Label definitions (test all variants in parallel — DON'T pick one upfront)

### Long-entry labels
For each bar B, label as `LONG_ENTRY` if forward window meets criteria:

| Variant | Forward window | Min gain | Max DD allowed |
|---|---|---|---|
| **L1: Quick** | next 4 bars | ≥0.5 ATR | ≤0.2 ATR |
| **L2: Modest** | next 8 bars | ≥1.0 ATR | ≤0.4 ATR |
| **L3: Big** | next 20 bars | ≥2.0 ATR | ≤0.5 ATR |
| **L4: Asymmetric** | next 12 bars | ≥1.5 ATR | ≤0.3 ATR (very tight) |

(Same for SHORT_ENTRY with inverted direction.)

### Exit labels
For bars within an open trade, label as `EXIT` if:
- Bar closed within 1 ATR of forward maximum (price near to-be peak), OR
- Forward 4 bars show >0.3 ATR retracement against position

This catches "the bar where you should have sold."

### Why multiple labels: each captures a different trade style. Features that predict L1 (scalping) differ from L3 (swing). Don't assume which the market rewards.

## Feature catalog (compute at every bar, no lookahead)

### Price action (immediate context)
- `body_pct` — body / range
- `upper_wick_pct`, `lower_wick_pct`
- `range_atr_ratio` — current range / 14-bar ATR
- `gap_pct` — gap from prior close (intraday only)
- `consecutive_direction` — # of bars in current direction
- `inside_bar`, `outside_bar` — boolean

### Trend context
- `ema_8`, `ema_21`, `ema_50`, `ema_200` — values + slopes
- `ema_stack` — categorical: bull/bear/mixed
- `dist_from_ema_50_atr` — how far price is from 50-EMA in ATR units
- `vwap_dist_atr` — distance from session VWAP (intraday)

### Volume
- `vol_z_20` — volume z-score over 20 bars
- `vol_z_60`
- `obv_slope_5` — on-balance-volume slope last 5 bars
- `volume_climax` — boolean: vol > 3σ AND price reverses
- `volume_dryup` — boolean: vol < 0.3 × 20-bar avg

### Volatility regime
- `atr_pct` — ATR / price (vol normalized)
- `atr_z_60` — ATR z-score over 60 bars (regime detection)
- `bb_width_pct` — Bollinger Band width / mid (vol expansion)
- `choppiness_14` — 0-100 trending (low) vs ranging (high)
- `realized_vol_20` — 20-bar realized vol annualized

### Multi-timeframe (computed from higher TF, projected to current)
- `htf_trend_4h` — categorical bull/bear/range from 4h EMAs
- `htf_dist_to_resistance_atr` — distance to nearest higher-TF swing high
- `htf_dist_to_support_atr` — distance to nearest higher-TF swing low
- `htf_regime_atr_z` — higher-TF volatility regime

### Time features
- `hour_of_day` (intraday)
- `day_of_week`
- `session` — categorical: Asia/London/NY (FX) or pre-market/RTH/post (US)
- `minutes_since_open` (intraday US instruments)
- `days_since_event` — last FOMC, last NFP, last earnings (where available)

### Order flow proxies (limited without true L2)
- `close_to_high_pct` — (close - low) / range (buy pressure proxy)
- `close_to_low_pct` (sell pressure proxy)
- `consecutive_close_above_open` — momentum
- `delta_proxy` — (close - open) × volume

### Microstructure (futures/FX)
- `spread_pct` — entry spread / price (fill-cost proxy)
- `tick_velocity` — ticks per minute (where available)

**Total: ~45 features per bar.** Some won't help. That's fine — we'll prune.

## Pipeline architecture

```
data/labels/<instrument>_<timeframe>_<label_variant>.parquet
  ↓
features/<instrument>_<timeframe>.parquet
  ↓
analysis/<instrument>_<timeframe>_<label_variant>/
  ├── feature_separation.csv  (KS-stat, mutual info per feature)
  ├── pair_correlations.csv   (find feature pairs that combine well)
  ├── candidate_rules.json    (top combinations w/ in-sample win rate)
  └── walkforward_report.json (out-of-sample validation)
```

**Validation:** chronological split — first 70% train, last 30% holdout. **Walk-forward** with 6 folds. **Rule must produce ≥30 trades in holdout** to count.

## Code stubs (4 modules to build)

```python
# 1. labeler.py
def label_perfect_entries(df, atr_col, label_variant) -> pd.Series:
    """Returns boolean series. True = bar B was a perfect entry per variant."""

# 2. features.py
def compute_features(df, htf_df=None) -> pd.DataFrame:
    """Returns ~45-column DataFrame. No lookahead anywhere."""

# 3. analyzer.py
def feature_separation(features, labels) -> pd.DataFrame:
    """Per feature: KS-stat, MI, mean diff. Sorted by separation strength."""

def find_combinations(features, labels, top_n=5) -> list[dict]:
    """Greedy search for 2-3 feature combos that maximize separation."""

# 4. validator.py
def walk_forward_validate(rule, full_df, n_folds=6) -> dict:
    """Returns OOS trade count, WR, PF, MDD per fold + aggregate."""
```

## Honest expectations

Of the 16 cells, expect:
- ~5-8 will produce features with strong in-sample separation
- ~2-4 will hold up out-of-sample at ≥30 trades
- ~1 might be genuinely fundable (PF ≥ 1.30 OOS with 60+ trades)

That's still **better than zero**, which is current state.

## Risks to actively mitigate

1. **Feature leakage** — a feature that secretly uses future data inflates results. Mitigation: every feature computed from `df.iloc[:i+1]` only; unit-test with shuffled future bars.
2. **Multiple-comparisons** — testing 45 features × 4 labels × 16 cells = 2,880 hypotheses. Some will look great by chance. Mitigation: Bonferroni-adjust p-values OR use mutual info (less p-value dependent).
3. **Survivorship/regime bias** — if the 60d 5min data is all one regime, features may not generalize. Mitigation: 1h and 1d tests provide multi-regime check.
4. **Spread realism** — labels assume mid-price fills. Real strategy must beat spread. Mitigation: only mark profitable trades that beat 2× typical spread.

## Phase plan (when activated)

| Day | Work | Deliverable |
|---|---|---|
| 1 | Build labeler.py + features.py | Labeled+featurized parquet for all 16 cells |
| 2 | Build analyzer.py, run separation analysis | Top features per cell ranked |
| 3 | Find combinations, generate candidate rules | candidate_rules.json per cell |
| 4 | Walk-forward validation on all candidates | OOS results, kill table |
| 5 | Convert OOS-passing rules to executable strategy stubs | Backtest-ready strategies |

**Estimated total: 1 work-week** of focused dev. Could compress to 3 days if we accept rougher feature engineering.

## Activation triggers

Don't start until ONE of:
- Current monitoring period yields decisive Argus/mamba data (4-6 weeks)
- User says "let's start"
- A current strategy posts PF ≥ 1.30 over 30 trades AND we want a second, uncorrelated edge

## File paths when active

- Code: `C:\Argus\repo\research\label_first\` (new dir)
- Data: `C:\Argus\repo\research\label_first\data\`
- Reports: `C:\Argus\repo\research\label_first\reports\`
- Conda env: `C:\Argus\.venv\Scripts\python.exe` (existing)

## Decision log

- **2026-04-16:** Project shape locked. 16-cell batch, ~150K bars total, multi-label, 45-feature catalog. Deferred until activation.
- **2026-04-16 (later):** ACTIVATED. Phase 1 batch executed. Findings in `research/label_first/reports/FINDINGS_v1.md`.

## Phase 1 results (2026-04-16)
- **Strongest finding:** GBPUSD daily upper-wick reversal pattern. KS=0.39, p<1e-130. 2.0-2.8x lift on conditional probability. Validated against measurement-bias hypothesis (strict label still KS=0.337).
- **Economic test:** simple OCO backtest gives PF 1.02 over 22yr, 530 trades. Q4 (last 5yr) PF 1.36 — improving. Statistical signal real, economic edge marginal with simple rules; expect material lift from combination filters + trail exits.
- **Other promising leads:** ES/NQ 1h session-of-day (KS 0.20, 9 strong features each), GLD 1h hour-of-day (KS 0.18), daily index short-side bias.
- **Confirmed nulls:** 5min noise across all instruments (no exploitable structure in 60-day window). Volume features underwhelming as primary edge.
- **Files:** `research/label_first/` (code), `research/label_first/reports/` (128 separation CSVs + batch_summary.json + FINDINGS_v1.md)

## Phase 2 candidates (when ready)
1. GBPUSD wick + combination filters (choppiness, HTF support proximity) + trail exits → walk-forward validate
2. ES/NQ session-bias edge: per-hour expected-return table → trade only positive-expectancy hours
3. Daily index short-only system using top-8 features
4. Apollo retroactive outcome tracking to apply same lens to catalyst signals

## Phase 2 results (2026-04-16) — FUNDABLE EDGE FOUND

### Top candidate (recommended productionization)
**`GBPUSD daily wick + bb_width(below) + choppiness(above), OCO target +2.0 ATR / stop -1.0 ATR / hold 20 bars`**
- 298 trades over 22 years
- Overall WR 43%, **PF 1.42**, expectancy +0.238 ATR/trade
- 5 of 5 valid folds positive (chronological 6-fold walk-forward; fold 1 had insufficient sample)
- Interpretable thesis: only take wick reversals when market is in range-bound consolidation

### Other robust candidates
- **#1 `wick + ema21_slope(middle) + ema8_dist(middle), OCO 1.0/0.5/h8`** — PF 1.41, 220 trades, WR 42%
- **#2 `wick + ema21_slope(middle) + bb_width(below), OCO 2.0/1.0/h20`** — PF 1.39, 272 trades

### Statistical validation
- Random-signal stress test (50 trials): 0% hit median PF >= 1.40 by chance
- 24 of 145 tested specs passed weak robust gate vs 20% random baseline → real signal beyond noise

### Files
- `research/label_first/reports/FINDINGS_v2.md` — full report
- `research/label_first/reports/phase2_walkforward.csv` — 140-row sweep
- `research/label_first/{backtest_engine,phase2_combination,phase2_walkforward}.py` — reusable framework

### Productionization next steps
1. Wire as `forge/wick_gbpusd_runner.py` paper-mode, IBKR forex feed
2. Add cohort tagging per Argus gold standard
3. 60-90 day paper trial to validate live signal generation matches backtest
4. If live matches backtest, advance to real-config promotion gate

## Phase 2C results + productionization (2026-04-16)

### TWO new production strategies wired up
**#1: `forge/wick_gbpusd/`** — daily GBPUSD wick reversal
- PF 1.42 (research) / 1.24 live-realistic backtest with rolling quantiles
- ~13 trades/year, daily eval via `run_cohort_report.ps1`
- Files: `runner.py`, `STRATEGY_SPEC.md`, `forge/logs/wick_gbpusd/`

**#2: `forge/gld_pm_long/`** — GLD intraday afternoon long (NEW STANDOUT)
- **PF 2.28 / 1.77 / 1.63 at hours 20/19/18 UTC**, 6/6 walk-forward folds positive each
- t-stats 6.2-7.3 (extraordinarily strong)
- Runner backtest: **PF 1.73 over 530 trades / 2 years**
- 250+ trades/year potential (intraday)
- Files: `runner.py`, `STRATEGY_SPEC.md`, `forge/logs/gld_pm_long/`

### Both wired into ops
- Added to `helio/fleet_monitor.py` as artifact-monitored systems (no auto-restart, scheduled externally)
- Added to `ops/run_cohort_report.ps1` for daily eval
- gld_pm_long needs hourly scheduling at 18/19/20 UTC for intraday — currently runs daily; user should add Task Scheduler entries OR run `--loop` continuously

### Key findings
- **GLD afternoon long** is the strongest finding of the entire research project (PF 2.28 with 6/6 folds positive)
- **NQ overnight long** (PF 1.20-1.29) deferred — modest edge, can productionize later
- Short-side intraday largely null across all instruments (long-only edge dominates)
- Three potential edges (GBPUSD wick + GLD PM + NQ overnight) are largely uncorrelated → real fleet diversification possible

### Reports
- `research/label_first/reports/FINDINGS_v3.md` — Phase 2C details
- `research/label_first/reports/phase2c_*.csv` — per-instrument hour tables

## Phase 2D + 4 productionizations (2026-04-16, later)

### NEW Phase 2D findings — JPY pair NY-afternoon short
- **USDJPY SHORT @ 19 UTC:** PF 1.37, t-stat 3.7, **6/6 folds positive**, 727 trades
- **CADJPY SHORT @ 19 UTC:** PF 1.32, t-stat 3.3, **6/6 folds positive**, 727 trades
- Same hour, two pairs = correlated mechanism (JPY strength in NY afternoon)
- Productionized as `forge/jpy_pm_short/` with one trade per pair per signal hour
- Backtest live-realistic: USDJPY PF 1.24, CADJPY PF 1.30

### Phase 2D combination search on GLD afternoon
- `vol_z_20 < bottom-third` (low-volume afternoons) lifts PF from 1.33 → 1.63 (1.22x)
- `range_atr_ratio` middle-third also helps
- Saved as future optimization candidate (would halve trade volume; current PF 1.73 is fine)

### NQ overnight productionized (Phase 2C deferred)
- Built `forge/nq_overnight/` with --loop mode
- Backtest: 695 trades, WR 47.6%, PF 1.23
- Hours 20, 21, 22, 23, 0 UTC

### gld_pm_long --loop mode added
- Now wakes at top of hours 18, 19, 20 UTC for entries + every other hour for position management
- Without --loop, daily cohort report only fires once per day, missing 2/3 of signals

## Productionization status (as of 2026-04-16)
4 strategies wired up + monitored. All paper-only. All need either Task Scheduler or `--loop` for full edge capture:
- `python -m forge.gld_pm_long.runner --loop` — runs continuously
- `python -m forge.nq_overnight.runner --loop` — runs continuously
- `python -m forge.jpy_pm_short.runner --loop` — runs continuously
- `forge/wick_gbpusd/` runs daily via cohort report (sufficient — daily strategy)

## Phase 3 candidates (next session)
1. Combination filters on JPY PM short (does volume/regime improve PF further?)
2. Apollo retroactive outcome tracking — backfill 356 historical signals with T+1/T+3/T+5 returns
3. Train Argus pairs (USDJPY/CADJPY/GBPUSD) per-conviction-tier analysis once they accumulate 20+ trades
4. Walk-forward on Argus's mtf_trend strategy using Phase 2 framework
5. Higher-order combinations: 3-feature combos on GBPUSD wick (currently 2-feature only)
