"""Chart pattern recognition from OHLC data and swing points."""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pct_diff(a: float, b: float) -> float:
    """Absolute percentage difference between two prices."""
    mid = (a + b) / 2.0
    if mid == 0:
        return 0.0
    return abs(a - b) / mid


def _linreg(xs: np.ndarray, ys: np.ndarray) -> tuple[float, float, float]:
    """Simple linear regression.  Returns (slope, intercept, r_squared)."""
    n = len(xs)
    if n < 2:
        return 0.0, ys[0] if n else 0.0, 0.0
    xs = xs.astype(float)
    ys = ys.astype(float)
    mx, my = xs.mean(), ys.mean()
    ss_xx = ((xs - mx) ** 2).sum()
    if ss_xx == 0:
        return 0.0, my, 0.0
    slope = ((xs - mx) * (ys - my)).sum() / ss_xx
    intercept = my - slope * mx
    ss_res = ((ys - (slope * xs + intercept)) ** 2).sum()
    ss_tot = ((ys - my) ** 2).sum()
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return slope, intercept, r2


# ---------------------------------------------------------------------------
# Double Top / Double Bottom
# ---------------------------------------------------------------------------

def detect_double_top(
    swing_highs: list[dict],
    swing_lows: list[dict],
    tolerance_pct: float = 0.002,
) -> dict | None:
    """Detect a double top from the most recent swing highs.

    Requires two swing highs within *tolerance_pct* of each other with at
    least one swing low (valley) between them.
    """
    if len(swing_highs) < 2:
        return None

    h2 = swing_highs[-1]
    h1 = swing_highs[-2]

    if _pct_diff(h1["price"], h2["price"]) > tolerance_pct:
        return None

    # Find the lowest swing low between the two highs
    valleys = [
        sl for sl in swing_lows
        if h1["bar_index"] < sl["bar_index"] < h2["bar_index"]
    ]
    if not valleys:
        return None

    neckline = min(v["price"] for v in valleys)
    peak_avg = (h1["price"] + h2["price"]) / 2.0
    range_size = peak_avg - neckline
    if range_size <= 0:
        return None

    # completion: how far price moved from second peak toward neckline
    drop_from_peak = h2["price"] - neckline
    completion = min(1.0, drop_from_peak / range_size) if range_size > 0 else 0.0

    # confidence: tighter peaks + deeper valley = more confident
    peak_sim = 1.0 - _pct_diff(h1["price"], h2["price"]) / max(tolerance_pct, 1e-9)
    conf = np.clip(peak_sim * 0.6 + completion * 0.4, 0.0, 1.0)

    return {
        "pattern": "double_top",
        "direction_bias": "short",
        "confidence": float(round(conf, 4)),
        "neckline": neckline,
        "completion_pct": float(round(completion, 4)),
    }


def detect_double_bottom(
    swing_highs: list[dict],
    swing_lows: list[dict],
    tolerance_pct: float = 0.002,
) -> dict | None:
    """Mirror of double top — two swing lows near the same price."""
    if len(swing_lows) < 2:
        return None

    l2 = swing_lows[-1]
    l1 = swing_lows[-2]

    if _pct_diff(l1["price"], l2["price"]) > tolerance_pct:
        return None

    peaks = [
        sh for sh in swing_highs
        if l1["bar_index"] < sh["bar_index"] < l2["bar_index"]
    ]
    if not peaks:
        return None

    neckline = max(p["price"] for p in peaks)
    trough_avg = (l1["price"] + l2["price"]) / 2.0
    range_size = neckline - trough_avg
    if range_size <= 0:
        return None

    rise_from_trough = neckline - l2["price"]
    completion = min(1.0, rise_from_trough / range_size) if range_size > 0 else 0.0

    trough_sim = 1.0 - _pct_diff(l1["price"], l2["price"]) / max(tolerance_pct, 1e-9)
    conf = np.clip(trough_sim * 0.6 + completion * 0.4, 0.0, 1.0)

    return {
        "pattern": "double_bottom",
        "direction_bias": "long",
        "confidence": float(round(conf, 4)),
        "neckline": neckline,
        "completion_pct": float(round(completion, 4)),
    }


# ---------------------------------------------------------------------------
# Head and Shoulders
# ---------------------------------------------------------------------------

def detect_head_shoulders(
    swing_highs: list[dict],
    swing_lows: list[dict],
    tolerance_pct: float = 0.003,
) -> dict | None:
    """Detect a head-and-shoulders top from the three most recent swing highs."""
    if len(swing_highs) < 3:
        return None

    ls, head, rs = swing_highs[-3], swing_highs[-2], swing_highs[-1]

    # Head must be the highest
    if not (head["price"] > ls["price"] and head["price"] > rs["price"]):
        return None

    # Shoulders within tolerance of each other
    if _pct_diff(ls["price"], rs["price"]) > tolerance_pct:
        return None

    # Find troughs between LS-Head and Head-RS
    t1 = [sl for sl in swing_lows if ls["bar_index"] < sl["bar_index"] < head["bar_index"]]
    t2 = [sl for sl in swing_lows if head["bar_index"] < sl["bar_index"] < rs["bar_index"]]
    if not t1 or not t2:
        return None

    trough1 = min(t1, key=lambda s: s["price"])["price"]
    trough2 = min(t2, key=lambda s: s["price"])["price"]
    neckline = (trough1 + trough2) / 2.0

    shoulder_sim = 1.0 - _pct_diff(ls["price"], rs["price"]) / max(tolerance_pct, 1e-9)
    head_prominence = (head["price"] - (ls["price"] + rs["price"]) / 2.0) / head["price"]
    conf = np.clip(shoulder_sim * 0.5 + min(head_prominence * 10, 1.0) * 0.5, 0.0, 1.0)

    return {
        "pattern": "head_shoulders",
        "direction_bias": "short",
        "confidence": float(round(conf, 4)),
        "neckline": float(round(neckline, 6)),
        "head_price": head["price"],
    }


def detect_inv_head_shoulders(
    swing_highs: list[dict],
    swing_lows: list[dict],
    tolerance_pct: float = 0.003,
) -> dict | None:
    """Inverse head-and-shoulders (bottom reversal)."""
    if len(swing_lows) < 3:
        return None

    ls, head, rs = swing_lows[-3], swing_lows[-2], swing_lows[-1]

    # Head must be the lowest
    if not (head["price"] < ls["price"] and head["price"] < rs["price"]):
        return None

    if _pct_diff(ls["price"], rs["price"]) > tolerance_pct:
        return None

    p1 = [sh for sh in swing_highs if ls["bar_index"] < sh["bar_index"] < head["bar_index"]]
    p2 = [sh for sh in swing_highs if head["bar_index"] < sh["bar_index"] < rs["bar_index"]]
    if not p1 or not p2:
        return None

    peak1 = max(p1, key=lambda s: s["price"])["price"]
    peak2 = max(p2, key=lambda s: s["price"])["price"]
    neckline = (peak1 + peak2) / 2.0

    shoulder_sim = 1.0 - _pct_diff(ls["price"], rs["price"]) / max(tolerance_pct, 1e-9)
    head_prominence = ((ls["price"] + rs["price"]) / 2.0 - head["price"]) / head["price"]
    conf = np.clip(shoulder_sim * 0.5 + min(head_prominence * 10, 1.0) * 0.5, 0.0, 1.0)

    return {
        "pattern": "inv_head_shoulders",
        "direction_bias": "long",
        "confidence": float(round(conf, 4)),
        "neckline": float(round(neckline, 6)),
        "head_price": head["price"],
    }


# ---------------------------------------------------------------------------
# Triangle Patterns
# ---------------------------------------------------------------------------

def detect_triangle(
    swing_highs: list[dict],
    swing_lows: list[dict],
    min_points: int = 4,
) -> dict | None:
    """Detect ascending, descending, or symmetric triangle patterns.

    Uses linear regression on the last *min_points* swing highs and swing
    lows to classify the triangle type.
    """
    if len(swing_highs) < min_points or len(swing_lows) < min_points:
        return None

    recent_h = swing_highs[-min_points:]
    recent_l = swing_lows[-min_points:]

    h_x = np.array([s["bar_index"] for s in recent_h])
    h_y = np.array([s["price"] for s in recent_h])
    l_x = np.array([s["bar_index"] for s in recent_l])
    l_y = np.array([s["price"] for s in recent_l])

    h_slope, h_int, h_r2 = _linreg(h_x, h_y)
    l_slope, l_int, l_r2 = _linreg(l_x, l_y)

    # Normalise slopes by average price for threshold comparisons
    avg_price = (h_y.mean() + l_y.mean()) / 2.0
    if avg_price == 0:
        return None
    h_slope_norm = h_slope / avg_price
    l_slope_norm = l_slope / avg_price

    flat_thresh = 1e-6  # slope per bar normalised

    # Classify
    if abs(h_slope_norm) < flat_thresh and l_slope_norm > flat_thresh:
        pat = "triangle_asc"
        bias = "long"
    elif abs(l_slope_norm) < flat_thresh and h_slope_norm < -flat_thresh:
        pat = "triangle_desc"
        bias = "short"
    elif h_slope_norm < -flat_thresh and l_slope_norm > flat_thresh:
        pat = "triangle_sym"
        bias = "neutral"
    else:
        return None  # no converging lines

    # Apex: where the two regression lines meet
    denom = l_slope - h_slope
    if abs(denom) < 1e-12:
        return None
    apex_x = (h_int - l_int) / denom
    apex_price = h_slope * apex_x + h_int

    # Completion: how far along the triangle we are (last bar vs apex)
    last_bar = max(h_x[-1], l_x[-1])
    first_bar = min(h_x[0], l_x[0])
    total_span = apex_x - first_bar
    completion = float(np.clip((last_bar - first_bar) / total_span, 0.0, 1.0)) if total_span > 0 else 0.0

    # Confidence from R-squared of regression fits
    conf = np.clip((h_r2 + l_r2) / 2.0 * 0.7 + completion * 0.3, 0.0, 1.0)

    return {
        "pattern": pat,
        "direction_bias": bias,
        "confidence": float(round(conf, 4)),
        "apex_price": float(round(apex_price, 6)),
        "completion_pct": float(round(completion, 4)),
    }


# ---------------------------------------------------------------------------
# Flag / Pennant
# ---------------------------------------------------------------------------

def detect_flag(
    df: pd.DataFrame,
    swing_highs: list[dict],
    swing_lows: list[dict],
    min_pole_pct: float = 0.005,
) -> dict | None:
    """Detect bull/bear flag: a strong pole followed by tight consolidation.

    Parameters
    ----------
    df : pd.DataFrame
        OHLC data with High/Low/Close columns.
    min_pole_pct : float
        Minimum price move (as fraction) to qualify as a pole.
    """
    if len(df) < 25 or len(swing_highs) < 2 or len(swing_lows) < 2:
        return None

    closes = df["Close"].values
    highs = df["High"].values
    lows = df["Low"].values
    n = len(closes)

    # Search the last 50 bars for a pole
    search_start = max(0, n - 50)
    best: dict | None = None

    for pole_len in range(5, 21):
        pole_end = n - 1
        pole_start_idx = pole_end - pole_len
        if pole_start_idx < search_start:
            continue

        pole_move = closes[pole_end] - closes[pole_start_idx]
        pole_pct = abs(pole_move) / closes[pole_start_idx] if closes[pole_start_idx] != 0 else 0.0
        if pole_pct < min_pole_pct:
            continue

        pole_range = highs[pole_start_idx:pole_end + 1].max() - lows[pole_start_idx:pole_end + 1].min()
        if pole_range <= 0:
            continue

        is_bull = pole_move > 0

        # Check consolidation zone after pole (remaining bars to end)
        consol_start = pole_end
        consol_end = min(n, consol_start + 30)
        consol_len = consol_end - consol_start
        if consol_len < 5:
            continue

        consol_range = highs[consol_start:consol_end].max() - lows[consol_start:consol_end].min()
        if consol_range > 0.5 * pole_range:
            continue

        # Check slight counter-trend drift in consolidation
        consol_slope = closes[consol_end - 1] - closes[consol_start]
        if is_bull and consol_slope > 0:
            continue  # should drift slightly down for bull flag
        if not is_bull and consol_slope < 0:
            continue

        conf = np.clip(
            pole_pct / (min_pole_pct * 5) * 0.5 + (1.0 - consol_range / pole_range) * 0.5,
            0.0, 1.0,
        )

        candidate = {
            "pattern": "flag_bull" if is_bull else "flag_bear",
            "direction_bias": "long" if is_bull else "short",
            "confidence": float(round(conf, 4)),
            "pole_start": float(closes[pole_start_idx]),
            "pole_end": float(closes[pole_end]),
        }

        if best is None or candidate["confidence"] > best["confidence"]:
            best = candidate

    return best


# ---------------------------------------------------------------------------
# Combined Scanner
# ---------------------------------------------------------------------------

def scan_patterns(
    df: pd.DataFrame,
    swing_highs: list[dict],
    swing_lows: list[dict],
) -> list[dict]:
    """Run all pattern detectors and return active patterns sorted by confidence."""
    results: list[dict] = []

    for fn in (
        lambda: detect_double_top(swing_highs, swing_lows),
        lambda: detect_double_bottom(swing_highs, swing_lows),
        lambda: detect_head_shoulders(swing_highs, swing_lows),
        lambda: detect_inv_head_shoulders(swing_highs, swing_lows),
        lambda: detect_triangle(swing_highs, swing_lows),
        lambda: detect_flag(df, swing_highs, swing_lows),
    ):
        pat = fn()
        if pat is not None:
            results.append(pat)

    results.sort(key=lambda p: p["confidence"], reverse=True)
    return results
