"""
Mamba Trendline & S/R Detection Engine (v2)
=============================================
Identifies trendlines, horizontal S/R levels, and 1-min market structure
on NQ=F and YM=F data per the MambaFX rulebook.

Core algorithm:
1. Find swing highs/lows (pivot points)
2. Fit trendlines through recent pivots
3. Count how many times price touches/respects the line
4. Detect when price breaks through with conviction (close + volume)
5. Find horizontal S/R levels with 2+ touches
6. Detect 1-min market structure shifts (HH/HL or LL/LH)
7. Score confluences for trade quality
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pivot detection
# ---------------------------------------------------------------------------

def find_pivot_highs(df: pd.DataFrame, left_bars: int = 5, right_bars: int = 2) -> list[dict]:
    """Find swing highs where High[i] is the highest in [i-left, i+right]."""
    highs = df["High"].values
    pivots = []
    for i in range(left_bars, len(highs) - right_bars):
        window_left = highs[i - left_bars : i]
        window_right = highs[i + 1 : i + right_bars + 1]
        if len(window_left) == 0 or len(window_right) == 0:
            continue
        if highs[i] >= window_left.max() and highs[i] >= window_right.max():
            pivots.append({
                "index": i,
                "price": float(highs[i]),
                "date": df.index[i] if isinstance(df.index, pd.DatetimeIndex) else i,
            })
    return pivots


def find_pivot_lows(df: pd.DataFrame, left_bars: int = 5, right_bars: int = 2) -> list[dict]:
    """Find swing lows where Low[i] is the lowest in [i-left, i+right]."""
    lows = df["Low"].values
    pivots = []
    for i in range(left_bars, len(lows) - right_bars):
        window_left = lows[i - left_bars : i]
        window_right = lows[i + 1 : i + right_bars + 1]
        if len(window_left) == 0 or len(window_right) == 0:
            continue
        if lows[i] <= window_left.min() and lows[i] <= window_right.min():
            pivots.append({
                "index": i,
                "price": float(lows[i]),
                "date": df.index[i] if isinstance(df.index, pd.DatetimeIndex) else i,
            })
    return pivots


# ---------------------------------------------------------------------------
# Trendline fitting
# ---------------------------------------------------------------------------

def fit_trendline(pivots: list[dict], max_pivots: int = 2) -> Optional[dict]:
    """
    Fit a line through the 2 most recent pivots.
    Returns: {slope, intercept, start_bar, end_bar, direction, pivots}
    """
    if len(pivots) < 2:
        return None

    recent = pivots[-max_pivots:]
    p1, p2 = recent[0], recent[1]

    dx = p2["index"] - p1["index"]
    if dx == 0:
        return None

    slope = (p2["price"] - p1["price"]) / dx
    intercept = p1["price"] - slope * p1["index"]

    direction = "descending" if slope < 0 else "ascending"

    return {
        "slope": slope,
        "intercept": intercept,
        "start_bar": p1["index"],
        "end_bar": p2["index"],
        "direction": direction,
        "pivots": recent,
    }


def trendline_value_at(tl: dict, bar_index: int) -> float:
    """Get the trendline's price value at a given bar index."""
    return tl["slope"] * bar_index + tl["intercept"]


# ---------------------------------------------------------------------------
# Touch counting
# ---------------------------------------------------------------------------

def count_touches(df: pd.DataFrame, trendline: dict, tolerance_atr_mult: float = 0.2) -> int:
    """
    Count how many bars touch/respect the trendline within tolerance.
    A touch = relevant price extreme comes within tolerance * ATR of the trendline.
    For descending trendlines: check High
    For ascending trendlines: check Low
    """
    atr = df["ATR"].values
    start = trendline["start_bar"]
    end = min(trendline["end_bar"], len(df) - 1)

    touches = 0
    pivot_indices = {p["index"] for p in trendline["pivots"]}

    for i in range(start, end + 1):
        if i in pivot_indices:
            touches += 1
            continue

        tl_val = trendline_value_at(trendline, i)
        tol = tolerance_atr_mult * atr[i] if i < len(atr) else 0

        if trendline["direction"] == "descending":
            price = df["High"].iloc[i]
            if abs(price - tl_val) <= tol and price <= tl_val + tol:
                touches += 1
        else:
            price = df["Low"].iloc[i]
            if abs(price - tl_val) <= tol and price >= tl_val - tol:
                touches += 1

    return touches


# ---------------------------------------------------------------------------
# Breakout detection
# ---------------------------------------------------------------------------

def detect_breakout(
    df: pd.DataFrame,
    trendline: dict,
    bar_index: int,
    atr: float,
) -> Optional[dict]:
    """
    Check if the bar at bar_index breaks the trendline.
    Breakout requires: close beyond line + volume confirmation.
    """
    if bar_index < 0 or bar_index >= len(df):
        return None

    close = df["Close"].iloc[bar_index]
    tl_val = trendline_value_at(trendline, bar_index)

    vol_window = 20
    if bar_index >= vol_window:
        avg_vol = df["Volume"].iloc[bar_index - vol_window : bar_index].mean()
    else:
        avg_vol = df["Volume"].iloc[: bar_index].mean() if bar_index > 0 else 1
    current_vol = df["Volume"].iloc[bar_index]
    volume_ratio = current_vol / avg_vol if avg_vol > 0 else 0

    breakout_threshold = atr * 0.1

    if trendline["direction"] == "descending":
        if close > tl_val + breakout_threshold and volume_ratio >= 1.5:
            return {
                "direction": "LONG",
                "break_price": float(close),
                "trendline_value": float(tl_val),
                "volume_ratio": round(volume_ratio, 2),
                "bar_index": bar_index,
            }
    else:
        if close < tl_val - breakout_threshold and volume_ratio >= 1.5:
            return {
                "direction": "SHORT",
                "break_price": float(close),
                "trendline_value": float(tl_val),
                "volume_ratio": round(volume_ratio, 2),
                "bar_index": bar_index,
            }

    return None


# ---------------------------------------------------------------------------
# Support / Resistance level detection (NEW in v2)
# ---------------------------------------------------------------------------

def find_support_resistance(
    df: pd.DataFrame,
    tolerance_pct: float = 0.001,
    min_touches: int = 2,
) -> list[dict]:
    """
    Find horizontal S/R levels where price has reversed 2+ times.
    Group nearby reversals within tolerance_pct into zones.

    Returns: [{level: float, touches: int, type: 'support'|'resistance', strength: float}]
    """
    pivot_highs = find_pivot_highs(df, left_bars=3, right_bars=2)
    pivot_lows = find_pivot_lows(df, left_bars=3, right_bars=2)

    # Collect all reversal prices with their type
    reversals: list[tuple[float, str]] = []
    for p in pivot_highs:
        reversals.append((p["price"], "resistance"))
    for p in pivot_lows:
        reversals.append((p["price"], "support"))

    if not reversals:
        return []

    # Sort by price
    reversals.sort(key=lambda x: x[0])

    # Cluster nearby reversals into zones
    zones: list[dict] = []
    current_zone_prices: list[float] = [reversals[0][0]]
    current_zone_types: list[str] = [reversals[0][1]]

    for i in range(1, len(reversals)):
        price, rtype = reversals[i]
        zone_avg = np.mean(current_zone_prices)
        if abs(price - zone_avg) / zone_avg <= tolerance_pct:
            current_zone_prices.append(price)
            current_zone_types.append(rtype)
        else:
            # Finalize current zone
            if len(current_zone_prices) >= min_touches:
                sup_count = current_zone_types.count("support")
                res_count = current_zone_types.count("resistance")
                zone_type = "support" if sup_count >= res_count else "resistance"
                zones.append({
                    "level": round(float(np.mean(current_zone_prices)), 2),
                    "touches": len(current_zone_prices),
                    "type": zone_type,
                    "strength": round(len(current_zone_prices) / max(len(pivot_highs) + len(pivot_lows), 1), 3),
                })
            current_zone_prices = [price]
            current_zone_types = [rtype]

    # Don't forget the last zone
    if len(current_zone_prices) >= min_touches:
        sup_count = current_zone_types.count("support")
        res_count = current_zone_types.count("resistance")
        zone_type = "support" if sup_count >= res_count else "resistance"
        zones.append({
            "level": round(float(np.mean(current_zone_prices)), 2),
            "touches": len(current_zone_prices),
            "type": zone_type,
            "strength": round(len(current_zone_prices) / max(len(pivot_highs) + len(pivot_lows), 1), 3),
        })

    # Sort by strength descending
    zones.sort(key=lambda z: z["touches"], reverse=True)
    return zones


def check_sr_break(
    close: float,
    sr_levels: list[dict],
    direction: str,
    atr: float,
) -> Optional[dict]:
    """
    Check if price has broken through an S/R level.
    For LONG: close above a resistance level
    For SHORT: close below a support level
    Returns the broken level or None.
    """
    threshold = atr * 0.05  # small buffer

    for sr in sr_levels:
        if direction == "LONG" and sr["type"] == "resistance":
            if close > sr["level"] + threshold:
                return sr
        elif direction == "SHORT" and sr["type"] == "support":
            if close < sr["level"] - threshold:
                return sr
    return None


# ---------------------------------------------------------------------------
# 1-min market structure detection (NEW in v2)
# ---------------------------------------------------------------------------

def detect_1min_structure(df_1min: pd.DataFrame, direction: str) -> Optional[dict]:
    """
    On 1-min bars, detect market structure shift:
    - For LONG: find higher high -> higher low -> higher high pattern
    - For SHORT: find lower low -> lower high -> lower low pattern

    Uses small pivot detection (left=2, right=1) suitable for 1-min timeframe.

    Returns: {confirmed: bool, swing_points: [...], entry_bar: int} or None
    """
    if len(df_1min) < 8:
        return None

    # Use tight pivots for 1-min
    highs = find_pivot_highs(df_1min, left_bars=2, right_bars=1)
    lows = find_pivot_lows(df_1min, left_bars=2, right_bars=1)

    if direction == "LONG":
        # Need at least 2 swing highs and 1 swing low to form HH->HL->HH
        if len(highs) < 2 or len(lows) < 1:
            return None

        # Check last few swing points for HH + HL pattern
        # Find a higher high, then a higher low after it, then another push up
        for i in range(len(highs) - 1, 0, -1):
            h2 = highs[i]
            h1 = highs[i - 1]
            # h2 must be a higher high than h1
            if h2["price"] <= h1["price"]:
                continue

            # Find a low between h1 and h2 that is higher than a prior low
            lows_between = [l for l in lows if h1["index"] < l["index"] < h2["index"]]
            if not lows_between:
                continue

            # Check if this low is a higher low relative to any low before h1
            lows_before = [l for l in lows if l["index"] <= h1["index"]]
            if lows_before:
                recent_prior_low = lows_before[-1]["price"]
                hl = lows_between[-1]
                if hl["price"] > recent_prior_low:
                    return {
                        "confirmed": True,
                        "swing_points": [h1, hl, h2],
                        "entry_bar": h2["index"],
                    }

            # Even without a prior low, HH pattern alone is valid
            return {
                "confirmed": True,
                "swing_points": [h1, lows_between[-1], h2],
                "entry_bar": h2["index"],
            }

    elif direction == "SHORT":
        # Need at least 2 swing lows and 1 swing high to form LL->LH->LL
        if len(lows) < 2 or len(highs) < 1:
            return None

        for i in range(len(lows) - 1, 0, -1):
            l2 = lows[i]
            l1 = lows[i - 1]
            if l2["price"] >= l1["price"]:
                continue

            highs_between = [h for h in highs if l1["index"] < h["index"] < l2["index"]]
            if not highs_between:
                continue

            highs_before = [h for h in highs if h["index"] <= l1["index"]]
            if highs_before:
                recent_prior_high = highs_before[-1]["price"]
                lh = highs_between[-1]
                if lh["price"] < recent_prior_high:
                    return {
                        "confirmed": True,
                        "swing_points": [l1, lh, l2],
                        "entry_bar": l2["index"],
                    }

            return {
                "confirmed": True,
                "swing_points": [l1, highs_between[-1], l2],
                "entry_bar": l2["index"],
            }

    return None


# ---------------------------------------------------------------------------
# Confluence scoring (NEW in v2)
# ---------------------------------------------------------------------------

def score_confluences(
    sr_break: bool,
    trendline_break: bool,
    structure_confirmed: bool,
    volume_spike: bool,
    candle_quality: bool,
) -> int:
    """
    Count confluences. Need 2+ to trade.
    Each confirmation adds +1:
      - S/R level break
      - Trendline break
      - 1-min structure shift (HH/HL or LL/LH)
      - Volume spike (>2x avg)
      - Candle quality (large body, small wicks)
    """
    score = 0
    if sr_break:
        score += 1
    if trendline_break:
        score += 1
    if structure_confirmed:
        score += 1
    if volume_spike:
        score += 1
    if candle_quality:
        score += 1
    return score


def check_candle_quality(bar: pd.Series) -> bool:
    """
    Check if the bar is a strong impulse candle:
    body > 60% of total range, i.e. small wicks relative to body.
    """
    open_p = bar["Open"]
    close_p = bar["Close"]
    high_p = bar["High"]
    low_p = bar["Low"]

    total_range = high_p - low_p
    if total_range <= 0:
        return False

    body = abs(close_p - open_p)
    return (body / total_range) >= 0.60


def check_volume_spike(df: pd.DataFrame, bar_index: int, threshold: float = 2.0) -> bool:
    """Check if volume at bar_index is >= threshold * 20-bar average."""
    vol_window = 20
    if bar_index < 1:
        return False
    start = max(0, bar_index - vol_window)
    avg_vol = df["Volume"].iloc[start:bar_index].mean()
    if avg_vol <= 0:
        return False
    return float(df["Volume"].iloc[bar_index]) >= threshold * avg_vol


# ---------------------------------------------------------------------------
# Full scan (updated for v2)
# ---------------------------------------------------------------------------

MIN_TRENDLINE_BARS = 6  # 30 minutes on 5-min chart


def scan_for_setups(df: pd.DataFrame, min_touches: int = 3) -> list[dict]:
    """
    Full scan: find all active trendlines + S/R levels, count touches,
    check for breakouts. Returns list of setups sorted by conviction.
    """
    if "ATR" not in df.columns:
        raise ValueError("DataFrame must have ATR column. Compute ATR(14) first.")

    setups = []
    pivot_highs = find_pivot_highs(df)
    pivot_lows = find_pivot_lows(df)

    last_bar = len(df) - 1
    current_atr = df["ATR"].iloc[last_bar]

    # Also find S/R levels
    sr_levels = find_support_resistance(df)

    # --- Descending trendlines (through swing highs) ---
    for end_idx in range(len(pivot_highs) - 1, 0, -1):
        for start_idx in range(end_idx - 1, max(end_idx - 5, -1), -1):
            pair = [pivot_highs[start_idx], pivot_highs[end_idx]]
            tl = fit_trendline(pair)
            if tl is None:
                continue
            if tl["end_bar"] - tl["start_bar"] < MIN_TRENDLINE_BARS:
                continue

            extended_tl = {**tl, "end_bar": last_bar}
            touches = count_touches(df, extended_tl)
            if touches < min_touches:
                continue

            breakout = detect_breakout(df, extended_tl, last_bar, current_atr)
            conviction = _score_setup(touches, breakout, tl)

            setups.append({
                "trendline": extended_tl,
                "touches": touches,
                "breakout": breakout,
                "conviction": conviction,
                "tl_value_now": trendline_value_at(extended_tl, last_bar),
                "current_close": float(df["Close"].iloc[last_bar]),
                "current_atr": float(current_atr),
                "sr_levels": sr_levels,
            })

    # --- Ascending trendlines (through swing lows) ---
    for end_idx in range(len(pivot_lows) - 1, 0, -1):
        for start_idx in range(end_idx - 1, max(end_idx - 5, -1), -1):
            pair = [pivot_lows[start_idx], pivot_lows[end_idx]]
            tl = fit_trendline(pair)
            if tl is None:
                continue
            if tl["end_bar"] - tl["start_bar"] < MIN_TRENDLINE_BARS:
                continue

            extended_tl = {**tl, "end_bar": last_bar}
            touches = count_touches(df, extended_tl)
            if touches < min_touches:
                continue

            breakout = detect_breakout(df, extended_tl, last_bar, current_atr)
            conviction = _score_setup(touches, breakout, tl)

            setups.append({
                "trendline": extended_tl,
                "touches": touches,
                "breakout": breakout,
                "conviction": conviction,
                "tl_value_now": trendline_value_at(extended_tl, last_bar),
                "current_close": float(df["Close"].iloc[last_bar]),
                "current_atr": float(current_atr),
                "sr_levels": sr_levels,
            })

    setups.sort(key=lambda s: s["conviction"], reverse=True)
    setups = _deduplicate(setups, df)
    return setups


def _score_setup(touches: int, breakout: Optional[dict], tl: dict) -> float:
    """Score a setup from 0-100 based on quality factors."""
    score = 0.0
    score += min(touches * 12, 60)
    duration = tl["end_bar"] - tl["start_bar"]
    score += min(duration * 0.5, 20)
    if breakout:
        score += 15
        if breakout["volume_ratio"] > 2.0:
            score += 5
    return min(score, 100)


def _deduplicate(setups: list[dict], df: pd.DataFrame) -> list[dict]:
    """Remove near-duplicate trendlines (similar slope and intercept)."""
    if len(setups) <= 1:
        return setups

    kept = [setups[0]]
    for s in setups[1:]:
        is_dup = False
        for k in kept:
            slope_diff = abs(s["trendline"]["slope"] - k["trendline"]["slope"])
            mid = len(df) // 2
            val_diff = abs(
                trendline_value_at(s["trendline"], mid)
                - trendline_value_at(k["trendline"], mid)
            )
            atr_mid = df["ATR"].iloc[mid] if mid < len(df) else 1
            if slope_diff < 0.05 and val_diff < atr_mid * 0.5:
                is_dup = True
                break
        if not is_dup:
            kept.append(s)
    return kept
