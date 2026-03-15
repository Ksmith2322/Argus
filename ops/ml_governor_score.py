#!/usr/bin/env python3
"""ops/ml_governor_score.py -- ML Governor scoring for live/backtest entry decisions.

Loads the trained governor model and scores a feature vector at entry time.
Returns a win probability (0.0-1.0) that can be used as an additional gate
or score modifier in the engine.

Usage (standalone test):
    python ops/ml_governor_score.py                  # score synthetic example
    python ops/ml_governor_score.py --threshold 0.4  # show gate decision

Integration (from engine/runner):
    from ops.ml_governor_score import GovernorScorer
    scorer = GovernorScorer()  # loads model once
    prob = scorer.score(signal_dict)  # returns win probability 0.0-1.0
"""
import os
import pickle
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
MODEL_PATH = REPO / "data" / "ml_governor.pkl"

# Same feature lists as ml_train_governor.py — must stay in sync
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

# Map from signal CSV column names to feature names (strip sig_ prefix)
_SIG_PREFIX = "sig_"


def _safe_float(v):
    if v is None or v == "" or v == "None" or v == "N/A":
        return np.nan
    try:
        return float(v)
    except (ValueError, TypeError):
        return np.nan


class GovernorScorer:
    """Loads trained ML governor model and scores entry signals."""

    def __init__(self, model_path=None):
        path = Path(model_path) if model_path else MODEL_PATH
        if not path.exists():
            raise FileNotFoundError(f"Governor model not found: {path}")

        with open(path, "rb") as f:
            artifact = pickle.load(f)

        self.model = artifact["model"]
        self.imputer = artifact["imputer"]
        self.encoders = artifact["encoders"]
        self.feature_names = artifact["feature_names"]
        self.training_stats = artifact.get("training_stats", {})

    def score(self, signal_dict: dict) -> float:
        """Score a signal dict and return win probability (0.0-1.0).

        Args:
            signal_dict: Dict with signal fields. Keys can be either:
                - Full feature names: "sig_confluence_score", "sig_regime", etc.
                - Raw signal names: "confluence_score", "regime", etc.
                - Also accepts "entry_hour_utc" and "entry_dow" directly.

        Returns:
            Win probability between 0.0 and 1.0.
        """
        def _get(key):
            """Try both sig_-prefixed and raw key names."""
            v = signal_dict.get(key)
            if v is not None:
                return v
            if key.startswith(_SIG_PREFIX):
                return signal_dict.get(key[len(_SIG_PREFIX):])
            return None

        # Build numeric feature vector
        row = []
        for f in NUMERIC_FEATURES:
            row.append(_safe_float(_get(f)))

        # Build categorical features
        cat_vals = []
        for f in CATEGORICAL_FEATURES:
            v = _get(f)
            if v is None or v == "":
                v = "unknown"
            cat_vals.append(str(v))

        # Encode categoricals
        cat_encoded = []
        for j, f in enumerate(CATEGORICAL_FEATURES):
            le = self.encoders.get(f)
            val = cat_vals[j]
            if le is not None and val in le.classes_:
                cat_encoded.append(float(le.transform([val])[0]))
            else:
                # Unknown category — use 0 (will be imputed)
                cat_encoded.append(np.nan)

        X = np.array([row + cat_encoded], dtype=float)
        X = self.imputer.transform(X)

        proba = self.model.predict_proba(X)[0, 1]
        return float(proba)

    def gate(self, signal_dict: dict, threshold: float = 0.35) -> tuple:
        """Score and return (allow: bool, probability: float).

        Args:
            signal_dict: Signal features dict.
            threshold: Minimum win probability to allow entry (default 0.35).

        Returns:
            (allow, probability) tuple.
        """
        prob = self.score(signal_dict)
        return prob >= threshold, prob


def main():
    threshold = 0.35
    args = sys.argv[1:]
    if "--threshold" in args:
        idx = args.index("--threshold")
        threshold = float(args[idx + 1])

    scorer = GovernorScorer()
    stats = scorer.training_stats
    print(f"Governor model loaded: {stats.get('n_trades', '?')} trades, "
          f"{stats.get('n_features', '?')} features, "
          f"baseline WR={stats.get('win_rate', 0):.1%}")

    # Test with a few synthetic scenarios
    scenarios = [
        {
            "name": "Strong entry (high confluence, trend, good hour)",
            "sig_confluence_score": 95,
            "sig_dist_ma200_pct": 1.5,
            "sig_trend_strength": 0.8,
            "sig_score": 85, "sig_score_5m": 80, "sig_score_1h": 90,
            "sig_candle_volume_1m": 50000,
            "sig_vol": 100000,
            "sig_liq_vol_1m": 200000, "sig_liq_vol_baseline": 150000,
            "sig_liq_atr_norm": 0.3, "sig_liq_spread_bps": 2.0,
            "sig_regime": "TREND_UP", "sig_session": "OVERLAP",
            "sig_confluence_gate": "TRADE",
            "entry_hour_utc": 15, "entry_dow": 2,
        },
        {
            "name": "Weak entry (low confluence, bad hour, RANGE)",
            "sig_confluence_score": 45,
            "sig_dist_ma200_pct": -0.5,
            "sig_trend_strength": 0.1,
            "sig_score": 30, "sig_score_5m": 25, "sig_score_1h": 20,
            "sig_candle_volume_1m": 10000,
            "sig_vol": 30000,
            "sig_liq_vol_1m": 50000, "sig_liq_vol_baseline": 80000,
            "sig_liq_atr_norm": 0.8, "sig_liq_spread_bps": 8.0,
            "sig_regime": "RANGE", "sig_session": "OFF",
            "sig_confluence_gate": "WATCH",
            "entry_hour_utc": 6, "entry_dow": 6,
        },
        {
            "name": "Medium entry (decent score, ASIA session)",
            "sig_confluence_score": 75,
            "sig_dist_ma200_pct": 0.8,
            "sig_trend_strength": 0.4,
            "sig_score": 60, "sig_score_5m": 55, "sig_score_1h": 65,
            "sig_candle_volume_1m": 30000,
            "sig_vol": 60000,
            "sig_liq_vol_1m": 100000, "sig_liq_vol_baseline": 120000,
            "sig_liq_atr_norm": 0.5, "sig_liq_spread_bps": 4.0,
            "sig_regime": "TREND_UP", "sig_session": "ASIA",
            "sig_confluence_gate": "TRADE",
            "entry_hour_utc": 3, "entry_dow": 1,
        },
    ]

    print(f"\nGate threshold: {threshold}")
    print()
    for s in scenarios:
        name = s.pop("name")
        allow, prob = scorer.gate(s, threshold=threshold)
        status = "ALLOW" if allow else "BLOCK"
        print(f"  [{status}] {prob:.1%} win prob — {name}")


if __name__ == "__main__":
    main()