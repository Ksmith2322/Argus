"""Data types for multi-timeframe technical analysis."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TrendInfo:
    direction: str = "neutral"  # up | down | neutral
    ema50: float = 0.0
    ema100: float = 0.0
    ema200: float = 0.0
    price_vs_ema50: str = "at"  # above | below | at
    strength: float = 0.0  # 0-1, how aligned EMAs are


@dataclass
class FibLevels:
    swing_high: float = 0.0
    swing_low: float = 0.0
    levels: dict[str, float] = field(default_factory=dict)
    # keys: "23.6", "38.2", "50.0", "61.8", "78.6"
    nearest_level: str = ""
    distance_to_nearest: float = 0.0


@dataclass
class SRLevel:
    price: float = 0.0
    strength: int = 0  # number of touches
    kind: str = ""  # support | resistance
    source: str = ""  # cluster | pivot | trendline


@dataclass
class TrendlinePair:
    ascending_slope: float = 0.0  # pips per bar
    ascending_intercept: float = 0.0
    ascending_current: float = 0.0  # projected price now
    ascending_touches: int = 0
    descending_slope: float = 0.0
    descending_intercept: float = 0.0
    descending_current: float = 0.0
    descending_touches: int = 0
    converging: bool = False  # triangle pattern hint


@dataclass
class PatternInfo:
    pattern_type: str = ""  # double_top | double_bottom | head_shoulders | inv_head_shoulders | triangle_asc | triangle_desc | triangle_sym | flag_bull | flag_bear
    direction_bias: str = ""  # long | short
    confidence: float = 0.0  # 0-1
    completion_pct: float = 0.0  # how close to breakout
    neckline: float = 0.0


@dataclass
class BreakoutInfo:
    is_breakout: bool = False
    is_fakeout: bool = False
    direction: str = "none"  # long | short | none
    level: float = 0.0
    volume_confirmed: bool = False
    bars_since_break: int = 0
    fakeout_risk: float = 0.0  # 0-1


@dataclass
class AnalysisResult:
    # Primary outputs for the runner
    directional_bias: str = "BOTH"  # LONG_ONLY | SHORT_ONLY | BOTH | NONE
    confidence: float = 0.0  # 0.0 - 1.0

    # Trend consensus
    trend_4h: TrendInfo = field(default_factory=TrendInfo)
    trend_1h: TrendInfo = field(default_factory=TrendInfo)
    trend_30m: TrendInfo = field(default_factory=TrendInfo)
    trend_5m: TrendInfo = field(default_factory=TrendInfo)
    trend_consensus_score: float = 0.0  # -1.0 (strong short) to +1.0 (strong long)

    # Key levels
    nearest_support: float = 0.0
    nearest_resistance: float = 0.0
    support_levels: list[SRLevel] = field(default_factory=list)
    resistance_levels: list[SRLevel] = field(default_factory=list)
    fib: FibLevels = field(default_factory=FibLevels)
    pivot_pp: float = 0.0
    pivot_r1: float = 0.0
    pivot_s1: float = 0.0

    # Indicators
    rsi_14: float = 50.0
    rsi_divergence: str = "none"  # bullish | bearish | none
    bb_position: float = 0.5  # 0=lower band, 0.5=middle, 1=upper band
    bb_upper: float = 0.0
    bb_lower: float = 0.0
    bb_squeeze: bool = False
    vwap: float = 0.0
    vwap_position: str = "at"  # above | below | at

    # Trendlines
    trendlines: TrendlinePair = field(default_factory=TrendlinePair)

    # Pattern detection
    active_patterns: list[PatternInfo] = field(default_factory=list)

    # Breakout/fakeout
    breakout: BreakoutInfo = field(default_factory=BreakoutInfo)

    # Entry quality hints
    at_support: bool = False
    at_resistance: bool = False
    near_fib_level: bool = False
    with_trend: bool = False  # True if the proposed direction aligns with HTF trend

    # Metadata
    bars_analyzed: int = 0
    computation_ms: float = 0.0
    timestamp: str = ""
