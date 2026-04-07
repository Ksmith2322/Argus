"""MTF Analysis Engine — orchestrates all sub-analyzers."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .types import (
    AnalysisResult, TrendInfo, FibLevels, SRLevel, TrendlinePair,
    PatternInfo, BreakoutInfo,
)

log = logging.getLogger("mtf_engine")


class MTFAnalysisEngine:
    """Multi-timeframe technical analysis engine.

    Usage:
        engine = MTFAnalysisEngine("EURUSD", config)
        engine.on_seed(df_1m)          # bulk load 5 days of 1m bars
        engine.on_bar(bar_dict)        # incremental 1m bar update
        result = engine.analyze(1.1560) # get full analysis
    """

    # Recompute expensive analysis every N minutes
    _SR_RECOMPUTE_MINUTES = 30
    _PATTERN_RECOMPUTE_MINUTES = 30

    def __init__(self, symbol: str, config: dict | None = None):
        self.symbol = symbol.upper()
        self.config = config or {}
        self._seeded = False

        # Lazy imports to avoid circular deps and allow partial builds
        self._resampler = None
        self._1m_bars: pd.DataFrame | None = None  # raw 1m bars for VWAP
        self._cached_result: AnalysisResult | None = None
        self._cache_ts: float = 0
        self._cache_ttl: float = 30  # seconds — reuse analysis within this window

        # Cached expensive computations
        self._sr_levels: list[dict] = []
        self._sr_last_compute: float = 0
        self._patterns: list[dict] = []
        self._patterns_last_compute: float = 0

    def on_seed(self, df_1m: pd.DataFrame) -> None:
        """Bulk-load historical 1m bars to warm up all indicators."""
        from .resampler import Resampler
        self._resampler = Resampler()
        self._resampler.on_seed(df_1m)
        # Keep tail of 1m bars for VWAP (last 1500 = ~25 hours)
        tail = df_1m.tail(1500).copy()
        if not isinstance(tail.index, pd.DatetimeIndex):
            tail.index = pd.to_datetime(tail.index)
        # Normalize columns to capitalized
        col_map = {c: c.capitalize() for c in tail.columns if c[0].islower()}
        if col_map:
            tail = tail.rename(columns=col_map)
        self._1m_bars = tail
        self._seeded = True
        log.info(f"{self.symbol}: MTF engine seeded with {len(df_1m)} bars")

    def on_bar(self, bar: dict) -> None:
        """Incremental update from a new 1m bar close."""
        if self._resampler is None:
            return
        self._resampler.on_bar(bar)
        # Append to 1m bar store for VWAP
        if self._1m_bars is not None:
            ts = bar.get("t") or bar.get("ts")
            new_row = pd.DataFrame([{
                "Open": bar.get("o", bar.get("open", 0)),
                "High": bar.get("h", bar.get("high", 0)),
                "Low": bar.get("l", bar.get("low", 0)),
                "Close": bar.get("c", bar.get("close", 0)),
                "Volume": bar.get("v", bar.get("volume", 0)),
            }], index=[pd.Timestamp(ts)])
            self._1m_bars = pd.concat([self._1m_bars.iloc[-1499:], new_row])
        # Invalidate cache on new bar
        self._cached_result = None

    def analyze(self, current_price: float) -> AnalysisResult:
        """Run full multi-timeframe analysis. Returns cached result if recent."""
        if not self._seeded or self._resampler is None:
            return AnalysisResult(directional_bias="BOTH", confidence=0.0)

        now = time.time()
        if self._cached_result and (now - self._cache_ts) < self._cache_ttl:
            return self._cached_result

        t0 = time.perf_counter()
        result = self._compute(current_price, now)
        result.computation_ms = (time.perf_counter() - t0) * 1000
        result.timestamp = datetime.now(timezone.utc).isoformat()

        self._cached_result = result
        self._cache_ts = now
        return result

    def _compute(self, price: float, now: float) -> AnalysisResult:
        """Core analysis computation."""
        result = AnalysisResult()

        # Get resampled DataFrames
        df_5m = self._resampler.get_df("5m")
        df_30m = self._resampler.get_df("30m")
        df_1h = self._resampler.get_df("1h")
        df_4h = self._resampler.get_df("4h")
        df_1m = self._1m_bars

        result.bars_analyzed = len(df_1m) if df_1m is not None else 0

        # --- 1. EMAs + Trend per timeframe ---
        from .indicators import compute_emas
        from .trend_consensus import classify_trend, compute_consensus

        for tf_name, df in [("4h", df_4h), ("1h", df_1h), ("30m", df_30m), ("5m", df_5m)]:
            if df is None or len(df) < 20:
                continue
            emas = compute_emas(df, periods=[50, 100, 200])
            e50 = float(emas[50].iloc[-1]) if 50 in emas and len(emas[50]) > 0 else 0
            e100 = float(emas[100].iloc[-1]) if 100 in emas and len(emas[100]) > 0 else 0
            e200 = float(emas[200].iloc[-1]) if 200 in emas and len(emas[200]) > 0 else 0
            trend = classify_trend(price, e50, e100, e200)
            setattr(result, f"trend_{tf_name}", trend)

        # Consensus
        score, bias, confidence = compute_consensus(
            result.trend_4h, result.trend_1h, result.trend_30m, result.trend_5m
        )
        result.trend_consensus_score = score
        result.directional_bias = bias
        result.confidence = confidence

        # --- 2. Bollinger Bands (on 5m) ---
        from .indicators import compute_bollinger
        if df_5m is not None and len(df_5m) >= 20:
            bb = compute_bollinger(df_5m)
            result.bb_upper = float(bb["upper"].iloc[-1])
            result.bb_lower = float(bb["lower"].iloc[-1])
            result.bb_position = float(bb["pct_b"].iloc[-1]) if not pd.isna(bb["pct_b"].iloc[-1]) else 0.5
            result.bb_squeeze = float(bb["bandwidth"].iloc[-1]) < 0.001 if not pd.isna(bb["bandwidth"].iloc[-1]) else False

        # --- 3. RSI + Divergence (on 5m) ---
        from .indicators import compute_rsi, detect_rsi_divergence
        if df_5m is not None and len(df_5m) >= 20:
            rsi = compute_rsi(df_5m)
            if len(rsi) > 0:
                result.rsi_14 = float(rsi.iloc[-1])
                result.rsi_divergence = detect_rsi_divergence(df_5m, rsi)

        # --- 4. VWAP (on 1m) ---
        from .indicators import compute_vwap
        if df_1m is not None and len(df_1m) >= 10:
            vwap_series = compute_vwap(df_1m)
            if len(vwap_series) > 0:
                result.vwap = float(vwap_series.iloc[-1])
                if result.vwap > 0:
                    result.vwap_position = "above" if price > result.vwap * 1.0001 else (
                        "below" if price < result.vwap * 0.9999 else "at"
                    )

        # --- 5. Swing Detection + Fibonacci (on 30m) ---
        from .swing_detection import find_swings, compute_fibonacci, nearest_fib_level
        swings_df = df_30m if df_30m is not None and len(df_30m) >= 30 else df_1h
        if swings_df is not None and len(swings_df) >= 20:
            swing_highs, swing_lows = find_swings(swings_df, order=5)
            if swing_highs and swing_lows:
                sh = max(swing_highs, key=lambda s: s["price"])
                sl = min(swing_lows, key=lambda s: s["price"])
                fib_levels = compute_fibonacci(sh["price"], sl["price"])
                result.fib = FibLevels(
                    swing_high=sh["price"],
                    swing_low=sl["price"],
                    levels=fib_levels,
                )
                nearest = nearest_fib_level(price, fib_levels)
                if nearest:
                    result.fib.nearest_level = nearest[0]
                    result.fib.distance_to_nearest = nearest[2]
                    result.near_fib_level = abs(nearest[2]) < 0.001

                # --- 6. Support/Resistance (throttled) ---
                if (now - self._sr_last_compute) > self._SR_RECOMPUTE_MINUTES * 60:
                    from .support_resistance import find_sr_clusters, compute_pivots, find_trendlines, get_nearest_sr
                    self._sr_levels = find_sr_clusters(swings_df)
                    self._sr_last_compute = now

                    # Trendlines
                    tl = find_trendlines(swing_highs, swing_lows)
                    if tl:
                        n = len(swings_df) - 1  # current bar index for projection
                        result.trendlines = TrendlinePair(
                            ascending_slope=tl.get("ascending_slope", 0),
                            ascending_intercept=tl.get("ascending_intercept", 0),
                            ascending_current=tl.get("ascending_slope", 0) * n + tl.get("ascending_intercept", 0) if tl.get("ascending_slope") else 0,
                            ascending_touches=tl.get("ascending_touches", 0),
                            descending_slope=tl.get("descending_slope", 0),
                            descending_intercept=tl.get("descending_intercept", 0),
                            descending_current=tl.get("descending_slope", 0) * n + tl.get("descending_intercept", 0) if tl.get("descending_slope") else 0,
                            descending_touches=tl.get("descending_touches", 0),
                            converging=tl.get("converging", False),
                        )

                # Pivots (from daily data or 4H)
                if df_4h is not None and len(df_4h) >= 2:
                    prev = df_4h.iloc[-2]
                    from .support_resistance import compute_pivots
                    pivots = compute_pivots(float(prev["High"]), float(prev["Low"]), float(prev["Close"]))
                    result.pivot_pp = pivots["pp"]
                    result.pivot_r1 = pivots["r1"]
                    result.pivot_s1 = pivots["s1"]

                # Nearest S/R
                if self._sr_levels:
                    from .support_resistance import get_nearest_sr
                    sup, res = get_nearest_sr(price, self._sr_levels)
                    if sup:
                        result.nearest_support = sup["price"]
                        result.at_support = abs(price - sup["price"]) / price < 0.001
                    if res:
                        result.nearest_resistance = res["price"]
                        result.at_resistance = abs(price - res["price"]) / price < 0.001

                # --- 7. Chart Patterns (throttled) ---
                if (now - self._patterns_last_compute) > self._PATTERN_RECOMPUTE_MINUTES * 60:
                    try:
                        from .patterns import scan_patterns
                        self._patterns = scan_patterns(swings_df, swing_highs, swing_lows)
                        self._patterns_last_compute = now
                    except Exception:
                        self._patterns = []

                result.active_patterns = [
                    PatternInfo(
                        pattern_type=p.get("pattern", ""),
                        direction_bias=p.get("direction_bias", ""),
                        confidence=p.get("confidence", 0),
                        completion_pct=p.get("completion_pct", 0),
                        neckline=p.get("neckline", 0),
                    )
                    for p in self._patterns
                ]

                # --- 8. Breakout Detection ---
                if self._sr_levels and df_5m is not None and len(df_5m) >= 10:
                    try:
                        from .breakout_detector import check_breakout
                        bo = check_breakout(price, df_5m.tail(10), self._sr_levels)
                        result.breakout = BreakoutInfo(
                            is_breakout=bo.get("is_breakout", False),
                            is_fakeout=bo.get("is_fakeout", False),
                            direction=bo.get("direction", "none"),
                            level=bo.get("level", 0),
                            volume_confirmed=bo.get("volume_confirmed", False),
                            bars_since_break=bo.get("bars_since_break", 0),
                            fakeout_risk=bo.get("fakeout_risk", 0),
                        )
                    except Exception:
                        pass

        # --- With-trend flag ---
        result.with_trend = result.trend_consensus_score > 0.2 or result.trend_consensus_score < -0.2

        return result
