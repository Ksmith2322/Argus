"""Volatility-targeted sizing helper.

The 5/25 cohort correlation audit found variants correlate 0.07-0.62
in NORMAL months but de-correlate badly in tail months (DD 23-66% →
50-88% in bear regimes). Vol-targeting scales exposure inversely to
recent realized vol, so when markets get volatile (which usually
precedes drawdowns) the fleet automatically reduces size.

USAGE
=====
    from helio.vol_target import vol_target_scale
    scale = vol_target_scale(
        recent_returns=last_60_monthly_returns,
        target_annual_vol=0.15,
    )
    position_size *= scale

DESIGN
======
- Computes trailing realized vol of the strategy's monthly returns
- Returns scale = target_vol / realized_vol
- Clamps to [0.5, 2.0] so the strategy never goes fully flat or
  over-leverages on a deceptively quiet month
- Returns 1.0 (no scaling) on insufficient data — fail-OPEN to the
  default size

CONVENTIONS
===========
- All vols are annualized
- All returns are MONTHLY (matching xs_momentum cadence). For daily
  strategies, pass daily returns + use daily_to_annual_factor()
- This is a PURE FUNCTION — no I/O, no state. Calling it twice with
  the same input returns the same output.
"""
from __future__ import annotations

import math


# Scale factor clamps. Live tested 2x as the practical max — beyond
# that the strategy is leveraging up on a quiet month that might
# reverse next bar.
MIN_SCALE = 0.5
MAX_SCALE = 2.0


def realized_vol_annual(
    returns: list[float],
    *,
    periods_per_year: float = 12.0,
) -> float | None:
    """Annualized realized volatility of a return series.

    `returns` are period-level (monthly by default; pass daily with
    periods_per_year=252).

    Returns None if fewer than 3 observations or zero variance.
    """
    if returns is None or len(returns) < 3:
        return None
    n = len(returns)
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    if var <= 0:
        return None
    return math.sqrt(var) * math.sqrt(periods_per_year)


def vol_target_scale(
    recent_returns: list[float],
    *,
    target_annual_vol: float = 0.15,
    periods_per_year: float = 12.0,
    min_scale: float = MIN_SCALE,
    max_scale: float = MAX_SCALE,
) -> float:
    """Compute the position-size scale factor that targets
    `target_annual_vol`.

    scale = target / realized, clamped to [min_scale, max_scale].

    Returns 1.0 (no scaling) when realized vol can't be computed
    (insufficient data) — strategy operates at default sizing.
    """
    realized = realized_vol_annual(recent_returns,
                                   periods_per_year=periods_per_year)
    if realized is None or realized <= 0:
        return 1.0
    raw = target_annual_vol / realized
    return max(min_scale, min(max_scale, raw))
