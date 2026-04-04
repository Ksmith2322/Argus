"""Advanced Features — multi-timeframe analysis, spread gating, session scoring.

Provides higher-timeframe trend confirmation, spread quality checks,
adaptive session scoring, and news event filtering. Used by runner_unified.py
as an optional feature layer on top of the base trigger logic.

Multi-Timeframe Bars:
  Aggregates 1-minute bars into 5m, 15m, 30m, 1h bars and computes
  trend direction on each. Entry requires alignment across timeframes.

Spread Gate:
  Blocks entry when bid-ask spread exceeds recent average spread.

Adaptive Session:
  Scores current hour based on historical win rate from QA learning data.

News Filter:
  Blocks entry around high-impact economic events (loaded from schedule file).
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd


# ═══════════════════════════════════════════════════════════════
# Multi-Timeframe Bar Aggregator
# ═══════════════════════════════════════════════════════════════

class MultiTimeframeBuffer:
    """Aggregates 1-minute bars into higher timeframes and computes trend signals."""

    def __init__(self):
        self.bars_5m: list[dict] = []
        self.bars_15m: list[dict] = []
        self.bars_30m: list[dict] = []
        self.bars_1h: list[dict] = []
        self.bars_4h: list[dict] = []

        self._current_5m: dict = {}
        self._current_15m: dict = {}
        self._current_30m: dict = {}
        self._current_1h: dict = {}
        self._current_4h: dict = {}

        self._maxlen = 200  # keep last 200 bars per timeframe

    def on_bar_close(self, bar: dict) -> None:
        """Called when a 1-minute bar closes. Aggregates into higher timeframes."""
        ts_str = bar.get("ts", "")
        try:
            # Parse minute from timestamp
            if isinstance(ts_str, str) and len(ts_str) >= 16:
                minute = int(ts_str[14:16]) if ts_str[14:16].isdigit() else 0
                hour_min = int(ts_str[11:13]) * 60 + minute
            else:
                minute = 0
                hour_min = 0
        except (ValueError, IndexError):
            minute = 0
            hour_min = 0

        o, h, l, c = bar.get("open", 0), bar.get("high", 0), bar.get("low", 0), bar.get("close", 0)
        vol = bar.get("volume", 0)

        # Aggregate into each timeframe
        self._aggregate(self._current_5m, self.bars_5m, o, h, l, c, vol, ts_str, minute % 5 == 0)
        self._aggregate(self._current_15m, self.bars_15m, o, h, l, c, vol, ts_str, minute % 15 == 0)
        self._aggregate(self._current_30m, self.bars_30m, o, h, l, c, vol, ts_str, minute % 30 == 0)
        self._aggregate(self._current_1h, self.bars_1h, o, h, l, c, vol, ts_str, minute == 0)
        # 4hr: boundary at hours 0, 4, 8, 12, 16, 20
        try:
            hour = int(ts_str[11:13]) if isinstance(ts_str, str) and len(ts_str) >= 13 else 0
        except (ValueError, IndexError):
            hour = 0
        is_4h_boundary = (hour % 4 == 0) and minute == 0
        self._aggregate(self._current_4h, self.bars_4h, o, h, l, c, vol, ts_str, is_4h_boundary)

    def _aggregate(self, current: dict, completed: list, o, h, l, c, vol, ts, is_boundary: bool):
        if is_boundary and current:
            # Close current bar
            current["close"] = c
            completed.append(dict(current))
            if len(completed) > self._maxlen:
                completed.pop(0)
            current.clear()

        if not current:
            # Start new bar
            current["ts"] = ts
            current["open"] = o
            current["high"] = h
            current["low"] = l
            current["close"] = c
            current["volume"] = vol
        else:
            # Update current bar
            current["high"] = max(current["high"], h)
            current["low"] = min(current["low"], l)
            current["close"] = c
            current["volume"] = current.get("volume", 0) + vol

    def get_trend(self, timeframe: str = "15m", lookback: int = 20) -> dict:
        """Compute trend direction for a given timeframe.

        Returns: {direction: 'up'|'down'|'neutral', strength: 0-1, ma_fast, ma_slow}
        """
        bars = {
            "5m": self.bars_5m,
            "15m": self.bars_15m,
            "30m": self.bars_30m,
            "1h": self.bars_1h,
            "4h": self.bars_4h,
        }.get(timeframe, self.bars_15m)

        if len(bars) < lookback:
            return {"direction": "neutral", "strength": 0, "ma_fast": 0, "ma_slow": 0, "bars": len(bars)}

        closes = [b["close"] for b in bars[-lookback:]]
        fast_period = min(8, lookback // 2)
        slow_period = lookback

        ma_fast = statistics.mean(closes[-fast_period:])
        ma_slow = statistics.mean(closes[-slow_period:])

        if ma_slow == 0:
            return {"direction": "neutral", "strength": 0, "ma_fast": ma_fast, "ma_slow": ma_slow, "bars": len(bars)}

        spread = (ma_fast - ma_slow) / ma_slow
        threshold = 0.0002  # 2 bps minimum for trend

        if spread > threshold:
            direction = "up"
        elif spread < -threshold:
            direction = "down"
        else:
            direction = "neutral"

        strength = min(1.0, abs(spread) / 0.002)  # normalize to 0-1

        return {
            "direction": direction,
            "strength": round(strength, 3),
            "ma_fast": round(ma_fast, 6),
            "ma_slow": round(ma_slow, 6),
            "spread": round(spread, 6),
            "bars": len(bars),
        }

    def get_4h_bias(self) -> dict:
        """Get 4hr directional bias from last completed candle.

        Uses single-bar close-vs-open rather than MA crossover,
        since 4hr bars are sparse (only ~30 from 5-day seed).
        """
        if not self.bars_4h:
            return {"direction": "neutral", "strength": 0, "bars": 0}
        last = self.bars_4h[-1]
        o, c = last.get("open", 0), last.get("close", 0)
        h, l = last.get("high", 0), last.get("low", 0)
        if o == 0 or c == 0 or h == l:
            return {"direction": "neutral", "strength": 0, "bars": len(self.bars_4h)}
        body = abs(c - o)
        full_range = h - l
        body_ratio = body / full_range if full_range > 0 else 0
        direction = "up" if c > o else "down" if c < o else "neutral"
        # Strength = how decisive the candle was (body / range)
        strength = round(min(1.0, body_ratio), 3)
        return {
            "direction": direction,
            "strength": strength,
            "body_ratio": round(body_ratio, 3),
            "bars": len(self.bars_4h),
        }

    def get_multi_trend(self) -> dict:
        """Get trend across all timeframes. Returns alignment score."""
        trends = {}
        for tf in ("5m", "15m", "30m", "1h"):
            trends[tf] = self.get_trend(tf)
        trends["4h"] = self.get_4h_bias()

        # Alignment: how many timeframes agree on direction?
        directions = [t["direction"] for t in trends.values() if t["direction"] != "neutral"]
        if not directions:
            alignment = "neutral"
            alignment_score = 0.0
        else:
            up_count = sum(1 for d in directions if d == "up")
            down_count = sum(1 for d in directions if d == "down")
            if up_count > down_count:
                alignment = "up"
                alignment_score = up_count / len(directions)
            elif down_count > up_count:
                alignment = "down"
                alignment_score = down_count / len(directions)
            else:
                alignment = "mixed"
                alignment_score = 0.0

        return {
            "timeframes": trends,
            "alignment": alignment,
            "alignment_score": round(alignment_score, 2),
            "aligned_count": max(
                sum(1 for d in directions if d == "up"),
                sum(1 for d in directions if d == "down"),
            ) if directions else 0,
            "total_active": len(directions),
        }

    def check_entry_alignment(self, direction: str) -> tuple[bool, str]:
        """Check if a proposed entry direction aligns with higher timeframes.

        Returns (allowed, reason).
        Rules:
          - Block if both 15m AND 30m oppose entry direction
          - Block if 1h opposes and neither 15m/30m confirms
          - 4hr bias is advisory: logged but does not hard-block
            (too few bars for conviction, but weights alignment score)
        """
        multi = self.get_multi_trend()
        trend_15m = multi["timeframes"].get("15m", {}).get("direction", "neutral")
        trend_30m = multi["timeframes"].get("30m", {}).get("direction", "neutral")
        trend_1h = multi["timeframes"].get("1h", {}).get("direction", "neutral")
        bias_4h = multi["timeframes"].get("4h", {}).get("direction", "neutral")

        target = direction.lower()  # "long" -> check for "up"
        trend_match = "up" if target == "long" else "down"

        opposes_15m = trend_15m != "neutral" and trend_15m != trend_match
        opposes_30m = trend_30m != "neutral" and trend_30m != trend_match
        opposes_1h = trend_1h != "neutral" and trend_1h != trend_match

        # Block if both 15m AND 30m oppose
        if opposes_15m and opposes_30m:
            return False, f"MTF_BLOCK: 15m={trend_15m} 30m={trend_30m} vs {target} (4h={bias_4h})"

        # Block if 1h strongly opposes and neither 15m/30m confirms
        if opposes_1h and not (trend_15m == trend_match or trend_30m == trend_match):
            return False, f"MTF_BLOCK: 1h={trend_1h} opposes {target}, no lower confirm (4h={bias_4h})"

        return True, f"MTF_OK: 15m={trend_15m} 30m={trend_30m} 1h={trend_1h} 4h={bias_4h} align={multi['alignment_score']}"


# ═══════════════════════════════════════════════════════════════
# Spread Gate
# ═══════════════════════════════════════════════════════════════

class SpreadTracker:
    """Track rolling spread and gate entries on spread quality."""

    def __init__(self, window: int = 60):
        self._spreads: list[float] = []
        self._window = window

    def update(self, bid: float, ask: float) -> None:
        if bid > 0 and ask > 0 and ask > bid:
            spread = ask - bid
            self._spreads.append(spread)
            if len(self._spreads) > self._window:
                self._spreads.pop(0)

    @property
    def current_spread(self) -> float:
        return self._spreads[-1] if self._spreads else 0.0

    @property
    def avg_spread(self) -> float:
        return statistics.mean(self._spreads) if self._spreads else 0.0

    @property
    def spread_ratio(self) -> float:
        """Current spread as multiple of average. <1 = tight, >1 = wide."""
        avg = self.avg_spread
        if avg <= 0:
            return 1.0
        return self.current_spread / avg

    def check_entry(self, max_spread_ratio: float = 1.5, min_samples: int = 20) -> tuple[bool, str]:
        """Allow entry only when spread is within acceptable range.

        Args:
            max_spread_ratio: max multiple of avg spread (e.g., 1.5 = 50% above average)
            min_samples: minimum spread observations before gating kicks in
        """
        if len(self._spreads) < min_samples:
            return True, "SPREAD_OK: insufficient data, pass-through"

        ratio = self.spread_ratio
        if ratio <= max_spread_ratio:
            return True, f"SPREAD_OK: ratio={ratio:.2f} (avg={self.avg_spread:.6f})"
        return False, f"SPREAD_WIDE: ratio={ratio:.2f} > {max_spread_ratio} (current={self.current_spread:.6f} avg={self.avg_spread:.6f})"

    def to_dict(self) -> dict:
        return {
            "current": round(self.current_spread, 7),
            "avg": round(self.avg_spread, 7),
            "ratio": round(self.spread_ratio, 3),
            "samples": len(self._spreads),
        }


# ═══════════════════════════════════════════════════════════════
# Adaptive Session Scoring
# ═══════════════════════════════════════════════════════════════

class SessionScorer:
    """Score current hour based on historical win rate data."""

    def __init__(self, symbol: str, base_path: Path | None = None):
        self.symbol = symbol.upper()
        self._heatmap: dict[int, dict] = {}  # hour -> {trades, win_rate, avg_pnl}
        self._base_path = base_path or Path("C:/Argus/repo/argus_flow/logs")
        self._last_load = 0.0

    def refresh(self) -> None:
        """Load session heatmap from QA learning report."""
        import time
        if time.time() - self._last_load < 300:  # refresh every 5 min
            return
        self._last_load = time.time()

        report_path = self._base_path / "qa_learning_report.json"
        if not report_path.exists():
            return
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
            instrument = data.get("instruments", {}).get(self.symbol, {})
            heatmap = instrument.get("session_heatmap", {})
            self._heatmap = {int(k): v for k, v in heatmap.items()}
        except Exception:
            pass

    def score_hour(self, hour: int) -> dict:
        """Score current hour. Returns {score: 0-1, trades, win_rate, label}."""
        self.refresh()
        data = self._heatmap.get(hour, {})
        trades = data.get("trades", 0)
        wr = data.get("win_rate", 0.5)
        avg_pnl = data.get("avg_pnl", 0)

        if trades < 3:
            return {"score": 0.5, "label": "UNKNOWN", "trades": trades, "win_rate": wr}

        # Score based on win rate and sample size
        # Higher trades = more confidence in the win rate
        confidence = min(1.0, trades / 20)
        raw_score = wr * confidence + 0.5 * (1 - confidence)  # regress toward 0.5

        if raw_score >= 0.6:
            label = "HOT"
        elif raw_score >= 0.45:
            label = "WARM"
        else:
            label = "COLD"

        return {
            "score": round(raw_score, 3),
            "label": label,
            "trades": trades,
            "win_rate": round(wr, 3),
            "avg_pnl": round(avg_pnl, 2),
            "confidence": round(confidence, 2),
        }

    def check_entry(self, hour: int, min_score: float = 0.0) -> tuple[bool, str]:
        """Gate entry based on session score. Set min_score=0 to log-only."""
        s = self.score_hour(hour)
        if min_score > 0 and s["score"] < min_score:
            return False, f"SESSION_COLD: hour={hour} score={s['score']:.2f} < {min_score} ({s['label']})"
        return True, f"SESSION_{s['label']}: hour={hour} score={s['score']:.2f} wr={s['win_rate']:.0%} ({s['trades']}T)"


# ═══════════════════════════════════════════════════════════════
# News / Economic Calendar Filter
# ═══════════════════════════════════════════════════════════════

REPO = Path(__file__).resolve().parents[1]
NEWS_SCHEDULE_PATH = REPO / "data" / "economic_calendar.json"

# Built-in high-impact events (UTC hours, recurring weekly)
# NOTE: Only truly recurring events belong here.  ECB/FOMC/NFP are NOT every
# week — use data/economic_calendar.json for actual dates.  The old recurring
# table was blocking EUR/USD on 4 out of 5 weekdays, killing signal frequency.
HIGH_IMPACT_RECURRING: dict[int, list[tuple[int, int, str]]] = {
    0: [],  # Monday
    1: [],  # Tuesday  — US data varies; use calendar file
    2: [],  # Wednesday — FOMC ~8x/year; use calendar file
    3: [],  # Thursday  — ECB ~8x/year; use calendar file
    4: [],  # Friday    — NFP first Friday only; use calendar file
}

# Currencies affected by each event type
EVENT_CURRENCY_MAP = {
    "FOMC_Window": {"USD"},
    "NFP_Window": {"USD"},
    "ECB_Window": {"EUR"},
    "US_Data_Tue": {"USD"},
    "US_Data_Wed": {"USD"},
    "US_Data_Thu": {"USD"},
    "US_Data_Fri": {"USD"},
}


def check_news_filter(
    symbol: str,
    now: datetime,
    buffer_minutes: int = 30,
) -> tuple[bool, str]:
    """Check if current time is near a high-impact economic event.

    Returns (allowed, reason). Blocks entry within buffer_minutes of events
    that affect currencies in the symbol.
    """
    symbol = symbol.upper()
    # Extract currencies from symbol (e.g., EURUSD -> EUR, USD)
    currencies = set()
    if len(symbol) >= 6:
        currencies.add(symbol[:3])
        currencies.add(symbol[3:6])
    elif len(symbol) >= 3:
        currencies.add(symbol[:3])

    dow = now.weekday()
    current_minutes = now.hour * 60 + now.minute

    events_today = HIGH_IMPACT_RECURRING.get(dow, [])
    for event_hour, duration, label in events_today:
        event_start = event_hour * 60 - buffer_minutes
        event_end = event_hour * 60 + duration + buffer_minutes

        if event_start <= current_minutes <= event_end:
            affected = EVENT_CURRENCY_MAP.get(label, set())
            if currencies & affected:
                return False, f"NEWS_BLOCK: {label} window ({event_hour}:00 UTC +/- {buffer_minutes}min), affects {currencies & affected}"

    # Also check custom calendar file if it exists
    if NEWS_SCHEDULE_PATH.exists():
        try:
            calendar = json.loads(NEWS_SCHEDULE_PATH.read_text(encoding="utf-8"))
            today_str = now.strftime("%Y-%m-%d")
            today_events = calendar.get(today_str, [])
            for event in today_events:
                evt_hour = int(event.get("hour_utc", 0))
                evt_dur = int(event.get("duration_minutes", 30))
                evt_currencies = set(event.get("currencies", []))
                evt_impact = event.get("impact", "").upper()
                if evt_impact != "HIGH":
                    continue
                evt_start = evt_hour * 60 - buffer_minutes
                evt_end = evt_hour * 60 + evt_dur + buffer_minutes
                if evt_start <= current_minutes <= evt_end and currencies & evt_currencies:
                    return False, f"NEWS_BLOCK: {event.get('name', 'event')} ({evt_hour}:00 UTC), affects {currencies & evt_currencies}"
        except Exception:
            pass

    return True, "NEWS_OK"


# ═══════════════════════════════════════════════════════════════
# Conviction Score (LOG_ONLY — equal-weighted)
# ═══════════════════════════════════════════════════════════════

def compute_conviction_score(features: dict, direction: str, mtf_data: dict) -> float:
    """Compute equal-weighted conviction score from available features.

    Ranges 0-1. Higher = more confirming factors aligned.
    LOG_ONLY until proven to separate outcomes after 60+ trades.
    """
    scores = []

    # 1. MTF alignment (0-1)
    mtf_score = mtf_data.get("alignment_score", 0)
    # Check if alignment direction matches trade direction
    mtf_dir = mtf_data.get("alignment", "neutral")
    target = "up" if direction.lower() == "long" else "down"
    if mtf_dir == target:
        scores.append(mtf_score)
    elif mtf_dir == "neutral":
        scores.append(0.5)
    else:
        scores.append(1.0 - mtf_score)  # opposing = low conviction

    # 2. Session score (0-1)
    session_score = features.get("session_score", 0.5)
    scores.append(session_score)

    # 3. Spread quality (inverted — lower ratio = higher conviction)
    spread_ratio = features.get("spread_ratio", 1.0)
    spread_score = max(0, min(1.0, 1.0 - (spread_ratio - 0.5) / 1.5))
    scores.append(spread_score)

    # 4. Regime match (binary)
    regime = features.get("regime", "UNKNOWN")
    # range_accel strategies prefer RANGING; trend strategies prefer TRENDING
    if regime in ("RANGING", "CHOPPY"):
        scores.append(0.7)
    elif regime == "TRENDING":
        scores.append(0.5)
    else:
        scores.append(0.3)

    # 5. 4hr bias match
    bias_4h = mtf_data.get("timeframes", {}).get("4h", {}).get("direction", "neutral")
    if bias_4h == target:
        scores.append(1.0)
    elif bias_4h == "neutral":
        scores.append(0.5)
    else:
        scores.append(0.0)

    # 6. News clear (binary)
    # If we got here, news filter already passed (or is LOG_ONLY)
    scores.append(0.8)  # slight bonus for not being near events

    # Equal-weighted average
    return round(sum(scores) / len(scores), 3) if scores else 0.5


# ═══════════════════════════════════════════════════════════════
# Correlation-Aware Entry Sequencing
# ═══════════════════════════════════════════════════════════════

# Currency pairs that are correlated (same direction exposure)
CORRELATION_GROUPS = {
    "USD_shorts": ["EURUSD", "GBPUSD", "AUDUSD"],  # all long = short USD
    "JPY_shorts": ["EURJPY", "GBPJPY", "AUDJPY", "CADJPY"],  # all long = short JPY
}


def check_entry_sequencing(
    symbol: str,
    direction: str,
    instruments: list,
    min_gap_minutes: float = 30.0,
) -> tuple[bool, str]:
    """Delay correlated entries to avoid all-in-at-once on same exposure.

    If a correlated pair entered within min_gap_minutes, delay this entry.
    """
    symbol = symbol.upper()
    direction = direction.upper()

    # Find which correlation group this symbol belongs to
    my_groups = []
    for group_name, members in CORRELATION_GROUPS.items():
        if symbol in members:
            my_groups.append((group_name, members))

    if not my_groups:
        return True, "SEQ_OK: no correlation group"

    now = datetime.now(timezone.utc)

    for group_name, members in my_groups:
        for inst in instruments:
            inst_sym = getattr(inst, "symbol", "").upper()
            if inst_sym == symbol or inst_sym not in members:
                continue

            inst_state = getattr(inst, "state", None)
            if not inst_state:
                continue

            # Check if this correlated pair has an open position in same direction
            inst_pos = getattr(inst_state, "position", "FLAT")
            if inst_pos == "FLAT":
                continue

            # Same direction? (both LONG in USD_shorts = both short USD)
            if inst_pos == direction:
                entry_time = getattr(inst_state, "entry_time", None)
                if entry_time:
                    elapsed = (now - entry_time).total_seconds() / 60
                    if elapsed < min_gap_minutes:
                        return False, f"SEQ_DELAY: {inst_sym} entered {direction} {elapsed:.0f}min ago (need {min_gap_minutes}min gap in {group_name})"

    return True, "SEQ_OK"
