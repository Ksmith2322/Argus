#!/usr/bin/env python3
"""argus_flow/ops/retrain_governor_fx.py -- Train FX Governor model on IBKR trade data.

Trains a Gradient Boosting classifier on FX trade data from the IBKR unified
runner to predict win probability at entry time.  Uses ONLY features available
before trade entry (signal-level features).

Data sources:
  - Trades:  argus_flow/logs/{symbol}/trades.csv
  - Signals: argus_flow/logs/{symbol}/signals.csv

Features (entry-only, no leakage):
  range_pct, vol_z, range_accel, dist_from_low,
  hour_sin, hour_cos (cyclical encoding of hour),
  direction (categorical: long/short),
  symbol   (categorical: EURUSD/GBPUSD/EURJPY/...)

Label: win = 1 if pnl_pips > 0, else 0

Validates with both:
  1. Stratified 5-Fold cross-validation
  2. Time-series walk-forward validation (no future peeking)

Outputs:
  - Trained model: argus_flow/data/fx_governor.pkl
  - Feature importance report

Usage:
    python -m argus_flow.ops.retrain_governor_fx
    python -m argus_flow.ops.retrain_governor_fx --dry-run
    python -m argus_flow.ops.retrain_governor_fx --min-trades 50
"""
import argparse
import csv
import json
import math
import os
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.model_selection import cross_val_score, cross_val_predict, StratifiedKFold
from sklearn.preprocessing import LabelEncoder

# ── Paths ────────────────────────────────────────────────────────────────
REPO = Path(__file__).resolve().parent.parent          # argus_flow/
LOGS_DIR = REPO / "logs"
DATA_DIR = REPO / "data"

# All FX pairs to scan for trade data
FX_PAIRS = [
    "eurusd", "gbpusd", "eurjpy",
    "usdjpy", "audusd", "gbpjpy",
    "cadjpy", "audjpy",
]

# ── Feature definitions ─────────────────────────────────────────────────
# Strict entry-only features from signals.csv (no post-trade leakage).
SIGNAL_NUMERIC_FEATURES = [
    "range_pct",
    "vol_z",
    "range_accel",
    "dist_from_low",
]

# Derived numeric (computed, not raw from CSV)
DERIVED_NUMERIC_FEATURES = [
    "hour_sin",
    "hour_cos",
]

CATEGORICAL_FEATURES = [
    "direction",
    "symbol",
]

ALL_NUMERIC = SIGNAL_NUMERIC_FEATURES + DERIVED_NUMERIC_FEATURES
ALL_FEATURES = ALL_NUMERIC + CATEGORICAL_FEATURES


# ── Helpers ──────────────────────────────────────────────────────────────

def _safe_float(v):
    if v is None or v == "" or v == "None" or v == "N/A":
        return np.nan
    try:
        return float(v)
    except (ValueError, TypeError):
        return np.nan


def _parse_ts(ts_str):
    """Parse ISO timestamp string to epoch seconds."""
    try:
        dt = datetime.fromisoformat(ts_str)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _load_csv(path):
    """Load CSV file and return list of dicts."""
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ── Data loading ─────────────────────────────────────────────────────────

def load_signals_for_symbol(symbol):
    """Load signals.csv for a symbol, keyed by session_id for fast lookup."""
    path = LOGS_DIR / symbol / "signals.csv"
    rows = _load_csv(path)

    # Group by session_id, sorted by timestamp
    by_session = {}
    for r in rows:
        sid = r.get("session_id", "")
        if not sid:
            continue
        epoch = _parse_ts(r.get("ts", ""))
        if epoch is None:
            continue
        r["_epoch"] = epoch
        by_session.setdefault(sid, []).append(r)

    # Sort each session's signals by time
    for sid in by_session:
        by_session[sid].sort(key=lambda s: s["_epoch"])

    return by_session


def find_entry_signal(signals_by_session, session_id, trade_epoch):
    """Find the matching entry signal: same session, closest timestamp <= trade entry.

    We look for the signal with an ENTRY action (direction is set) that is
    closest in time before the trade timestamp.  Falls back to the closest
    signal overall if no exact entry action is found.
    """
    signals = signals_by_session.get(session_id, [])
    if not signals:
        return None

    best = None
    best_delta = float("inf")

    for s in signals:
        sig_epoch = s["_epoch"]
        delta = trade_epoch - sig_epoch
        # Signal must be at or before trade entry
        if delta < 0:
            continue
        # Prefer signals with a direction (actual entry triggers)
        has_direction = bool(s.get("direction", "").strip())
        action = s.get("action", "")
        is_entry = action in ("ENTRY", "BUY", "SELL") or has_direction

        if delta < best_delta:
            best = s
            best_delta = delta
        elif delta == best_delta and is_entry and best is not None:
            # Tie-break: prefer entry signal
            best = s

    # Sanity: reject if signal is more than 10 minutes before trade
    if best is not None and best_delta > 600:
        return None

    return best


def collect_trades():
    """Scan all FX pair log dirs and collect valid trades with matched signals."""
    records = []
    stats = {"pairs_found": 0, "total_trades": 0, "valid_trades": 0,
             "matched": 0, "unmatched": 0}

    for symbol in FX_PAIRS:
        trades_path = LOGS_DIR / symbol / "trades.csv"
        if not trades_path.exists():
            continue

        trades = _load_csv(trades_path)
        if not trades:
            continue

        stats["pairs_found"] += 1
        stats["total_trades"] += len(trades)

        # Load signals for this symbol
        signals_by_session = load_signals_for_symbol(symbol)

        for t in trades:
            # Filter: only experiment_valid=true
            valid_str = t.get("experiment_valid", "").strip().lower()
            if valid_str != "true":
                continue
            stats["valid_trades"] += 1

            trade_epoch = _parse_ts(t.get("ts", ""))
            if trade_epoch is None:
                stats["unmatched"] += 1
                continue

            session_id = t.get("session_id", "")
            signal = find_entry_signal(signals_by_session, session_id, trade_epoch)

            if signal is None:
                stats["unmatched"] += 1
                continue

            stats["matched"] += 1

            # Extract features from signal
            hour = _safe_float(signal.get("hour", ""))
            hour_sin = math.sin(2 * math.pi * hour / 24) if not np.isnan(hour) else np.nan
            hour_cos = math.cos(2 * math.pi * hour / 24) if not np.isnan(hour) else np.nan

            record = {
                # Signal numeric features
                "range_pct": _safe_float(signal.get("range_pct", "")),
                "vol_z": _safe_float(signal.get("vol_z", "")),
                "range_accel": _safe_float(signal.get("range_accel", "")),
                "dist_from_low": _safe_float(signal.get("dist_from_low", "")),
                # Derived
                "hour_sin": hour_sin,
                "hour_cos": hour_cos,
                # Categorical
                "direction": t.get("direction", "").strip().lower() or "unknown",
                "symbol": symbol.upper(),
                # Label
                "pnl_pips": _safe_float(t.get("pnl_pips", "")),
                "win": 1 if _safe_float(t.get("pnl_pips", "")) > 0 else 0,
                # Metadata (not used as features)
                "_epoch": trade_epoch,
                "_duration_min": _safe_float(t.get("duration_min", "")),
                "_exit_reason": t.get("exit_reason", ""),
            }
            records.append(record)

    return records, stats


def build_dataset(records):
    """Build feature matrix X, labels y, and metadata from collected records."""
    # Sort by trade epoch for time-series validation
    records.sort(key=lambda r: r["_epoch"])

    X_rows = []
    y = []
    epochs = []
    pnls = []

    for rec in records:
        row_numeric = [rec[f] for f in ALL_NUMERIC]
        row_cat = [rec[f] for f in CATEGORICAL_FEATURES]
        X_rows.append(row_numeric + row_cat)
        y.append(rec["win"])
        epochs.append(rec["_epoch"])
        pnls.append(rec["pnl_pips"])

    n_num = len(ALL_NUMERIC)
    X_numeric = np.array([[r[j] for j in range(n_num)] for r in X_rows], dtype=float)

    # Encode categoricals
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

    return X, y, epochs, pnls, encoders, imputer


# ── Validation ───────────────────────────────────────────────────────────

def kfold_validation(X, y):
    """Stratified 5-Fold cross-validation."""
    gb = GradientBoostingClassifier(
        n_estimators=200, max_depth=4, learning_rate=0.05,
        min_samples_leaf=10, subsample=0.8, random_state=42,
    )
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)

    acc = cross_val_score(gb, X, y, cv=cv, scoring="accuracy")
    f1 = cross_val_score(gb, X, y, cv=cv, scoring="f1")
    roc = cross_val_score(gb, X, y, cv=cv, scoring="roc_auc")

    print("=== Stratified 5-Fold CV ===")
    print(f"  Accuracy: {acc.mean():.3f} +/- {acc.std():.3f}")
    print(f"  F1 (win): {f1.mean():.3f} +/- {f1.std():.3f}")
    print(f"  ROC-AUC:  {roc.mean():.3f} +/- {roc.std():.3f}")

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

        if len(np.unique(y_train)) < 2:
            print(f"  Fold {fold}: skipped (single class in training set)")
            continue

        gb = GradientBoostingClassifier(
            n_estimators=200, max_depth=4, learning_rate=0.05,
            min_samples_leaf=10, subsample=0.8, random_state=42,
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
    if valid.sum() > 0:
        y_v = y[valid]
        p_v = all_proba[valid]
        pnl_v = pnls[valid]
        print(f"\n  Overall: {int(valid.sum())} trades evaluated")
        print(f"  Baseline WR: {y_v.mean():.1%}, PnL: {pnl_v.sum():.1f} pips")
    else:
        print("\n  No trades evaluated in walk-forward.")

    return all_proba


def governor_simulation(y, pnls, proba, label=""):
    """Simulate PnL impact of blocking low-confidence trades."""
    valid = ~np.isnan(proba)
    if valid.sum() == 0:
        print(f"\n=== Governor PnL Simulation {label} === (no data)")
        return []

    y_v = y[valid]
    p_v = proba[valid]
    pnl_v = pnls[valid]
    order = np.argsort(p_v)[::-1]

    print(f"\n=== Governor PnL Simulation {label} ===")
    print(f"  Baseline: {len(y_v)} trades, WR={y_v.mean():.1%}, PnL={pnl_v.sum():.1f} pips")

    results = []
    for block_pct in [10, 20, 30, 40, 50]:
        k = max(1, int(len(y_v) * block_pct / 100))
        bottom_k = set(order[-k:].tolist())
        kept = np.array([i not in bottom_k for i in range(len(y_v))])
        kept_wr = y_v[kept].mean() if kept.sum() > 0 else 0
        kept_pnl = pnl_v[kept].sum()
        blocked_pnl = pnl_v[~kept].sum()
        print(f"  Block bottom {block_pct:2d}%: {kept.sum()} trades, "
              f"WR={kept_wr:.1%}, PnL={kept_pnl:+.1f} pips "
              f"(avoided {blocked_pnl:+.1f} pips)")
        results.append({
            "block_pct": block_pct,
            "kept_trades": int(kept.sum()),
            "kept_wr": round(float(kept_wr), 4),
            "kept_pnl": round(float(kept_pnl), 2),
            "blocked_pnl": round(float(blocked_pnl), 2),
        })
    return results


def train_final_model(X, y):
    """Train final model on all data and report feature importances."""
    gb = GradientBoostingClassifier(
        n_estimators=200, max_depth=4, learning_rate=0.05,
        min_samples_leaf=10, subsample=0.8, random_state=42,
    )
    gb.fit(X, y)

    imp = gb.feature_importances_
    idx = np.argsort(imp)[::-1]

    print("\n=== Feature Importances (Final Model) ===")
    for rank, i in enumerate(idx):
        bar = "#" * int(imp[i] * 100)
        print(f"  {rank + 1:2d}. {ALL_FEATURES[i]:20s} {imp[i]:.4f}  {bar}")

    return gb


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Train FX Governor model on IBKR trade data",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Analyze without saving model")
    parser.add_argument("--min-trades", type=int, default=100,
                        help="Minimum valid trades required to train (default: 100)")
    args = parser.parse_args()

    print("=" * 60)
    print("FX Governor Training Pipeline")
    print("=" * 60)

    # 1. Collect trades
    print(f"\nScanning {LOGS_DIR} for FX trade data...")
    records, stats = collect_trades()

    print(f"  Pairs with data: {stats['pairs_found']}")
    print(f"  Total trades:    {stats['total_trades']}")
    print(f"  Valid trades:    {stats['valid_trades']} (experiment_valid=true)")
    print(f"  Signal matched:  {stats['matched']}")
    print(f"  Unmatched:       {stats['unmatched']}")

    if stats["matched"] < args.min_trades:
        print(f"\nERROR: Only {stats['matched']} matched trades found, "
              f"need at least {args.min_trades}.")
        print("Collect more trade data or use --min-trades to lower the threshold.")
        sys.exit(1)

    # 2. Build dataset
    X, y, epochs, pnls, encoders, imputer = build_dataset(records)

    print(f"\nDataset built:")
    print(f"  Features: {X.shape[1]} ({len(ALL_NUMERIC)} numeric + "
          f"{len(CATEGORICAL_FEATURES)} categorical)")
    print(f"  Trades:   {len(y)} ({sum(y == 1)} wins / {sum(y == 0)} losses "
          f"= {y.mean():.1%} WR)")

    # Symbol breakdown
    symbols = [r["symbol"] for r in sorted(records, key=lambda r: r["_epoch"])]
    unique_symbols = sorted(set(symbols))
    for sym in unique_symbols:
        sym_idx = [i for i, s in enumerate(symbols) if s == sym]
        sym_wr = y[sym_idx].mean()
        sym_pnl = pnls[sym_idx].sum()
        print(f"    {sym}: {len(sym_idx)} trades, WR={sym_wr:.1%}, PnL={sym_pnl:+.1f} pips")

    # Exit reason breakdown
    exit_reasons = {}
    for r in records:
        reason = r.get("_exit_reason", "unknown")
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
    print(f"  Exit reasons: {dict(sorted(exit_reasons.items(), key=lambda x: -x[1]))}")

    # 3. K-Fold CV
    print()
    kfold_proba = kfold_validation(X, y)
    governor_simulation(y, pnls, kfold_proba, "(K-Fold)")

    # 4. Time-series walk-forward
    ts_proba = timeseries_validation(X, y, pnls)
    governor_simulation(y, pnls, ts_proba, "(Time-Series)")

    # 5. Train final model
    model = train_final_model(X, y)

    # 6. Save artifacts
    if args.dry_run:
        print("\n[DRY RUN] Skipping model save.")
    else:
        os.makedirs(DATA_DIR, exist_ok=True)

        model_path = DATA_DIR / "fx_governor.pkl"
        artifact = {
            "model": model,
            "imputer": imputer,
            "encoders": encoders,
            "feature_names": ALL_FEATURES,
            "numeric_features": ALL_NUMERIC,
            "categorical_features": CATEGORICAL_FEATURES,
            "training_stats": {
                "n_trades": len(y),
                "n_wins": int(sum(y == 1)),
                "n_losses": int(sum(y == 0)),
                "win_rate": round(float(y.mean()), 4),
                "n_features": X.shape[1],
                "pairs": unique_symbols,
                "total_pnl_pips": round(float(pnls.sum()), 2),
                "trained_at": datetime.now(timezone.utc).isoformat(),
            },
        }
        with open(model_path, "wb") as f:
            pickle.dump(artifact, f)
        print(f"\nModel saved: {model_path}")

        # Save feature importance as JSON
        imp_path = DATA_DIR / "fx_governor_features.json"
        imp_vals = model.feature_importances_
        idx = np.argsort(imp_vals)[::-1]
        feat_imp = [
            {"rank": r + 1, "feature": ALL_FEATURES[i],
             "importance": round(float(imp_vals[i]), 6)}
            for r, i in enumerate(idx)
        ]
        with open(imp_path, "w") as f:
            json.dump(feat_imp, f, indent=2)
        print(f"Feature importances: {imp_path}")

    print("\nDone.")


if __name__ == "__main__":
    main()
