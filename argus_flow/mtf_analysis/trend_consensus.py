"""Multi-timeframe trend consensus with hierarchical override."""
from __future__ import annotations

from .types import TrendInfo


def classify_trend(price: float, ema50: float, ema100: float, ema200: float) -> TrendInfo:
    """Classify trend from EMA alignment at a single timeframe."""
    if ema50 == 0 or ema100 == 0 or ema200 == 0:
        return TrendInfo(direction="neutral", ema50=ema50, ema100=ema100, ema200=ema200)

    info = TrendInfo(ema50=ema50, ema100=ema100, ema200=ema200)

    # Price position relative to EMAs
    above_50 = price > ema50
    above_100 = price > ema100
    above_200 = price > ema200
    info.price_vs_ema50 = "above" if above_50 else "below"

    # EMA alignment
    ema_bullish = ema50 > ema100 > ema200
    ema_bearish = ema50 < ema100 < ema200

    # Strong trend: price and all EMAs aligned
    if above_50 and above_100 and above_200 and ema_bullish:
        info.direction = "up"
        info.strength = 1.0
    elif not above_50 and not above_100 and not above_200 and ema_bearish:
        info.direction = "down"
        info.strength = 1.0
    # Moderate trend: EMAs aligned but price may be pulling back
    elif ema_bullish:
        info.direction = "up"
        info.strength = 0.6 if above_200 else 0.3
    elif ema_bearish:
        info.direction = "down"
        info.strength = 0.6 if not above_200 else 0.3
    # Weak/transitioning: EMAs not aligned
    elif above_200 and above_100:
        info.direction = "up"
        info.strength = 0.2
    elif not above_200 and not above_100:
        info.direction = "down"
        info.strength = 0.2
    else:
        info.direction = "neutral"
        info.strength = 0.0

    return info


def compute_consensus(
    trend_4h: TrendInfo,
    trend_1h: TrendInfo,
    trend_30m: TrendInfo,
    trend_5m: TrendInfo,
) -> tuple[float, str, float]:
    """Compute weighted consensus across timeframes.

    Returns:
        (consensus_score, directional_bias, confidence)
        consensus_score: -1.0 (strong short) to +1.0 (strong long)
        directional_bias: LONG_ONLY | SHORT_ONLY | BOTH | NONE
        confidence: 0.0 - 1.0
    """
    # Weights: higher timeframes matter more
    weights = {"4h": 0.40, "1h": 0.25, "30m": 0.20, "5m": 0.15}

    trends = {"4h": trend_4h, "1h": trend_1h, "30m": trend_30m, "5m": trend_5m}

    score = 0.0
    total_weight = 0.0
    for tf, weight in weights.items():
        t = trends[tf]
        if t.direction == "up":
            score += weight * t.strength
        elif t.direction == "down":
            score -= weight * t.strength
        total_weight += weight

    if total_weight > 0:
        score /= total_weight

    # Confidence: how much do timeframes agree?
    directions = [t.direction for t in trends.values() if t.direction != "neutral"]
    if not directions:
        confidence = 0.0
    else:
        up_count = sum(1 for d in directions if d == "up")
        down_count = sum(1 for d in directions if d == "down")
        max_agree = max(up_count, down_count)
        confidence = max_agree / len(directions)

    # 4H override: if 4H is strong, it dominates
    if trend_4h.strength >= 0.8:
        confidence = max(confidence, 0.8)
        if trend_4h.direction == "up":
            score = max(score, 0.5)
        elif trend_4h.direction == "down":
            score = min(score, -0.5)

    # Directional bias thresholds
    if score >= 0.4 and confidence >= 0.5:
        bias = "LONG_ONLY"
    elif score <= -0.4 and confidence >= 0.5:
        bias = "SHORT_ONLY"
    elif abs(score) <= 0.2:
        bias = "BOTH"
    else:
        # Moderate lean but not enough to restrict
        # If timeframes conflict strongly, block everything
        if confidence < 0.3 and len(directions) >= 3:
            bias = "NONE"
        else:
            bias = "BOTH"

    return score, bias, confidence
