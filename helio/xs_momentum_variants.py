"""Cross-sectional momentum variants: alternative ranking + sizing logic.

Three orthogonal axes of variation explored by the universe-sweep
backtest:

  RANKING MODE                  SIZING MODE
  ─────────────────             ─────────────────────
  single_12_1   (current)       equal_weight   (current)
  multi_horizon                 vol_scaled
                                conviction_scaled

The production strategy uses (single_12_1, equal_weight). Variants
are scored via the disciplined gate and only deployed if they
pass at the realistic-slippage CI floor.

Pure-function — no I/O, no IBKR coupling. Backtest + sweep tools
import these and pass the rank/size hooks into the existing engine.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from helio.xs_momentum import (
    LOOKBACK_LONG_DAYS,
    LOOKBACK_SHORT_DAYS,
    MomentumScore,
    compute_momentum_score,
)


# ─── Multi-horizon momentum ─────────────────────────────────────────

# Three lookbacks: 12-1 (long horizon), 6-1 (medium), 3-1 (short).
# Equal-weight by default; future versions can re-weight by regime.
MULTI_HORIZON_LOOKBACKS: tuple[tuple[int, int], ...] = (
    (252, 21),   # 12-1
    (126, 21),   # 6-1
    (63, 21),    # 3-1
)


def rank_universe_multi_horizon(
    per_asset_closes: dict[str, list[float]],
    *,
    lookbacks: tuple[tuple[int, int], ...] = MULTI_HORIZON_LOOKBACKS,
) -> list[MomentumScore]:
    """Rank assets by an equal-weight average of multiple lookback
    horizons. Each horizon contributes its own 12-1-style score; the
    final score is the mean.

    Returns the same MomentumScore shape as the single-horizon
    ranker so the rest of the pipeline doesn't need to change.
    Assets that lack data for ANY horizon are dropped.
    """
    out: list[MomentumScore] = []
    for ticker, closes in per_asset_closes.items():
        scores: list[float] = []
        for long_lb, short_lb in lookbacks:
            s = compute_momentum_score(closes, long_lb, short_lb)
            if s is None:
                break
            scores.append(s)
        if len(scores) != len(lookbacks):
            continue
        avg = sum(scores) / len(scores)
        # Use the longest-horizon prices for the diagnostic fields.
        long_lb, short_lb = lookbacks[0]
        out.append(MomentumScore(
            ticker=ticker,
            score=avg,
            p_now=float(closes[-1]),
            p_long_ago=float(closes[-(long_lb + 1)]),
            p_recent=float(closes[-(short_lb + 1)]),
        ))
    return sorted(out, key=lambda s: s.score, reverse=True)


# ─── Vol-scaled position sizing ─────────────────────────────────────

@dataclass(frozen=True)
class SizedPick:
    """One pick with its weight in the portfolio.
    Weights across all picks sum to 1.0."""
    ticker: str
    score: float
    weight: float


def _trailing_volatility(
    closes: list[float],
    window_days: int = 60,
) -> float | None:
    """Annualized trailing volatility of daily log returns. Returns
    None if insufficient data."""
    if len(closes) < window_days + 1:
        return None
    tail = closes[-(window_days + 1):]
    rets: list[float] = []
    for i in range(1, len(tail)):
        p0, p1 = float(tail[i - 1]), float(tail[i])
        if p0 <= 0 or p1 <= 0:
            return None
        rets.append(math.log(p1 / p0))
    if not rets:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    daily_vol = math.sqrt(var)
    return daily_vol * math.sqrt(252.0)


def size_picks_equal_weight(picks: list[MomentumScore]) -> list[SizedPick]:
    """Production-style sizing: equal weight across picks."""
    if not picks:
        return []
    w = 1.0 / len(picks)
    return [SizedPick(p.ticker, p.score, w) for p in picks]


def size_picks_vol_scaled(
    picks: list[MomentumScore],
    per_asset_closes: dict[str, list[float]],
    *,
    vol_window_days: int = 60,
    floor_vol: float = 0.05,
) -> list[SizedPick]:
    """Inverse-volatility sizing: weight each pick by 1/sigma, then
    normalize so weights sum to 1.0.

    `floor_vol` (default 0.05 = 5% annualized) caps the maximum
    weight that a near-zero-vol pick can receive. Without this,
    a temporarily quiet asset can dominate the portfolio.

    Falls back to equal_weight for any pick whose vol can't be
    computed (insufficient history).
    """
    if not picks:
        return []
    inv_vols: list[tuple[MomentumScore, float]] = []
    for p in picks:
        closes = per_asset_closes.get(p.ticker)
        if not closes:
            return size_picks_equal_weight(picks)
        vol = _trailing_volatility(closes, vol_window_days)
        if vol is None:
            return size_picks_equal_weight(picks)
        vol_effective = max(vol, floor_vol)
        inv_vols.append((p, 1.0 / vol_effective))
    total = sum(iv for _, iv in inv_vols)
    if total <= 0:
        return size_picks_equal_weight(picks)
    return [SizedPick(p.ticker, p.score, iv / total) for p, iv in inv_vols]


# ─── Strategy variant catalog ───────────────────────────────────────

VARIANTS: dict[str, dict] = {
    "v1_baseline":           {"ranker": "single_12_1", "sizer": "equal_weight"},
    "v2_volscaled":          {"ranker": "single_12_1", "sizer": "vol_scaled"},
    "v3_multihorizon":       {"ranker": "multi_horizon", "sizer": "equal_weight"},
    "v4_multihorizon_volscaled": {"ranker": "multi_horizon", "sizer": "vol_scaled"},
}
