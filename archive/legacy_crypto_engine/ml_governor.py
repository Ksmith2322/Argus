"""ml_governor.py -- ML Governor integration for engine entry decisions.

Bridges the trained ML model with the engine's DecisionSnapshot to provide
a win probability score at entry time. Acts as a score modifier (not a hard gate)
per the Truth-Surface Doctrine: ML never replaces deterministic rules.

Integration points:
  1. Engine calls `governor.evaluate(snap)` after confluence/risk checks pass
  2. Governor returns GovernorResult with win_prob and recommendation
  3. Engine can use governor_score as an additional confluence modifier
     or emit it in signals CSV for offline analysis

Config (.env):
  USE_ML_GOVERNOR=false         # master switch (default off)
  ML_GOVERNOR_THRESHOLD=0.30    # below this → BLOCK recommendation
  ML_GOVERNOR_MODE=LOG_ONLY     # LOG_ONLY (default) | SCORE_MODIFY | GATE
"""
import os
import pickle
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

REPO = Path(__file__).resolve().parent
MODEL_PATH = REPO / "data" / "ml_governor.pkl"


@dataclass
class GovernorResult:
    """Result of ML governor evaluation."""
    win_prob: float           # 0.0 - 1.0
    recommendation: str       # "ALLOW", "CAUTION", "BLOCK"
    score_modifier: int       # suggested confluence score adjustment (-20 to +10)
    reason: str               # human-readable explanation
    model_loaded: bool = True


# Singleton — avoid reloading model on every tick
_cached_scorer = None


def _get_scorer():
    global _cached_scorer
    if _cached_scorer is None:
        _cached_scorer = _GovernorScorer()
    return _cached_scorer


class _GovernorScorer:
    """Internal scorer that loads model once."""

    def __init__(self):
        self.model = None
        self.imputer = None
        self.encoders = None
        self.loaded = False

        if not MODEL_PATH.exists():
            return

        try:
            with open(MODEL_PATH, "rb") as f:
                artifact = pickle.load(f)
            self.model = artifact["model"]
            self.imputer = artifact["imputer"]
            self.encoders = artifact["encoders"]
            self.numeric_features = artifact["numeric_features"]
            self.categorical_features = artifact["categorical_features"]
            self.loaded = True
        except Exception:
            self.loaded = False

    def score(self, features: dict) -> float:
        """Return win probability given a feature dict."""
        if not self.loaded:
            return 0.5  # neutral if no model

        row = []
        for f in self.numeric_features:
            v = features.get(f)
            if v is None or v == "" or v == "None":
                row.append(np.nan)
            else:
                try:
                    row.append(float(v))
                except (ValueError, TypeError):
                    row.append(np.nan)

        cat_encoded = []
        for f in self.categorical_features:
            v = str(features.get(f, "") or "unknown")
            le = self.encoders.get(f)
            if le is not None and v in le.classes_:
                cat_encoded.append(float(le.transform([v])[0]))
            else:
                cat_encoded.append(np.nan)

        X = np.array([row + cat_encoded], dtype=float)
        X = self.imputer.transform(X)
        return float(self.model.predict_proba(X)[0, 1])


def _snap_to_features(snap) -> dict:
    """Extract ML features from a DecisionSnapshot object.

    Maps snap attributes to the feature names expected by the model.
    Handles both attribute access and dict-style access.
    """
    def _get(attr, default=None):
        if hasattr(snap, attr):
            return getattr(snap, attr, default)
        if isinstance(snap, dict):
            return snap.get(attr, default)
        return default

    # Compute entry hour/dow from epoch
    epoch = _get("epoch") or _get("now_epoch") or 0
    hour_utc = None
    dow = None
    if epoch:
        try:
            dt = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
            hour_utc = dt.hour
            dow = dt.weekday()
        except Exception:
            pass

    return {
        "sig_score": _get("score"),
        "sig_score_5m": _get("score_5m"),
        "sig_score_1h": _get("score_1h"),
        "sig_dist_ma200_pct": _get("dist_ma200_pct"),
        "sig_confluence_score": _get("confluence_score"),
        "sig_ac_base_score": _get("ac_base_score"),
        "sig_ac_adjusted_score": _get("ac_adjusted_score"),
        "sig_ac_delta": _get("ac_delta"),
        "sig_candle_volume_1m": _get("candle_volume_1m"),
        "sig_vol": _get("vol"),
        "sig_liq_spread_bps": _get("liq_spread_bps"),
        "sig_liq_vol_1m": _get("liq_vol_1m"),
        "sig_liq_vol_baseline": _get("liq_vol_baseline"),
        "sig_liq_atr_norm": _get("liq_atr_norm"),
        "sig_liq_penalty_points": _get("liq_penalty_points"),
        "sig_trend_strength": _get("trend_strength"),
        "sig_session_bonus_points": _get("session_bonus_points"),
        "sig_session_risk_mult": _get("session_risk_mult"),
        "sig_dist_support": _get("dist_support"),
        "sig_dist_resistance": _get("dist_resistance"),
        "sig_cooldown_remaining_s": _get("cooldown_remaining_s"),
        "entry_hour_utc": hour_utc,
        "entry_dow": dow,
        "sig_regime": _get("regime"),
        "sig_session": _get("session"),
        "sig_confluence_gate": _get("confluence_gate"),
    }


def evaluate(snap, cfg: dict = None) -> GovernorResult:
    """Evaluate a DecisionSnapshot and return governor recommendation.

    This is the main entry point called from engine.py.
    Safe to call even if model is not loaded (returns neutral result).
    """
    if cfg is None:
        cfg = {}

    use_governor = str(cfg.get("USE_ML_GOVERNOR", "false")).lower() in ("true", "1", "yes")
    if not use_governor:
        return GovernorResult(
            win_prob=0.5,
            recommendation="DISABLED",
            score_modifier=0,
            reason="USE_ML_GOVERNOR=false",
            model_loaded=False,
        )

    scorer = _get_scorer()
    if not scorer.loaded:
        return GovernorResult(
            win_prob=0.5,
            recommendation="NO_MODEL",
            score_modifier=0,
            reason="model not found at data/ml_governor.pkl",
            model_loaded=False,
        )

    features = _snap_to_features(snap)
    win_prob = scorer.score(features)

    threshold = float(cfg.get("ML_GOVERNOR_THRESHOLD", "0.30"))
    caution_threshold = threshold + 0.15  # e.g., 0.45 if threshold=0.30

    if win_prob >= caution_threshold:
        recommendation = "ALLOW"
        score_modifier = min(10, int((win_prob - 0.5) * 20))
    elif win_prob >= threshold:
        recommendation = "CAUTION"
        score_modifier = 0
    else:
        recommendation = "BLOCK"
        score_modifier = max(-20, int((win_prob - threshold) * 50))

    reason = f"win_prob={win_prob:.3f} threshold={threshold:.2f}"

    return GovernorResult(
        win_prob=win_prob,
        recommendation=recommendation,
        score_modifier=score_modifier,
        reason=reason,
        model_loaded=True,
    )