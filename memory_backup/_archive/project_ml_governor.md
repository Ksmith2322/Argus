---
name: ML Governor Phase 1 Complete
description: ML governor model trained, integrated into engine, dashboard upgraded with decision flow panel
type: project
---

ML Governor Phase 1 completed 2026-03-14.

**Why:** Backtest data (1,029 trades) showed clear patterns in win/loss — confluence score, dist_ma200, volume, time-of-day all predictive. Governor acts as entry quality scorer.

**How to apply:**
- Governor is ON in LOG_ONLY mode (`USE_ML_GOVERNOR=true` in .env)
- Three modes: LOG_ONLY (observe), SCORE_MODIFY (adjust confluence), GATE (block entries)
- Model: GradientBoosting, 26 features (strict entry-only, no leakage), trained on 1,029 trades
- K-Fold ROC-AUC: 0.944, Time-series walk-forward shows usable but needs more data for early folds
- PnL impact (K-Fold): blocking bottom 30% turns -$25.54 → +$31.39
- Top features: sig_dist_ma200_pct (34%), volume features (23%), trend_strength (6.5%), time (9.6%)
- Retrain pipeline: `python ops/ml_retrain.py` (extract features → train → save model → Discord notify)

**Files:**
- `ml_governor.py` — engine integration (singleton scorer, snap-to-features bridge)
- `ops/ml_extract_features.py` — feature extraction from backtest artifacts
- `ops/ml_train_governor.py` — model training + validation
- `ops/ml_governor_score.py` — standalone scorer CLI
- `ops/ml_retrain.py` — one-command retrain pipeline
- `data/ml_governor.pkl` — trained model artifact
- `data/ml_trades.csv` — training dataset (1,029 trades x 60 features)

**Dashboard upgrades (same session):**
- ML Governor card with win probability gauge bar
- Decision Flow panel showing entry attempts with governor scores
- `/api/decisions`, `/api/governor` endpoints
- Governor config highlighted in config panel

**Key insight from data:**
- Duration: 0-30m trades win 66.7% vs 15.8% for 30m-90m
- Hour: 15:00 UTC best (+$18.79), 06:00 worst (-$7.11)
- RANGE regime: 4.1% WR (nearly always loses)
- OVERLAP/LONDON sessions: 32-34% WR vs OFF 26%
