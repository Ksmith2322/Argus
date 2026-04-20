"""
Cue Banks Confluence Detection Engine
=======================================
Core of Cue Banks' strategy — detecting where multiple technical levels overlap.

Per rulebook: the edge comes from OVERLAPPING technical levels. One level is
not enough. Requires 3+ of:
  1. Horizontal S/R (psychological round numbers, role-flip)
  2. Fibonacci retracement (38.2%, 61.8% key levels)
  3. Fibonacci extensions (-27%, -61.8% for targets)
  4. Market structure (HH/HL = bullish, LH/LL = bearish)
  5. Exhaustion patterns (wicks rejecting, higher lows)
  6. Trendline / counter-trend line breaks
  7. Consolidation zone breaks

"Where multiple elements overlap = the trade."
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Fibonacci
# ---------------------------------------------------------------------------

def compute_fib_levels(swing_high: float, swing_low: float) -> dict:
    """
    Compute Fibonacci retracement and extension levels.
    Retracements: 0.236, 0.382, 0.500, 0.618, 0.786
    Extensions: -0.272, -0.618, -1.000
    Returns: {retracements: {level: price}, extensions: {level: price}}
    """
    diff = swing_high - swing_low

    retracements = {
        "0.236": swing_high - diff * 0.236,
        "0.382": swing_high - diff * 0.382,
        "0.500": swing_high - diff * 0.500,
        "0.618": swing_high - diff * 0.618,
        "0.786": swing_high - diff * 0.786,
    }

    extensions = {
        "-0.272": swing_high + diff * 0.272,
        "-0.618": swing_high + diff * 0.618,
        "-1.000": swing_high + diff * 1.000,
    }

    return {"retracements": retracements, "extensions": extensions}


# ---------------------------------------------------------------------------
# Swing points
# ---------------------------------------------------------------------------

def find_swing_points(df: pd.DataFrame, left: int = 10, right: int = 5) -> tuple:
    """
    Find swing highs and lows for Fibonacci drawing.
    A swing high has 'left' lower highs before and 'right' lower highs after.
    A swing low has 'left' higher lows before and 'right' higher lows after.

    Returns: (swing_highs, swing_lows) — each a list of {idx, price, bar_idx}
    """
    highs = df["High"].values
    lows = df["Low"].values
    n = len(df)

    swing_highs = []
    swing_lows = []

    for i in range(left, n - right):
        # Swing high check
        is_sh = True
        for j in range(1, left + 1):
            if highs[i - j] >= highs[i]:
                is_sh = False
                break
        if is_sh:
            for j in range(1, right + 1):
                if highs[i + j] >= highs[i]:
                    is_sh = False
                    break
        if is_sh:
            swing_highs.append({
                "idx": df.index[i],
                "price": float(highs[i]),
                "bar_idx": i,
            })

        # Swing low check
        is_sl = True
        for j in range(1, left + 1):
            if lows[i - j] <= lows[i]:
                is_sl = False
                break
        if is_sl:
            for j in range(1, right + 1):
                if lows[i + j] <= lows[i]:
                    is_sl = False
                    break
        if is_sl:
            swing_lows.append({
                "idx": df.index[i],
                "price": float(lows[i]),
                "bar_idx": i,
            })

    return swing_highs, swing_lows


# ---------------------------------------------------------------------------
# Horizontal S/R
# ---------------------------------------------------------------------------

def find_horizontal_sr(
    df: pd.DataFrame,
    tolerance_pct: float = 0.001,
    min_touches: int = 2,
) -> list:
    """
    Find horizontal S/R levels including:
    - Prior swing highs/lows that were respected
    - Psychological round numbers (nearest 1000, 500, 100)
    - Role-flip levels (was support, now resistance or vice versa)

    Returns: [{level, touches, type, is_round_number, is_role_flip}]
    """
    swing_highs, swing_lows = find_swing_points(df, left=8, right=4)

    # Collect candidate levels
    candidates = []
    for sh in swing_highs:
        candidates.append({"price": sh["price"], "source": "high", "bar_idx": sh["bar_idx"]})
    for sl in swing_lows:
        candidates.append({"price": sl["price"], "source": "low", "bar_idx": sl["bar_idx"]})

    if not candidates:
        return []

    # Cluster nearby levels
    candidates.sort(key=lambda x: x["price"])
    clusters = []
    current_cluster = [candidates[0]]

    for i in range(1, len(candidates)):
        ref_price = current_cluster[0]["price"]
        if abs(candidates[i]["price"] - ref_price) / ref_price < tolerance_pct * 3:
            current_cluster.append(candidates[i])
        else:
            clusters.append(current_cluster)
            current_cluster = [candidates[i]]
    clusters.append(current_cluster)

    # Build S/R levels
    levels = []
    last_price = float(df["Close"].iloc[-1])

    for cluster in clusters:
        avg_price = np.mean([c["price"] for c in cluster])
        touches = len(cluster)
        if touches < min_touches:
            continue

        sources = [c["source"] for c in cluster]
        has_high = "high" in sources
        has_low = "low" in sources

        # Role-flip: was both support and resistance
        is_role_flip = has_high and has_low

        # Type relative to current price
        level_type = "support" if avg_price < last_price else "resistance"

        # Psychological round numbers
        is_round = (avg_price % 1000 < 50 or avg_price % 1000 > 950 or
                    avg_price % 500 < 25 or avg_price % 500 > 475)

        levels.append({
            "level": round(float(avg_price), 2),
            "touches": touches,
            "type": level_type,
            "is_round_number": is_round,
            "is_role_flip": is_role_flip,
        })

    # Add psychological round numbers near current price
    for rnd in [1000, 500, 100]:
        base = round(last_price / rnd) * rnd
        for offset in [-rnd, 0, rnd]:
            lvl = base + offset
            if abs(lvl - last_price) / last_price < 0.03:
                # Check if already covered
                already = any(abs(l["level"] - lvl) / lvl < tolerance_pct for l in levels)
                if not already:
                    levels.append({
                        "level": float(lvl),
                        "touches": 0,
                        "type": "support" if lvl < last_price else "resistance",
                        "is_round_number": True,
                        "is_role_flip": False,
                    })

    levels.sort(key=lambda x: x["level"])
    return levels


# ---------------------------------------------------------------------------
# Exhaustion
# ---------------------------------------------------------------------------

def detect_exhaustion(df: pd.DataFrame, bar_idx: int) -> dict | None:
    """
    Detect exhaustion patterns: long wicks rejecting a level.
    Wick > 2x body = exhaustion signal.
    Returns: {direction, wick_ratio, bar_idx} or None
    """
    if bar_idx < 0 or bar_idx >= len(df):
        return None

    row = df.iloc[bar_idx]
    o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])

    body = abs(c - o)
    if body < 0.01:
        body = 0.01  # avoid division by zero

    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    # Bullish exhaustion: long lower wick (rejection of lows)
    if lower_wick > 2 * body and lower_wick > upper_wick:
        return {
            "direction": "bullish",
            "wick_ratio": round(lower_wick / body, 2),
            "bar_idx": bar_idx,
        }

    # Bearish exhaustion: long upper wick (rejection of highs)
    if upper_wick > 2 * body and upper_wick > lower_wick:
        return {
            "direction": "bearish",
            "wick_ratio": round(upper_wick / body, 2),
            "bar_idx": bar_idx,
        }

    return None


# ---------------------------------------------------------------------------
# Market structure
# ---------------------------------------------------------------------------

def detect_structure(df: pd.DataFrame, lookback: int = 20) -> dict:
    """
    Detect market structure: HH/HL = bullish, LH/LL = bearish.
    Returns: {bias, swings: [{type, price, idx}]}
    """
    if len(df) < lookback:
        return {"bias": "neutral", "swings": []}

    window = df.iloc[-lookback:]
    swing_highs, swing_lows = find_swing_points(window, left=4, right=2)

    swings = []
    for sh in swing_highs:
        swings.append({"type": "high", "price": sh["price"], "idx": sh["idx"]})
    for sl in swing_lows:
        swings.append({"type": "low", "price": sl["price"], "idx": sl["idx"]})

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return {"bias": "neutral", "swings": swings}

    # Check recent swing structure
    recent_highs = swing_highs[-3:]
    recent_lows = swing_lows[-3:]

    hh_count = sum(1 for i in range(1, len(recent_highs))
                   if recent_highs[i]["price"] > recent_highs[i - 1]["price"])
    hl_count = sum(1 for i in range(1, len(recent_lows))
                   if recent_lows[i]["price"] > recent_lows[i - 1]["price"])

    lh_count = sum(1 for i in range(1, len(recent_highs))
                   if recent_highs[i]["price"] < recent_highs[i - 1]["price"])
    ll_count = sum(1 for i in range(1, len(recent_lows))
                   if recent_lows[i]["price"] < recent_lows[i - 1]["price"])

    bull_score = hh_count + hl_count
    bear_score = lh_count + ll_count

    if bull_score >= 2 and bull_score > bear_score:
        bias = "bullish"
    elif bear_score >= 2 and bear_score > bull_score:
        bias = "bearish"
    else:
        bias = "neutral"

    return {"bias": bias, "swings": swings}


# ---------------------------------------------------------------------------
# Consolidation break
# ---------------------------------------------------------------------------

def detect_consolidation_break(
    df: pd.DataFrame, bar_idx: int, lookback: int = 30,
) -> dict | None:
    """
    Detect if price just broke out of a consolidation range.
    Range = price staying within 0.5% band for 10+ bars.
    Break = candle closing outside the range.
    Returns: {direction, range_high, range_low, break_price} or None
    """
    if bar_idx < lookback or bar_idx >= len(df):
        return None

    window = df.iloc[bar_idx - lookback:bar_idx]
    current = df.iloc[bar_idx]

    highs = window["High"].values
    lows = window["Low"].values

    # Find longest consolidation range ending near current bar
    best_range = None
    best_count = 0

    for start in range(len(window) - 10):
        range_high = float(highs[start])
        range_low = float(lows[start])
        count = 1

        for j in range(start + 1, len(window)):
            if float(highs[j]) > range_high:
                range_high = float(highs[j])
            if float(lows[j]) < range_low:
                range_low = float(lows[j])

            band_pct = (range_high - range_low) / range_low if range_low > 0 else 0
            if band_pct > 0.005:  # 0.5% band exceeded
                break
            count += 1

        if count >= 10 and count > best_count:
            best_count = count
            best_range = (range_high, range_low)

    if best_range is None:
        return None

    range_high, range_low = best_range
    close = float(current["Close"])

    if close > range_high:
        return {
            "direction": "bullish",
            "range_high": round(range_high, 2),
            "range_low": round(range_low, 2),
            "break_price": round(close, 2),
        }
    elif close < range_low:
        return {
            "direction": "bearish",
            "range_high": round(range_high, 2),
            "range_low": round(range_low, 2),
            "break_price": round(close, 2),
        }

    return None


# ---------------------------------------------------------------------------
# Supply/Demand zones (2026-04-19 — Confluence 1.0 transcript rule)
# ---------------------------------------------------------------------------

def find_supply_demand_zones(
    df: pd.DataFrame,
    lookback_bars: int = 60,
    min_breakout_pct: float = 0.004,
) -> list[dict]:
    """Detect supply/demand zones per Cue Banks' Confluence 1.0 rule:
    the zone is drawn from the PREVIOUS candle's wick to the breakout
    candle's body.

    A demand zone (support) forms when:
      1. Consolidation or pullback candle prints (candle_a)
      2. Next candle breaks upward strongly (candle_b), with its close
         >= (1 + min_breakout_pct) × candle_a.close
      3. Zone = [candle_a.low (wick), candle_b.open (body start)]

    Supply zone (resistance) is the mirror: strong down candle after a
    pullback, zone = [candle_b.open, candle_a.high].

    Returns a list of zone dicts with price range, bar index, and type.
    """
    zones = []
    if len(df) < 3:
        return zones

    start = max(0, len(df) - lookback_bars)
    for i in range(start + 1, len(df)):
        candle_a = df.iloc[i - 1]
        candle_b = df.iloc[i]

        # Demand: candle_b breaks UP strongly from candle_a
        if float(candle_b["Close"]) >= float(candle_a["Close"]) * (1 + min_breakout_pct):
            zone_bottom = float(candle_a["Low"])  # wick
            zone_top = float(candle_b["Open"])    # breakout body start
            if zone_bottom < zone_top:
                zones.append({
                    "type": "demand",
                    "zone_bottom": round(zone_bottom, 2),
                    "zone_top": round(zone_top, 2),
                    "breakout_bar": i,
                })

        # Supply: candle_b breaks DOWN strongly
        elif float(candle_b["Close"]) <= float(candle_a["Close"]) * (1 - min_breakout_pct):
            zone_bottom = float(candle_b["Open"])  # breakout body start
            zone_top = float(candle_a["High"])     # wick
            if zone_bottom < zone_top:
                zones.append({
                    "type": "supply",
                    "zone_bottom": round(zone_bottom, 2),
                    "zone_top": round(zone_top, 2),
                    "breakout_bar": i,
                })

    return zones


def price_in_zone(price: float, zone: dict, tolerance_pct: float = 0.001) -> bool:
    """True if `price` is inside the zone (optionally expanded by tolerance)."""
    pad = zone["zone_top"] * tolerance_pct
    return (zone["zone_bottom"] - pad) <= price <= (zone["zone_top"] + pad)


# ---------------------------------------------------------------------------
# Harmonic patterns (2026-04-19 — bullish bat + bearish bat)
# ---------------------------------------------------------------------------

def _swing_idx(s: dict) -> int:
    """Swing-point index accessor that handles both {"index": ...} (helio/tori)
    and {"bar_idx": ...} (cuebanks) conventions."""
    if "index" in s:
        return int(s["index"])
    if "bar_idx" in s:
        return int(s["bar_idx"])
    raise KeyError("swing dict must carry 'index' or 'bar_idx'")


def detect_bullish_bat(
    swing_highs: list[dict],
    swing_lows: list[dict],
    current_price: float,
    tolerance_pct: float = 0.015,
) -> dict | None:
    """Bullish Bat harmonic pattern per Cue Banks Confluence 1.0.

    Points (X low, A high, B low, C high, D low):
      X→A: initial up leg
      A→B: retrace 50% of X→A (B is above X)
      B→C: retrace 78.6% of A→B (C is below A, above B)
      C→D: target 88.6% retracement of X→A (D is entry, below X)

    Valid when the latest swing structure matches and current price is
    near D. Returns pattern dict or None.
    """
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return None
    highs_sorted = sorted(swing_highs, key=_swing_idx)
    lows_sorted = sorted(swing_lows, key=_swing_idx)
    if len(highs_sorted) < 2 or len(lows_sorted) < 2:
        return None

    try:
        x, a, b, c = lows_sorted[-2], highs_sorted[-2], lows_sorted[-1], highs_sorted[-1]
    except IndexError:
        return None

    if not (_swing_idx(x) < _swing_idx(a) < _swing_idx(b) < _swing_idx(c)):
        return None

    xa_leg = a["price"] - x["price"]
    if xa_leg <= 0:
        return None

    ab_retrace = (a["price"] - b["price"]) / xa_leg
    if not (0.5 - tolerance_pct <= ab_retrace <= 0.5 + tolerance_pct):
        return None

    ab_leg = a["price"] - b["price"]
    if ab_leg <= 0:
        return None
    bc_retrace = (c["price"] - b["price"]) / ab_leg
    if not (0.786 - tolerance_pct <= bc_retrace <= 0.786 + tolerance_pct):
        return None

    d_target = a["price"] - xa_leg * 0.886
    d_tolerance = xa_leg * tolerance_pct

    if abs(current_price - d_target) > d_tolerance:
        return None

    return {
        "pattern": "bullish_bat",
        "direction": "LONG",
        "x": x["price"], "a": a["price"], "b": b["price"],
        "c": c["price"], "d_entry": round(d_target, 2),
        "stop_below": round(x["price"] - xa_leg * 0.03, 2),
        "target_1": round(d_target + xa_leg * 0.382, 2),
        "target_2": round(d_target + xa_leg * 0.618, 2),
    }


def detect_bearish_bat(
    swing_highs: list[dict],
    swing_lows: list[dict],
    current_price: float,
    tolerance_pct: float = 0.015,
) -> dict | None:
    """Bearish Bat — mirror of bullish. X high, A low, B high, C low, D high.
    Entry at D above X, expecting reversion down."""
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return None
    highs_sorted = sorted(swing_highs, key=_swing_idx)
    lows_sorted = sorted(swing_lows, key=_swing_idx)
    if len(highs_sorted) < 2 or len(lows_sorted) < 2:
        return None

    try:
        x, a, b, c = highs_sorted[-2], lows_sorted[-2], highs_sorted[-1], lows_sorted[-1]
    except IndexError:
        return None
    if not (_swing_idx(x) < _swing_idx(a) < _swing_idx(b) < _swing_idx(c)):
        return None

    xa_leg = x["price"] - a["price"]
    if xa_leg <= 0:
        return None

    ab_retrace = (b["price"] - a["price"]) / xa_leg
    if not (0.5 - tolerance_pct <= ab_retrace <= 0.5 + tolerance_pct):
        return None

    ab_leg = b["price"] - a["price"]
    if ab_leg <= 0:
        return None
    bc_retrace = (b["price"] - c["price"]) / ab_leg
    if not (0.786 - tolerance_pct <= bc_retrace <= 0.786 + tolerance_pct):
        return None

    d_target = a["price"] + xa_leg * 0.886
    d_tolerance = xa_leg * tolerance_pct

    if abs(current_price - d_target) > d_tolerance:
        return None

    return {
        "pattern": "bearish_bat",
        "direction": "SHORT",
        "x": x["price"], "a": a["price"], "b": b["price"],
        "c": c["price"], "d_entry": round(d_target, 2),
        "stop_above": round(x["price"] + xa_leg * 0.03, 2),
        "target_1": round(d_target - xa_leg * 0.382, 2),
        "target_2": round(d_target - xa_leg * 0.618, 2),
    }


# ---------------------------------------------------------------------------
# Gap detection
# ---------------------------------------------------------------------------

def detect_gap(df: pd.DataFrame, min_gap_pct: float = 0.3) -> dict | None:
    """
    Detect opening session gaps (prior close vs current open).
    Returns: {direction, gap_size_pct, gap_open, gap_close, filled} or None
    """
    if len(df) < 2:
        return None

    prior_close = float(df["Close"].iloc[-2])
    current_open = float(df["Open"].iloc[-1])
    current_close = float(df["Close"].iloc[-1])
    current_low = float(df["Low"].iloc[-1])
    current_high = float(df["High"].iloc[-1])

    gap_pct = abs(current_open - prior_close) / prior_close * 100

    if gap_pct < min_gap_pct:
        return None

    if current_open > prior_close:
        direction = "gap_up"
        # Filled if price came back down to prior close
        filled = current_low <= prior_close
    else:
        direction = "gap_down"
        filled = current_high >= prior_close

    return {
        "direction": direction,
        "gap_size_pct": round(gap_pct, 3),
        "gap_open": round(current_open, 2),
        "gap_close": round(prior_close, 2),
        "filled": filled,
    }


def detect_gap_at_session_open(
    df: pd.DataFrame, date: str, min_gap_pct: float = 0.3,
) -> dict | None:
    """
    Detect opening gap for a specific trading day.
    Compares prior day's close to current day's open.
    """
    # Get bars for this date
    day_bars = df[df.index.strftime("%Y-%m-%d") == date]
    if day_bars.empty:
        return None

    current_open = float(day_bars["Open"].iloc[0])

    # Get prior trading day close
    prior_bars = df[df.index.strftime("%Y-%m-%d") < date]
    if prior_bars.empty:
        return None

    prior_close = float(prior_bars["Close"].iloc[-1])

    gap_pct = abs(current_open - prior_close) / prior_close * 100
    if gap_pct < min_gap_pct:
        return None

    # Check if gap was filled during the day
    if current_open > prior_close:
        direction = "gap_up"
        filled = float(day_bars["Low"].min()) <= prior_close
    else:
        direction = "gap_down"
        filled = float(day_bars["High"].max()) >= prior_close

    return {
        "direction": direction,
        "gap_size_pct": round(gap_pct, 3),
        "gap_open": round(current_open, 2),
        "gap_close": round(prior_close, 2),
        "filled": filled,
    }


# ---------------------------------------------------------------------------
# Confluence scorer
# ---------------------------------------------------------------------------

def score_confluence(
    price: float,
    sr_levels: list,
    fib_levels: dict,
    structure: dict,
    exhaustion: dict | None,
    trendline_break: bool,
    consolidation_break: dict | None,
    gap: dict | None,
    tolerance_pct: float = 0.002,
    sd_zones: list | None = None,
    harmonic: dict | None = None,
) -> dict:
    """
    Score how many confluence factors align at the current price.

    Added 2026-04-19 audit:
      - sd_zones: list of supply/demand zones from find_supply_demand_zones
      - harmonic: dict from detect_bullish_bat or detect_bearish_bat (or None)

    Returns:
        score, factors, tradeable, sniper, direction, nearest_sr,
        fib_level, entry_price, stop_price, tp1, tp2, tp3
    """
    score = 0
    factors = []
    direction = None
    nearest_sr = None
    nearest_sr_dist = float("inf")
    matched_fib = None
    matched_sd_zone = None
    harmonic_pattern = None

    # 1. Horizontal S/R — within tolerance
    for sr in sr_levels:
        dist = abs(price - sr["level"]) / price
        if dist < tolerance_pct:
            base_score = 1
            if sr.get("is_round_number"):
                base_score += 0.5
            if sr.get("is_role_flip"):
                base_score += 0.5
            score += base_score
            factors.append(f"S/R {sr['type']} @ {sr['level']:.0f}"
                           + (" [round]" if sr.get("is_round_number") else "")
                           + (" [flip]" if sr.get("is_role_flip") else ""))
        if dist < nearest_sr_dist:
            nearest_sr_dist = dist
            nearest_sr = sr["level"]

    # 2. Fibonacci retracement
    if fib_levels and "retracements" in fib_levels:
        for level_name, level_price in fib_levels["retracements"].items():
            if abs(price - level_price) / price < tolerance_pct:
                score += 1
                matched_fib = level_name
                factors.append(f"Fib {level_name} @ {level_price:.0f}")
                break  # only count once

    # 3. Market structure
    bias = structure.get("bias", "neutral")
    if bias in ("bullish", "bearish"):
        score += 1
        factors.append(f"Structure {bias}")
        direction = "LONG" if bias == "bullish" else "SHORT"

    # 4. Exhaustion
    if exhaustion:
        score += 1
        factors.append(f"Exhaustion {exhaustion['direction']} (wick {exhaustion['wick_ratio']}x)")
        if direction is None:
            direction = "LONG" if exhaustion["direction"] == "bullish" else "SHORT"

    # 5. Trendline break
    if trendline_break:
        score += 1
        factors.append("Trendline break")

    # 6. Consolidation break
    if consolidation_break:
        score += 1
        factors.append(f"Consolidation break {consolidation_break['direction']}")
        if direction is None:
            direction = "LONG" if consolidation_break["direction"] == "bullish" else "SHORT"

    # 7. Gap
    if gap:
        score += 1
        factors.append(f"Gap {gap['direction']} ({gap['gap_size_pct']:.2f}%)")

    # 8. Supply/demand zone (2026-04-19 addition)
    if sd_zones:
        for z in sd_zones:
            if price_in_zone(price, z, tolerance_pct=tolerance_pct):
                score += 1
                matched_sd_zone = z
                factors.append(f"S/D {z['type']} zone [{z['zone_bottom']:.0f}-{z['zone_top']:.0f}]")
                if direction is None:
                    direction = "LONG" if z["type"] == "demand" else "SHORT"
                break

    # 9. Harmonic pattern (2026-04-19 addition)
    if harmonic:
        score += 1.5  # Harmonics are high-conviction per the transcript
        harmonic_pattern = harmonic["pattern"]
        factors.append(f"Harmonic {harmonic_pattern}")
        if direction is None:
            direction = harmonic["direction"]
        elif direction != harmonic["direction"]:
            # Conflict: harmonic disagrees with other signals — deprioritize
            score -= 0.5
            factors.append("!direction conflict with harmonic")

    # Determine direction from S/R type if still None
    if direction is None:
        for sr in sr_levels:
            if abs(price - sr["level"]) / price < tolerance_pct:
                direction = "LONG" if sr["type"] == "support" else "SHORT"
                break

    # Compute entry/stop/targets
    tradeable = int(score) >= 3
    sniper = int(score) >= 4

    entry_price = round(price, 2)
    # Stop: just outside nearest S/R or swing (tight, M5-based)
    atr_proxy = price * 0.001  # ~0.1% as proxy when no ATR available
    if direction == "LONG":
        stop_price = round((nearest_sr if nearest_sr and nearest_sr < price else price) - atr_proxy * 2, 2)
    elif direction == "SHORT":
        stop_price = round((nearest_sr if nearest_sr and nearest_sr > price else price) + atr_proxy * 2, 2)
    else:
        stop_price = round(price - atr_proxy * 2, 2)

    stop_dist = abs(entry_price - stop_price)

    # Targets using Fib extensions / measured move
    if direction == "LONG":
        tp1 = round(entry_price + stop_dist * 3.82, 2)   # Fib 38.2% move
        tp2 = round(entry_price + stop_dist * 6.18, 2)   # Fib 61.8% move
        tp3 = round(entry_price + stop_dist * 7.27, 2)   # -27% extension (1:7+ R:R)
    elif direction == "SHORT":
        tp1 = round(entry_price - stop_dist * 3.82, 2)
        tp2 = round(entry_price - stop_dist * 6.18, 2)
        tp3 = round(entry_price - stop_dist * 7.27, 2)
    else:
        tp1 = tp2 = tp3 = entry_price

    return {
        "score": round(score, 1),
        "factors": factors,
        "tradeable": tradeable,
        "sniper": sniper,
        "direction": direction,
        "nearest_sr": nearest_sr,
        "fib_level": matched_fib,
        "sd_zone": matched_sd_zone,
        "harmonic_pattern": harmonic_pattern,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
    }



# ---------------------------------------------------------------------------
# Break + retest stateful tracker (2026-04-19)
# ---------------------------------------------------------------------------

class BreakTracker:
    """Faithful implementation of Cue Banks' "no retest, no entry" rule.

    The naive check ("price was on the other side at any point in the last N
    bars") lost the A/B test because in volatile markets it's trivially
    satisfied. This tracker implements what Cue Banks actually teaches:

      1. A level is considered *broken* when a candle's body (not just wick)
         closes past the level by more than a configurable tolerance.
      2. After a break, the level is "armed for retest" — we watch for price
         to return and touch that exact level from the new side.
      3. The retest is valid for `retest_window_bars` after the break. Stale
         breaks reset.
      4. `is_fresh_retest` returns True only when the current bar touched a
         broken level within the last `recency_bars` — that's the narrow
         window Cue Banks teaches.

    Usage per session/day:
        tracker = BreakTracker()
        tracker.register_levels(sr_levels)
        for bar_idx in range(len(df)):
            bar = df.iloc[bar_idx]
            tracker.update(bar_idx, bar)
            # later, when a confluence signal fires:
            matched = tracker.is_fresh_retest(bar_idx, direction, tolerance_pct)
            if matched is None:
                continue  # no retest gate pass
    """

    def __init__(
        self,
        tolerance_pct: float = 0.002,
        retest_window_bars: int = 30,
        recency_bars: int = 3,
    ):
        self.levels: dict[float, dict] = {}
        self.tolerance_pct = tolerance_pct
        self.retest_window_bars = retest_window_bars
        self.recency_bars = recency_bars

    def register_levels(self, sr_levels: list[dict]) -> None:
        """Call once with the session's active S/R levels. Levels pre-existing
        in the tracker are preserved (their break state isn't reset)."""
        for sr in sr_levels:
            price = float(sr["level"])
            if price not in self.levels:
                self.levels[price] = {
                    "type": sr.get("type"),
                    "status": "armed",  # armed | broken | stale
                    "broken_at_bar": None,
                    "broken_direction": None,  # 'up' or 'down'
                    "retest_touched_at": None,
                    "prev_close": None,  # for cross-detection on first update
                }

    def update(self, bar_idx: int, bar: "pd.Series") -> None:
        """Advance tracker state one bar. Detects new breaks and retest touches.

        A break fires once per level: a candle closing past the level by more
        than tolerance flips state from 'armed' to 'broken'. After the retest
        window elapses without a touch, state permanently transitions to
        'stale' (the level is not re-armed) — this prevents the thrashing
        seen when price floats above a former-resistance line for many bars.
        """
        bar_close = float(bar["Close"])
        bar_high = float(bar["High"])
        bar_low = float(bar["Low"])

        for level_price, state in self.levels.items():
            tol = level_price * self.tolerance_pct
            prev_close = state["prev_close"]

            if state["status"] == "armed":
                # Break requires a CROSS: prev_close was on one side, current
                # close past tolerance on the other side. Without prev_close
                # we can't tell whether this is a cross or a continuation, so
                # the first bar only records position — no break registered.
                if prev_close is not None:
                    if bar_close > level_price + tol and prev_close <= level_price + tol:
                        state["status"] = "broken"
                        state["broken_at_bar"] = bar_idx
                        state["broken_direction"] = "up"
                    elif bar_close < level_price - tol and prev_close >= level_price - tol:
                        state["status"] = "broken"
                        state["broken_at_bar"] = bar_idx
                        state["broken_direction"] = "down"

            elif state["status"] == "broken":
                age = bar_idx - state["broken_at_bar"]
                if age > self.retest_window_bars:
                    # Stale — do not re-arm; the level's time has passed
                    state["status"] = "stale"
                    continue

                if state["retest_touched_at"] is None:
                    if state["broken_direction"] == "up":
                        # Retest = low reaches back to the level from above
                        if level_price - tol <= bar_low <= level_price + tol:
                            state["retest_touched_at"] = bar_idx
                    else:
                        # Retest = high reaches back to the level from below
                        if level_price - tol <= bar_high <= level_price + tol:
                            state["retest_touched_at"] = bar_idx
            # stale: no further updates

            # Record close for next-bar cross detection (used only by 'armed' branch)
            state["prev_close"] = bar_close

    def is_fresh_retest(
        self,
        bar_idx: int,
        direction: str,
        tolerance_pct: float | None = None,
    ) -> float | None:
        """Returns the matched level price if the current bar is at a fresh
        retest touch in the given direction, else None.

        Direction mapping:
          - 'LONG'  needs a level broken upward (old resistance → new support)
          - 'SHORT' needs a level broken downward (old support → new resistance)
        """
        for level_price, state in self.levels.items():
            if state["retest_touched_at"] is None:
                continue
            age = bar_idx - state["retest_touched_at"]
            if age < 0 or age > self.recency_bars:
                continue
            if direction == "LONG" and state["broken_direction"] == "up":
                return level_price
            if direction == "SHORT" and state["broken_direction"] == "down":
                return level_price
        return None
