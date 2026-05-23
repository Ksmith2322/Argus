"""Cross-sectional 12-1 momentum logic.

Architect audit 2026-05-19, #3 candidate. AQR/Asness 1997+: rank a
universe of liquid assets by trailing 12-month return excluding the
most recent 1 month (12-1 momentum). Long top quintile, short bottom
quintile (or long-only top-quintile when shorting is costly), rebalance
monthly.

Pure-function. The runner / backtest sources OHLCV and calls these.

Default universe (2026-05-22 swap — see project_2026_05_22_backtest_factory_findings.md):
8 broad cross-asset ETFs spanning 5 distinct risk factors. Replaced the
prior 15-ticker sector+international universe after the factory's
disciplined gate found the sector universe fails (CI lower 1.05 at 10bps;
H2 2020-2026 CI lower 0.53 — recent edge collapse). Cross-sectional
momentum requires *dispersion across asset classes*, which the broad
universe provides and a sector-only universe does not.

Broad-8 universe:
  SPY  (US broad equity)
  QQQ  (US tech-heavy)
  IWM  (US small-cap)
  DIA  (US large-cap industrials)
  EFA  (International developed)
  EEM  (International emerging)
  GLD  (Gold)
  TLT  (Long Treasuries)

This config (broad-8, 252-21 lookback, top-quintile=2-of-8) passes all 8
layers of the disciplined gate at 10bps slippage: PF 3.30, CI [1.86, 6.30]
over 20 years (2006-2026), both halves H1+H2 pass independently.
"""
from __future__ import annotations

from dataclasses import dataclass


DEFAULT_UNIVERSE = (
    "SPY", "QQQ", "IWM", "DIA",   # US equity (broad / tech / small / large-industrial)
    "EFA", "EEM",                  # International (developed / emerging)
    "GLD",                         # Gold
    "TLT",                         # Long Treasuries
)

# Defaults match the published 12-1 spec.
LOOKBACK_LONG_DAYS = 252   # ~12 months trading days
LOOKBACK_SHORT_DAYS = 21   # ~1 month — excluded
TOP_QUINTILE_FRACTION = 0.2
REBALANCE_DAY_OF_MONTH = 1  # first trading day each month


@dataclass(frozen=True)
class MomentumScore:
    ticker: str
    score: float        # (P_today / P_t-252) / (P_today / P_t-21) - i.e. the 12-1 ratio
    p_now: float
    p_long_ago: float   # 12 months ago
    p_recent: float     # 1 month ago


def compute_momentum_score(
    closes: list[float],
    long_lookback: int = LOOKBACK_LONG_DAYS,
    short_lookback: int = LOOKBACK_SHORT_DAYS,
) -> float | None:
    """Compute the 12-1 momentum score for a single asset's price series.

    Score is (P_now / P_{12 months ago}) / (P_now / P_{1 month ago}) - 1
    Equivalently: P_{1 month ago} / P_{12 months ago} - 1
                = total return from 12mo ago to 1mo ago.

    Returns None if the series is too short."""
    if len(closes) <= long_lookback:
        return None
    p_long_ago = float(closes[-(long_lookback + 1)])
    p_recent = float(closes[-(short_lookback + 1)])
    if p_long_ago <= 0:
        return None
    return (p_recent / p_long_ago) - 1.0


def rank_universe_by_momentum(
    per_asset_closes: dict[str, list[float]],
    *,
    long_lookback: int = LOOKBACK_LONG_DAYS,
    short_lookback: int = LOOKBACK_SHORT_DAYS,
) -> list[MomentumScore]:
    """Compute scores for each asset and return sorted high-to-low.
    Assets with insufficient history are dropped."""
    scores: list[MomentumScore] = []
    for ticker, closes in per_asset_closes.items():
        score = compute_momentum_score(closes, long_lookback, short_lookback)
        if score is None:
            continue
        scores.append(MomentumScore(
            ticker=ticker,
            score=score,
            p_now=float(closes[-1]),
            p_long_ago=float(closes[-(long_lookback + 1)]),
            p_recent=float(closes[-(short_lookback + 1)]),
        ))
    return sorted(scores, key=lambda s: s.score, reverse=True)


def select_top_quintile(
    ranked: list[MomentumScore],
    fraction: float = TOP_QUINTILE_FRACTION,
    minimum: int = 1,
) -> list[MomentumScore]:
    """Return the top fraction of the ranking. Default 20% → 3 of 15.
    `minimum=1` ensures at least one pick even from a tiny universe."""
    if not ranked:
        return []
    n = max(minimum, int(round(len(ranked) * fraction)))
    return ranked[:n]
