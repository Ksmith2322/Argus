"""Support / resistance detection from OHLC data.

Methods: price-cluster binning, standard & Fibonacci pivots,
linear-regression trendlines, and a combined nearest-S/R helper.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# -- helpers ---------------------------------------------------------------

def _atr(df: pd.DataFrame, period: int = 14) -> float:
    """Average True Range (scalar, last value)."""
    h, l, c = df["High"].values, df["Low"].values, df["Close"].values
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    return float(np.mean(tr[-period:]))


# -- Price-cluster S/R -----------------------------------------------------

def find_sr_clusters(
    df: pd.DataFrame,
    num_levels: int = 5,
    atr_divisor: int = 10,
) -> list[dict]:
    """Detect support/resistance by binning highs & lows into price buckets.

    Parameters
    ----------
    df : pd.DataFrame
        OHLC with datetime index.
    num_levels : int
        How many S/R levels to return.
    atr_divisor : int
        ``bucket_size = ATR / atr_divisor``.

    Returns
    -------
    List of ``{"price": float, "strength": int, "kind": "support"|"resistance"}``
    sorted by strength descending, capped at *num_levels*.
    """
    atr = _atr(df)
    if atr == 0:
        return []
    bucket_size = atr / atr_divisor

    highs = df["High"].values
    lows = df["Low"].values
    all_prices = np.concatenate([highs, lows])
    price_min = all_prices.min()

    # bucket each price
    high_buckets = ((highs - price_min) / bucket_size).astype(int)
    low_buckets = ((lows - price_min) / bucket_size).astype(int)

    # count touches per bucket, split by high/low origin
    from collections import Counter

    high_counts: Counter[int] = Counter(high_buckets.tolist())
    low_counts: Counter[int] = Counter(low_buckets.tolist())
    all_buckets = set(high_counts) | set(low_counts)

    levels: list[dict] = []
    for b in all_buckets:
        hc = high_counts.get(b, 0)
        lc = low_counts.get(b, 0)
        strength = hc + lc
        kind = "support" if lc >= hc else "resistance"
        price = price_min + (b + 0.5) * bucket_size
        levels.append({"price": float(price), "strength": int(strength), "kind": kind})

    levels.sort(key=lambda x: x["strength"], reverse=True)
    return levels[:num_levels]


# -- Pivot Points ----------------------------------------------------------

def compute_pivots(
    prev_high: float, prev_low: float, prev_close: float
) -> dict[str, float]:
    """Standard and Fibonacci pivot points from the previous period's HLC.

    Returns dict with keys: pp, r1-r3, s1-s3, fib_r1-fib_r3, fib_s1-fib_s3.
    """
    pp = (prev_high + prev_low + prev_close) / 3.0
    hl = prev_high - prev_low

    return {
        "pp": pp,
        # Standard
        "r1": 2 * pp - prev_low,
        "r2": pp + hl,
        "r3": prev_high + 2 * (pp - prev_low),
        "s1": 2 * pp - prev_high,
        "s2": pp - hl,
        "s3": prev_low - 2 * (prev_high - pp),
        # Fibonacci
        "fib_r1": pp + 0.382 * hl,
        "fib_r2": pp + 0.618 * hl,
        "fib_r3": pp + hl,
        "fib_s1": pp - 0.382 * hl,
        "fib_s2": pp - 0.618 * hl,
        "fib_s3": pp - hl,
    }


# -- Trendline Detection ---------------------------------------------------

def _fit_line(points: list[dict]) -> tuple[float, float, float, int]:
    """Fit a line to swing points via least-squares.

    Returns (slope, intercept, r2, n_points).
    slope is in price-per-bar units.
    """
    if len(points) < 2:
        return 0.0, 0.0, 0.0, len(points)

    x = np.array([p["bar_index"] for p in points], dtype=float)
    y = np.array([p["price"] for p in points], dtype=float)

    # linear regression
    n = len(x)
    sx = x.sum()
    sy = y.sum()
    sxy = (x * y).sum()
    sxx = (x * x).sum()

    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0, float(y.mean()), 0.0, n

    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n

    # R-squared
    y_pred = slope * x + intercept
    ss_res = ((y - y_pred) ** 2).sum()
    ss_tot = ((y - y.mean()) ** 2).sum()
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    return float(slope), float(intercept), float(r2), n


def _count_touches(
    points: list[dict], slope: float, intercept: float, tol_pct: float = 0.002
) -> int:
    """Count how many points lie within *tol_pct* of the fitted line."""
    touches = 0
    for p in points:
        expected = slope * p["bar_index"] + intercept
        if expected == 0:
            continue
        if abs(p["price"] - expected) / abs(expected) <= tol_pct:
            touches += 1
    return touches


def find_trendlines(
    swing_highs: list[dict],
    swing_lows: list[dict],
    min_touches: int = 2,
) -> dict[str, object]:
    """Fit ascending (support) and descending (resistance) trendlines.

    Uses the most recent 3-5 swing points for each side.

    Parameters
    ----------
    swing_highs, swing_lows : list[dict]
        Output from :func:`swing_detection.find_swings`.
    min_touches : int
        Minimum touches to consider a trendline valid.

    Returns
    -------
    Dict with ascending_slope, ascending_intercept, ascending_r2,
    ascending_touches, descending_slope, descending_intercept,
    descending_r2, descending_touches, converging (bool).
    """
    recent_lows = swing_lows[-5:] if len(swing_lows) >= 3 else swing_lows
    recent_highs = swing_highs[-5:] if len(swing_highs) >= 3 else swing_highs

    a_slope, a_int, a_r2, _ = _fit_line(recent_lows)
    d_slope, d_int, d_r2, _ = _fit_line(recent_highs)

    a_touches = _count_touches(swing_lows, a_slope, a_int) if swing_lows else 0
    d_touches = _count_touches(swing_highs, d_slope, d_int) if swing_highs else 0

    # converging = ascending slope > descending slope (lines narrow)
    converging = (a_slope > 0 and d_slope < 0) or (
        a_slope > d_slope and len(recent_lows) >= 2 and len(recent_highs) >= 2
    )

    return {
        "ascending_slope": a_slope,
        "ascending_intercept": a_int,
        "ascending_r2": a_r2,
        "ascending_touches": a_touches,
        "descending_slope": d_slope,
        "descending_intercept": d_int,
        "descending_r2": d_r2,
        "descending_touches": d_touches,
        "converging": converging,
    }


# -- Combined S/R helper ---------------------------------------------------

def get_nearest_sr(
    price: float, levels: list[dict]
) -> tuple[dict | None, dict | None]:
    """Find the nearest support below and nearest resistance above *price*.

    Parameters
    ----------
    price : float
        Current market price.
    levels : list[dict]
        Each dict must have at least ``"price"`` and ``"kind"`` keys.

    Returns
    -------
    (nearest_support, nearest_resistance) — either may be ``None`` if no
    qualifying level exists on that side.
    """
    best_sup: dict | None = None
    best_res: dict | None = None
    sup_dist = float("inf")
    res_dist = float("inf")

    for lvl in levels:
        lp = lvl["price"]
        if lp <= price:
            d = price - lp
            if d < sup_dist:
                sup_dist = d
                best_sup = lvl
        else:
            d = lp - price
            if d < res_dist:
                res_dist = d
                best_res = lvl

    return best_sup, best_res
