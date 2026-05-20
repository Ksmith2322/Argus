"""Cointegration helpers for forge_coint_pairs strategy.

Engle-Granger style test: fit a linear regression of asset_a on asset_b
(or equal-weight spread = a - b), compute the residual series, then
either run an ADF stationarity test OR use a simple half-life check
to filter pairs that mean-revert reliably.

For a one-person retail operation, the lightweight half-life check is
practical:
  - Fit hedge ratio: a = beta * b + spread
  - Compute spread = a - beta * b (residual series)
  - Estimate half-life of mean reversion via Ornstein-Uhlenbeck fit
  - Pair is tradeable if half-life is between 3 and 60 trading days

Trade rules (per Architect audit 2026-05-19, #5 candidate):
  - Compute rolling z-score of spread over a lookback window (60 days)
  - LONG spread (long a / short b * beta) when z <= -2 (spread cheap)
  - SHORT spread when z >= 2 (spread rich)
  - EXIT at z = 0 (mean revert) OR hard stop at |z| >= 3 (regime break)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


# Suggested pairs (Architect's spec — known cointegrated combos):
DEFAULT_PAIRS: tuple[tuple[str, str], ...] = (
    ("KO", "PEP"),        # consumer staples beverages
    ("XOM", "CVX"),       # integrated oil majors
    ("HD", "LOW"),         # home improvement
    ("GLD", "SLV"),        # precious metals
    ("EWZ", "EWW"),        # LatAm equities (Brazil/Mexico)
    ("TLT", "IEF"),        # duration curve
    ("V", "MA"),           # payment networks
    ("MSFT", "GOOGL"),     # mega-cap tech
)


@dataclass(frozen=True)
class PairStats:
    """Snapshot of one pair at evaluation time."""
    ticker_a: str
    ticker_b: str
    beta: float           # hedge ratio from regression
    spread_now: float     # most recent residual (a_t - beta * b_t)
    spread_mean: float    # rolling mean of spread over lookback
    spread_std: float     # rolling std of spread
    zscore: float         # (spread_now - mean) / std
    half_life_days: float  # estimated mean-reversion half-life


def compute_hedge_ratio(closes_a: Sequence[float],
                        closes_b: Sequence[float]) -> float:
    """OLS regression of a on b: a_t = beta * b_t + epsilon.
    Returns beta. Used to construct the spread = a - beta * b."""
    if len(closes_a) != len(closes_b) or len(closes_a) < 2:
        return 1.0
    n = len(closes_a)
    sum_a = sum(closes_a)
    sum_b = sum(closes_b)
    sum_ab = sum(a * b for a, b in zip(closes_a, closes_b))
    sum_bb = sum(b * b for b in closes_b)
    denom = n * sum_bb - sum_b * sum_b
    if abs(denom) < 1e-12:
        return 1.0
    beta = (n * sum_ab - sum_a * sum_b) / denom
    return beta


def compute_spread_series(
    closes_a: Sequence[float],
    closes_b: Sequence[float],
    beta: float,
) -> list[float]:
    """spread_t = a_t - beta * b_t for each t."""
    return [a - beta * b for a, b in zip(closes_a, closes_b)]


def estimate_half_life(spread: Sequence[float]) -> float:
    """Estimate Ornstein-Uhlenbeck half-life via regression of
    delta_spread on lagged spread:
        spread_t - spread_{t-1} = lambda * spread_{t-1} + e
    Half-life = -log(2) / lambda (days). Returns inf if non-mean-reverting."""
    if len(spread) < 30:
        return float("inf")
    deltas = []
    lags = []
    for i in range(1, len(spread)):
        deltas.append(spread[i] - spread[i - 1])
        lags.append(spread[i - 1])
    # OLS: delta = lambda * lag
    n = len(deltas)
    sum_x = sum(lags)
    sum_y = sum(deltas)
    sum_xy = sum(x * y for x, y in zip(lags, deltas))
    sum_xx = sum(x * x for x in lags)
    denom = n * sum_xx - sum_x * sum_x
    if abs(denom) < 1e-12:
        return float("inf")
    lam = (n * sum_xy - sum_x * sum_y) / denom
    if lam >= 0:
        # Non-mean-reverting (positive lambda = explosive)
        return float("inf")
    return -math.log(2) / lam


def compute_pair_stats(
    ticker_a: str, ticker_b: str,
    closes_a: Sequence[float], closes_b: Sequence[float],
    lookback: int = 60,
) -> PairStats:
    """Compute z-score + half-life on the trailing `lookback` window."""
    if len(closes_a) < lookback or len(closes_b) < lookback:
        # Insufficient history → return sentinel
        return PairStats(ticker_a, ticker_b, 1.0, 0.0, 0.0, 0.0, 0.0, float("inf"))
    a_window = list(closes_a[-lookback:])
    b_window = list(closes_b[-lookback:])
    beta = compute_hedge_ratio(a_window, b_window)
    spread = compute_spread_series(a_window, b_window, beta)
    mean_s = sum(spread) / len(spread)
    var_s = sum((s - mean_s) ** 2 for s in spread) / max(1, len(spread) - 1)
    std_s = math.sqrt(var_s) if var_s > 0 else 0.0
    spread_now = spread[-1]
    zscore = (spread_now - mean_s) / std_s if std_s > 0 else 0.0
    hl = estimate_half_life(spread)
    return PairStats(
        ticker_a=ticker_a, ticker_b=ticker_b,
        beta=beta, spread_now=spread_now,
        spread_mean=mean_s, spread_std=std_s, zscore=zscore,
        half_life_days=hl,
    )


@dataclass(frozen=True)
class PairDecision:
    action: str         # "ENTER_LONG_SPREAD" | "ENTER_SHORT_SPREAD" | "EXIT" | "HOLD" | "WAIT"
    reason: str
    zscore: float
    half_life_days: float


def evaluate_pair_signal(
    stats: PairStats,
    *,
    has_open_position: str = "",   # "" | "long" | "short"
    entry_z: float = 2.0,
    exit_z: float = 0.0,
    stop_z: float = 3.0,
    min_half_life_days: float = 3.0,
    max_half_life_days: float = 60.0,
) -> PairDecision:
    """Pure decision logic. The runner separately handles position state
    (open vs flat, which direction) by passing the appropriate has_open."""
    z = stats.zscore
    hl = stats.half_life_days

    if has_open_position == "long":
        # We're long spread, looking for mean revert OR stop
        if z >= stop_z:
            return PairDecision("EXIT", f"long_stop_z_{z:.2f}>={stop_z}", z, hl)
        if z >= -exit_z:  # spread mean-reverted toward 0
            return PairDecision("EXIT", f"long_mean_revert_z_{z:.2f}", z, hl)
        return PairDecision("HOLD", "long_holding", z, hl)

    if has_open_position == "short":
        if z <= -stop_z:
            return PairDecision("EXIT", f"short_stop_z_{z:.2f}<=-{stop_z}", z, hl)
        if z <= exit_z:
            return PairDecision("EXIT", f"short_mean_revert_z_{z:.2f}", z, hl)
        return PairDecision("HOLD", "short_holding", z, hl)

    # Flat — evaluate entry
    if not (min_half_life_days <= hl <= max_half_life_days):
        return PairDecision("WAIT",
                            f"half_life_outside_range_{hl:.1f}",
                            z, hl)
    if z <= -entry_z:
        return PairDecision("ENTER_LONG_SPREAD",
                            f"spread_cheap_z_{z:.2f}<=-{entry_z}", z, hl)
    if z >= entry_z:
        return PairDecision("ENTER_SHORT_SPREAD",
                            f"spread_rich_z_{z:.2f}>={entry_z}", z, hl)
    return PairDecision("WAIT", f"z_inside_{z:.2f}", z, hl)
