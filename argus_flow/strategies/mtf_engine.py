"""Multi-Timeframe Strategy Engine — 4H trend / 1H confirmation / 5M execution.

The meat of the move: don't catch reversals, don't chase exhaustion.
Wait for 4H trend, confirm on 1H, execute on 5M candle pattern.

Usage:
    engine = MTFStrategyEngine(symbol="AUDJPY", pip_size=0.01)
    engine.update_bar(bar_1m)  # feed 1-minute bars
    signal = engine.evaluate()  # returns {"direction": "long", "setup": ..., "confidence": ...} or None
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

_log = logging.getLogger("argus.mtf")


@dataclass
class MTFSignal:
    """Output of the MTF strategy evaluation."""
    direction: str          # "long" or "short"
    confidence: float       # 0-1 composite confidence
    trend_4h: str           # "UP" or "DOWN"
    setup_1h: str           # e.g. "PULLBACK_TO_EMA", "BREAK_OF_STRUCTURE", "TREND_CONTINUATION"
    trigger_5m: str         # e.g. "engulfing", "hammer", "strong_bar"
    support: float          # nearest 4H support
    resistance: float       # nearest 4H resistance
    ema_8_4h: float
    ema_21_4h: float
    rsi_1h: float
    reason: str             # human-readable explanation


class BarResampler:
    """Accumulate 1m bars into higher timeframe OHLCV bars."""

    def __init__(self, tf_minutes: int, maxlen: int = 200):
        self.tf_minutes = tf_minutes
        self.maxlen = maxlen
        self.bars: deque[dict] = deque(maxlen=maxlen)
        self._current: Optional[dict] = None
        self._current_boundary: Optional[datetime] = None
        self.total_completed: int = 0  # monotonic counter of completed bars

    def add_1m_bar(self, bar: dict) -> Optional[dict]:
        """Add a 1m bar. Returns completed higher-TF bar if boundary crossed, else None."""
        try:
            ts_raw = bar.get("ts", bar.get("t", ""))
            ts = pd.Timestamp(ts_raw)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
        except Exception:
            return None

        # Determine which TF bar this belongs to
        minute_of_day = ts.hour * 60 + ts.minute
        boundary = minute_of_day // self.tf_minutes * self.tf_minutes
        boundary_dt = ts.replace(hour=boundary // 60, minute=boundary % 60, second=0, microsecond=0)

        try:
            o = float(bar.get("open", bar.get("o", 0)))
            h = float(bar.get("high", bar.get("h", 0)))
            l = float(bar.get("low", bar.get("l", 0)))
            c = float(bar.get("close", bar.get("c", 0)))
            v = float(bar.get("volume", bar.get("v", 0)))
        except (TypeError, ValueError):
            return None
        if o <= 0 or c <= 0:
            return None

        completed = None

        if self._current is None or boundary_dt != self._current_boundary:
            # New TF bar started — close previous one
            if self._current is not None:
                completed = dict(self._current)
                self.bars.append(completed)
                self.total_completed += 1
            # Start new bar
            self._current = {
                "ts": str(boundary_dt), "open": o, "high": h, "low": l, "close": c, "volume": v
            }
            self._current_boundary = boundary_dt
        else:
            # Update current bar
            self._current["high"] = max(self._current["high"], h)
            self._current["low"] = min(self._current["low"], l)
            self._current["close"] = c
            self._current["volume"] += v

        return completed

    def to_df(self) -> pd.DataFrame:
        """Return completed bars as DataFrame."""
        if not self.bars:
            return pd.DataFrame()
        return pd.DataFrame(list(self.bars))

    def last(self) -> Optional[dict]:
        """Most recent completed bar."""
        return self.bars[-1] if self.bars else None

    def __len__(self) -> int:
        return len(self.bars)


class MTFStrategyEngine:
    """Multi-Timeframe strategy: 4H direction, 1H confirmation, 5M execution.

    Feed it 1-minute bars. It resamples internally and evaluates when all
    three timeframes align.
    """

    def __init__(
        self,
        symbol: str,
        pip_size: float = 0.0001,
        *,
        trend_ema_fast: int = 8,
        trend_ema_slow: int = 21,
        rsi_period: int = 14,
        min_trend_strength: float = 3.0,  # minimum EMA separation in pips (was 0.5 — too loose)
    ):
        self.symbol = symbol
        self.pip_size = pip_size
        self.trend_ema_fast = trend_ema_fast
        self.trend_ema_slow = trend_ema_slow
        self.rsi_period = rsi_period
        self.min_trend_strength = min_trend_strength

        # Resamplers: 1m -> 5m, 1m -> 1H, 1m -> 4H
        self.rs_5m = BarResampler(5, maxlen=200)
        self.rs_1h = BarResampler(60, maxlen=100)
        self.rs_4h = BarResampler(240, maxlen=50)

        # Cached analysis
        self._last_4h_analysis: Optional[dict] = None
        self._last_1h_analysis: Optional[dict] = None
        self._last_4h_bar_count: int = 0
        self._last_1h_bar_count: int = 0
        self._last_signal_5m_count: int = 0  # cooldown: min 5m bars between signals
        self._signal_cooldown_bars: int = 36  # 36 x 5m = 3 hours between signals
        self._last_eval_5m: int = 0

        self._log = logging.getLogger(f"mtf.{symbol.lower()}")

    def update_bar(self, bar_1m: dict) -> None:
        """Feed a 1-minute bar. Updates all internal timeframes."""
        self.rs_5m.add_1m_bar(bar_1m)
        self.rs_1h.add_1m_bar(bar_1m)
        self.rs_4h.add_1m_bar(bar_1m)

    def seed(self, bars_1m: list[dict]) -> None:
        """Seed with historical 1m bars (e.g., from IBKR)."""
        for bar in bars_1m:
            self.update_bar(bar)
        self._log.info(
            f"MTF seeded: 5m={len(self.rs_5m)} 1h={len(self.rs_1h)} 4h={len(self.rs_4h)}"
        )

    def evaluate(self, current_mid: Optional[float] = None) -> Optional[MTFSignal]:
        """Evaluate all timeframes. Returns MTFSignal if entry conditions met, else None.

        Call this after each new 5m bar closes (or on each 1m bar if desired).
        """
        # Need minimum data
        if len(self.rs_4h) < 22 or len(self.rs_1h) < 22 or len(self.rs_5m) < 15:
            return None

        # Only evaluate on new 5m bar close (not every 1m bar)
        completed_5m = self.rs_5m.total_completed
        if completed_5m == self._last_eval_5m:
            return None
        self._last_eval_5m = completed_5m

        # Cooldown: don't signal again within N completed 5m bars of last signal
        if completed_5m - self._last_signal_5m_count < self._signal_cooldown_bars:
            return None

        # Step 1: 4H Trend (cache — only recompute on new 4H bar)
        if self.rs_4h.total_completed != self._last_4h_bar_count:
            self._last_4h_analysis = self._analyze_4h()
            self._last_4h_bar_count = self.rs_4h.total_completed

        trend = self._last_4h_analysis
        if trend is None or trend["trend"] == "RANGE":
            return None  # Only trade with clear trend

        # Step 2: 1H Confirmation (cache — only recompute on new 1H bar)
        if self.rs_1h.total_completed != self._last_1h_bar_count:
            self._last_1h_analysis = self._analyze_1h(trend["trend"])
            self._last_1h_bar_count = self.rs_1h.total_completed

        confirm = self._last_1h_analysis
        if confirm is None or not confirm["confirmed"]:
            return None

        # Step 3: 5M Execution Trigger (must be near 1H key level)
        direction = "long" if trend["trend"] == "UP" else "short"
        trigger = self._find_5m_trigger(direction)
        if trigger is None:
            return None

        # Step 4: Level proximity filter — 5M trigger must be near S/R or EMA
        last_5m = self.rs_5m.last()
        if last_5m is not None:
            price = float(last_5m["close"])
            # For longs: price should be near support or 1H EMA21 (buying the dip)
            # For shorts: price should be near resistance or 1H EMA21 (selling the rally)
            ema_1h = confirm["ema_slow"]
            dist_to_ema = abs(price - ema_1h) / self.pip_size
            near_support = abs(price - trend["support"]) / self.pip_size < 30
            near_resistance = abs(price - trend["resistance"]) / self.pip_size < 30
            near_ema = dist_to_ema < 25  # within 25 pips of 1H EMA21

            if direction == "long" and not (near_support or near_ema):
                return None  # Long entry must be near support/EMA (buying the dip)
            if direction == "short" and not (near_resistance or near_ema):
                return None  # Short entry must be near resistance/EMA (selling the rally)

        # Composite confidence
        confidence = self._compute_confidence(trend, confirm, trigger)

        # Record signal time for cooldown
        self._last_signal_5m_count = self.rs_5m.total_completed

        return MTFSignal(
            direction=direction,
            confidence=confidence,
            trend_4h=trend["trend"],
            setup_1h=confirm["setup"],
            trigger_5m=trigger["type"],
            support=trend["support"],
            resistance=trend["resistance"],
            ema_8_4h=trend["ema_fast"],
            ema_21_4h=trend["ema_slow"],
            rsi_1h=confirm["rsi"],
            reason=(
                f"4H={trend['trend']}(str={trend['strength']:.1f}) "
                f"1H={confirm['setup']}(RSI={confirm['rsi']:.0f}) "
                f"5M={trigger['type']} conf={confidence:.0%}"
            ),
        )

    # ── 4H Analysis ──────────────────────────────────────────

    def _analyze_4h(self) -> Optional[dict]:
        """Determine 4H trend direction and key levels."""
        df = self.rs_4h.to_df()
        if len(df) < 22:
            return None

        closes = df["close"].astype(float).values
        highs = df["high"].astype(float).values
        lows = df["low"].astype(float).values

        # EMA 8 and 21
        ema_fast = self._ema(closes, self.trend_ema_fast)
        ema_slow = self._ema(closes, self.trend_ema_slow)
        current = closes[-1]

        # Trend determination
        separation = (ema_fast - ema_slow) / self.pip_size  # in pips
        if ema_fast > ema_slow and current > ema_slow and separation > self.min_trend_strength:
            trend = "UP"
        elif ema_fast < ema_slow and current < ema_slow and abs(separation) > self.min_trend_strength:
            trend = "DOWN"
        else:
            trend = "RANGE"

        # S/R from recent swing points
        recent_highs = sorted(highs[-20:], reverse=True)[:3]
        recent_lows = sorted(lows[-20:])[:3]
        resistance = float(np.mean(recent_highs))
        support = float(np.mean(recent_lows))

        return {
            "trend": trend,
            "strength": abs(separation),
            "ema_fast": float(ema_fast),
            "ema_slow": float(ema_slow),
            "support": support,
            "resistance": resistance,
            "current": current,
        }

    # ── 1H Confirmation ─────────────────────────────────────

    def _analyze_1h(self, trend_4h: str) -> Optional[dict]:
        """Check if 1H confirms the 4H setup."""
        df = self.rs_1h.to_df()
        if len(df) < 22:
            return None

        closes = df["close"].astype(float).values
        highs = df["high"].astype(float).values
        lows = df["low"].astype(float).values
        current = closes[-1]

        ema_fast = self._ema(closes, 8)
        ema_slow = self._ema(closes, 21)
        rsi = self._rsi(closes, self.rsi_period)

        confirmed = False
        setup = "NONE"
        reason = ""

        if trend_4h == "UP":
            # Pullback to EMA: price near or below 1H EMA21, RSI MUST be pulled back (not overbought)
            near_ema = current <= ema_slow * 1.001
            rsi_pulled_back = 30 < rsi < 50  # RSI must show actual pullback, not just "not overbought"
            # Break of structure: new 1H high with RSI momentum
            recent_high = max(highs[-6:-1]) if len(highs) > 6 else current
            new_high = current > recent_high
            # Trend continuation: EMAs aligned + RSI in sweet spot
            emas_aligned = ema_fast > ema_slow

            if near_ema and rsi_pulled_back:
                confirmed = True
                setup = "PULLBACK_TO_EMA"
                reason = f"Price at 1H EMA21, RSI pulled back to {rsi:.0f}"
            elif new_high and 50 < rsi < 70:
                confirmed = True
                setup = "BREAK_OF_STRUCTURE"
                reason = f"New 1H high, RSI={rsi:.0f} momentum"
            elif emas_aligned and 45 < rsi < 60 and near_ema:
                confirmed = True
                setup = "TREND_CONTINUATION"
                reason = f"1H EMAs UP + near EMA, RSI={rsi:.0f}"

        elif trend_4h == "DOWN":
            near_ema = current >= ema_slow * 0.999
            rsi_pulled_back = 50 < rsi < 70  # RSI must show rally into resistance
            recent_low = min(lows[-6:-1]) if len(lows) > 6 else current
            new_low = current < recent_low
            emas_aligned = ema_fast < ema_slow

            if near_ema and rsi_pulled_back:
                confirmed = True
                setup = "PULLBACK_TO_EMA"
                reason = f"Price at 1H EMA21, RSI rallied to {rsi:.0f}"
            elif new_low and 30 < rsi < 50:
                confirmed = True
                setup = "BREAK_OF_STRUCTURE"
                reason = f"New 1H low, RSI={rsi:.0f} momentum"
            elif emas_aligned and 40 < rsi < 55 and near_ema:
                confirmed = True
                setup = "TREND_CONTINUATION"
                reason = f"1H EMAs DOWN + near EMA, RSI={rsi:.0f}"

        return {
            "confirmed": confirmed,
            "setup": setup,
            "reason": reason,
            "rsi": rsi,
            "ema_fast": float(ema_fast),
            "ema_slow": float(ema_slow),
        }

    # ── 5M Execution Trigger ─────────────────────────────────

    def _find_5m_trigger(self, direction: str) -> Optional[dict]:
        """Check last few 5M bars for execution trigger (candle pattern)."""
        if len(self.rs_5m) < 3:
            return None

        df = self.rs_5m.to_df()
        # Only check the LAST completed bar (fresh signal only)
        for offset in [0]:
            idx = -(1 + offset)
            if abs(idx) > len(df):
                continue
            bar = df.iloc[idx]
            prev = df.iloc[idx - 1] if abs(idx - 1) <= len(df) else bar

            o = float(bar["open"])
            h = float(bar["high"])
            l = float(bar["low"])
            c = float(bar["close"])
            po = float(prev["open"])
            pc = float(prev["close"])

            body = abs(c - o)
            bar_range = h - l
            if bar_range <= 0:
                continue

            if direction == "long":
                # Bullish engulfing
                if c > o and c > max(po, pc) and o < min(po, pc) and body > bar_range * 0.3:
                    return {"type": "engulfing", "bar_idx": idx}
                # Hammer (long lower wick)
                if (min(o, c) - l) > body * 2 and (h - max(o, c)) < body * 0.5 and c > o:
                    return {"type": "hammer", "bar_idx": idx}
                # Strong bullish bar (>60% of range is body, closes in upper 30%)
                if c > o and body > bar_range * 0.6 and (h - c) < bar_range * 0.3:
                    return {"type": "strong_bullish", "bar_idx": idx}

            elif direction == "short":
                # Bearish engulfing
                if c < o and c < min(po, pc) and o > max(po, pc) and body > bar_range * 0.3:
                    return {"type": "engulfing", "bar_idx": idx}
                # Shooting star (long upper wick)
                if (h - max(o, c)) > body * 2 and (min(o, c) - l) < body * 0.5 and c < o:
                    return {"type": "shooting_star", "bar_idx": idx}
                # Strong bearish bar
                if c < o and body > bar_range * 0.6 and (c - l) < bar_range * 0.3:
                    return {"type": "strong_bearish", "bar_idx": idx}

        return None

    # ── Confidence scoring ───────────────────────────────────

    def _compute_confidence(self, trend: dict, confirm: dict, trigger: dict) -> float:
        """0-1 confidence score based on alignment quality."""
        score = 0.0

        # 4H trend strength (0-0.4)
        strength = min(trend["strength"] / 10.0, 1.0)  # normalize to 0-1
        score += strength * 0.4

        # 1H setup quality (0-0.3)
        setup_scores = {
            "PULLBACK_TO_EMA": 0.3,       # Best — buying the dip in a trend
            "BREAK_OF_STRUCTURE": 0.25,     # Good — momentum confirmation
            "TREND_CONTINUATION": 0.2,      # Decent — going with flow
        }
        score += setup_scores.get(confirm["setup"], 0.1)

        # 5M trigger quality (0-0.3)
        trigger_scores = {
            "engulfing": 0.3,              # Best — clear reversal pattern
            "hammer": 0.25,
            "shooting_star": 0.25,
            "strong_bullish": 0.2,
            "strong_bearish": 0.2,
        }
        score += trigger_scores.get(trigger["type"], 0.1)

        return min(score, 1.0)

    # ── Utilities ────────────────────────────────────────────

    @staticmethod
    def _ema(data: np.ndarray, period: int) -> float:
        """Compute EMA of the last value."""
        if len(data) < period:
            return float(np.mean(data))
        alpha = 2.0 / (period + 1)
        ema = data[0]
        for val in data[1:]:
            ema = alpha * val + (1 - alpha) * ema
        return float(ema)

    @staticmethod
    def _rsi(data: np.ndarray, period: int = 14) -> float:
        """Compute RSI."""
        if len(data) < period + 1:
            return 50.0
        deltas = np.diff(data)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return float(100.0 - (100.0 / (1.0 + rs)))
