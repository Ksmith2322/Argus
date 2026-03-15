#!/usr/bin/env python3
"""ops/ml_train_governor.py -- Train ML Governor model for entry quality scoring.

Trains a Gradient Boosting classifier on backtest trade data to predict
win probability at entry time. Uses ONLY features available before trade entry
(no MFE/MAE/exit/duration leakage).

Validates with both:
  1. Stratified K-Fold cross-validation (standard)
  2. Time-series walk-forward validation (realistic, no future peeking)

Outputs:
  - Trained model: data/ml_governor.pkl
  - Feature importance report
  - Governor PnL impact simulation

Usage:
    python ops/ml_train_governor.py                    # full pipeline
    python ops/ml_train_governor.py --no-save          # analysis only
    python ops/ml_train_governor.py --input data/ml_trades.csv  # custom input
"""
import csv
import json
import os
import pickle
import sys
from pathlib import Path

import numpy as np
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.model_selection import cross_val_score, cross_val_predict, StratifiedKFold
from sklearn.preprocessing import LabelEncoder

REPO = Path(__file__).resolve().parent.parent

# ── Feature definitions ──────────────────────────────────────────────────
# STRICT entry-only features: available BEFORE trade is placed.
# Excludes: entry_px (absolute price leaks time period), realized_pnl/equity/cash
# (portfolio state leaks run identity), MFE/MAE/duration (post-trade).
NUMERIC_FEATURES = [
    "sig_score", "sig_score_5m", "sig_score_1h",
    "sig_dist_ma200_pct",
    "sig_confluence_score",
    "sig_ac_base_score", "sig_ac_adjusted_score", "sig_ac_delta",
    "sig_candle_volume_1m", "sig_vol",
    "sig_liq_spread_bps", "sig_liq_vol_1m", "sig_liq_vol_baseline",
    "sig_liq_atr_norm", "sig_liq_penalty_points",
    "sig_trend_strength",
    "sig_session_bonus_points", "sig_session_risk_mult",
    "sig_dist_support", "sig_dist_resistance",
    "sig_cooldown_remaining_s",
    "entry_hour_utc", "entry_dow",
]

CATEGORICAL_FEATURES = ["sig_regime", "sig_session", "sig_confluence_gate"]


def _safe_float(v):
    if v is None or v == "" or v == "None" or v == "N/A":
        return np.nan
    try:
        return float(v)
    except (ValueError, TypeError):
        return np.nan


def load_dataset(path):
    """Load trades CSV and build feature matrix + labels."""
    trades = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            trades.append(row)

    # Sort by entry epoch for time-series validation
    trades.sort(key=lambda t: int(_safe_float(t.get("trade_entry_epoch", 0)) or 0))

    X_rows = []
    y = []
    epochs = []
    pnls = []

    for t in trades:
        row = [_safe_float(t.get(f, "")) for f in NUMERIC_FEATURES]
        for f in CATEGORICAL_FEATURES:
            row.append(t.get(f, "") or "unknown")
        X_rows.append(row)
        y.append(int(t.get("win", 0)))
        epochs.append(int(_safe_float(t.get("trade_entry_epoch", 0)) or 0))
        pnls.append(_safe_float(t.get("trade_pnl_usd", 0)) or 0)

    n_num = len(NUMERIC_FEATURES)
    X_numeric = np.array([[r[j] for j in range(n_num)] for r in X_rows], dtype=float)

    encoders = {}
    X_cat = np.zeros((len(X_rows), len(CATEGORICAL_FEATURES)), dtype=float)
    for j, f in enumerate(CATEGORICAL_FEATURES):
        le = LabelEncoder()
        vals = [row[n_num + j] for row in X_rows]
        X_cat[:, j] = le.fit_transform(vals)
        encoders[f] = le

    X = np.hstack([X_numeric, X_cat])
    y = np.array(y)
    epochs = np.array(epochs)
    pnls = np.array(pnls)

    # Impute NaN with column median
    imputer = SimpleImputer(strategy="median")
    X = imputer.fit_transform(X)

    feature_names = NUMERIC_FEATURES + CATEGORICAL_FEATURES

    return X, y, epochs, pnls, feature_names, encoders, imputer


def kfold_validation(X, y):
    """Standard stratified K-Fold cross-validation."""
    gb = GradientBoostingClassifier(
        n_estimators=200, max_depth=4, learning_rate=0.05,
        min_samples_leaf=10, subsample=0.8, random_state=42
    )
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)

    acc = cross_val_score(gb, X, y, cv=cv, scoring="accuracy")
    f1 = cross_val_score(gb, X, y, cv=cv, scoring="f1")
    roc = cross_val_score(gb, X, y, cv=cv, scoring="roc_auc")

    print("=== Stratified 5-Fold CV ===")
    print(f"  Accuracy: {acc.mean():.3f} +/- {acc.std():.3f}")
    print(f"  F1 (win): {f1.mean():.3f} +/- {f1.std():.3f}")
    print(f"  ROC-AUC:  {roc.mean():.3f} +/- {roc.std():.3f}")

    # Governor precision simulation
    y_proba = cross_val_predict(gb, X, y, cv=cv, method="predict_proba")[:, 1]
    return y_proba


def timeseries_validation(X, y, pnls):
    """Walk-forward time-series validation (no future peeking)."""
    n = len(y)
    n_splits = 5
    split_size = n // n_splits

    print(f"\n=== Time-Series Walk-Forward ({n_splits} splits, ~{split_size} trades each) ===")
    all_proba = np.full(n, np.nan)

    for fold in range(1, n_splits):
        train_end = fold * split_size
        test_end = min((fold + 1) * split_size, n)

        X_train, y_train = X[:train_end], y[:train_end]
        X_test, y_test = X[train_end:test_end], y[train_end:test_end]

        gb = GradientBoostingClassifier(
            n_estimators=200, max_depth=4, learning_rate=0.05,
            min_samples_leaf=10, subsample=0.8, random_state=42
        )
        gb.fit(X_train, y_train)

        preds = gb.predict(X_test)
        proba = gb.predict_proba(X_test)[:, 1]
        all_proba[train_end:test_end] = proba

        acc = (preds == y_test).mean()
        n_pred_win = (preds == 1).sum()
        n_actual_win = y_test.sum()
        precision = y_test[preds == 1].mean() if n_pred_win > 0 else 0

        print(f"  Fold {fold}: train={train_end}, test={test_end - train_end} | "
              f"acc={acc:.3f} | pred_wins={n_pred_win} actual_wins={n_actual_win} | "
              f"precision={precision:.3f}")

    # Overall
    valid = ~np.isnan(all_proba)
    y_v = y[valid]
    p_v = all_proba[valid]
    pnl_v = pnls[valid]

    print(f"\n  Overall: {int(valid.sum())} trades evaluated")
    print(f"  Baseline WR: {y_v.mean():.1%}, PnL: ${pnl_v.sum():.2f}")

    return all_proba


def governor_simulation(y, pnls, proba, label=""):
    """Simulate PnL impact of blocking low-confidence trades."""
    valid = ~np.isnan(proba)
    y_v = y[valid]
    p_v = proba[valid]
    pnl_v = pnls[valid]
    order = np.argsort(p_v)[::-1]

    print(f"\n=== Governor PnL Simulation {label} ===")
    print(f"  Baseline: {len(y_v)} trades, WR={y_v.mean():.1%}, PnL=${pnl_v.sum():.2f}")

    results = []
    for block_pct in [10, 20, 30, 40, 50]:
        k = max(1, int(len(y_v) * block_pct / 100))
        bottom_k = set(order[-k:].tolist())
        kept = np.array([i not in bottom_k for i in range(len(y_v))])
        kept_wr = y_v[kept].mean()
        kept_pnl = pnl_v[kept].sum()
        blocked_pnl = pnl_v[~kept].sum()
        print(f"  Block bottom {block_pct:2d}%: {kept.sum()} trades, "
              f"WR={kept_wr:.1%}, PnL=${kept_pnl:+.2f} "
              f"(avoided ${blocked_pnl:+.2f})")
        results.append({
            "block_pct": block_pct,
            "kept_trades": int(kept.sum()),
            "kept_wr": round(float(kept_wr), 4),
            "kept_pnl": round(float(kept_pnl), 2),
            "blocked_pnl": round(float(blocked_pnl), 2),
        })
    return results


def train_final_model(X, y, feature_names):
    """Train final model on all data and report feature importances."""
    gb = GradientBoostingClassifier(
        n_estimators=200, max_depth=4, learning_rate=0.05,
        min_samples_leaf=10, subsample=0.8, random_state=42
    )
    gb.fit(X, y)

    imp = gb.feature_importances_
    idx = np.argsort(imp)[::-1]

    print("\n=== Feature Importances (Final Model) ===")
    for rank, i in enumerate(idx[:15]):
        bar = "#" * int(imp[i] * 100)
        print(f"  {rank + 1:2d}. {feature_names[i]:30s} {imp[i]:.4f}  {bar}")

    return gb


def main():
    args = sys.argv[1:]
    no_save = "--no-save" in args
    input_path = str(REPO / "data" / "ml_trades.csv")
    if "--input" in args:
        idx = args.index("--input")
        input_path = args[idx + 1]

    print(f"Loading dataset: {input_path}")
    X, y, epochs, pnls, feature_names, encoders, imputer = load_dataset(input_path)

    print(f"Features: {X.shape[1]} (strict entry-only, no leakage)")
    print(f"Trades: {len(y)} ({sum(y == 1)} wins / {sum(y == 0)} losses = {y.mean():.1%} WR)")
    print()

    # 1. K-Fold CV
    kfold_proba = kfold_validation(X, y)
    governor_simulation(y, pnls, kfold_proba, "(K-Fold)")

    # 2. Time-series validation
    ts_proba = timeseries_validation(X, y, pnls)
    governor_simulation(y, pnls, ts_proba, "(Time-Series)")

    # 3. Train final model
    model = train_final_model(X, y, feature_names)

    # 4. Save artifacts
    if not no_save:
        output_dir = REPO / "data"
        os.makedirs(output_dir, exist_ok=True)

        model_path = output_dir / "ml_governor.pkl"
        artifact = {
            "model": model,
            "imputer": imputer,
            "encoders": encoders,
            "feature_names": feature_names,
            "numeric_features": NUMERIC_FEATURES,
            "categorical_features": CATEGORICAL_FEATURES,
            "training_stats": {
                "n_trades": len(y),
                "n_wins": int(sum(y == 1)),
                "win_rate": round(float(y.mean()), 4),
                "n_features": X.shape[1],
            },
        }
        with open(model_path, "wb") as f:
            pickle.dump(artifact, f)
        print(f"\nModel saved: {model_path}")

        # Save feature importance as JSON
        imp_path = output_dir / "ml_governor_features.json"
        imp_vals = model.feature_importances_
        idx = np.argsort(imp_vals)[::-1]
        feat_imp = [
            {"rank": r + 1, "feature": feature_names[i], "importance": round(float(imp_vals[i]), 6)}
            for r, i in enumerate(idx)
        ]
        with open(imp_path, "w") as f:
            json.dump(feat_imp, f, indent=2)
        print(f"Feature importances: {imp_path}")

    print("\nDone.")


if __name__ == "__main__":
    main()