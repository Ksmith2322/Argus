"""Pure-function stats for the ROI proof engine.

These functions take in trade PnL series or daily returns and produce
risk-adjusted measurements. No I/O, no module-level state, no broker
calls — all testable in isolation.

The companion modules:

* :mod:`helio.spy_benchmark` — SPY-over-strategy-active-timestamps comparison.
* :mod:`ops.audit.run_strategy_roi_proof` — orchestration + CSV outputs.

Design contract:

* Functions REFUSE to compute when sample size is below a defensible
  threshold. They return ``InsufficientSample`` rather than producing a
  number that will be over-interpreted. Per
  ``project_decisive_test_protocol.md``: 30 trades = continuation gate,
  75-100 = serious answer, 138+ = conviction.
* Bootstrap confidence intervals are reported alongside point estimates.
  A 0.45 win rate at n=20 is a wider CI than at n=200 — the engine's
  job is to surface that uncertainty, not hide it.
* Risk-adjusted ratios (Sortino, IR) use period-aware annualization. A
  weekly-frequency strategy uses ``periods_per_year=52`` etc.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence


# ---------------------------------------------------------------------------
# Sample-size discipline
# ---------------------------------------------------------------------------

SMOKE_N = 10          # smoke-test floor — IR with low_sample tag at this n
CONTINUATION_N = 30   # below this, no serious metric is reported
SERIOUS_N = 75        # below this, "conviction" claims are blocked
CONVICTION_N = 138    # at-or-above, full grading is allowed


@dataclass(frozen=True)
class InsufficientSample:
    """Returned when n is below the requested gate. Explicit non-numeric
    so callers cannot accidentally treat it as a metric."""

    actual_n: int
    required_n: int
    note: str = ""


# ---------------------------------------------------------------------------
# Annualized return + bootstrap CI
# ---------------------------------------------------------------------------

def total_return(pnls: Sequence[float], deployed_capital_usd: float) -> float:
    """Total return on deployed capital across the trade series."""
    if deployed_capital_usd <= 0:
        return 0.0
    return float(sum(pnls)) / float(deployed_capital_usd)


def annualized_return(
    pnls: Sequence[float],
    deployed_capital_usd: float,
    days_observed: float,
) -> float:
    """Annualize the return on deployed capital using actual observation
    window. Uses simple (non-compounded) annualization since we typically
    observe weeks, not multi-year cycles."""
    if days_observed <= 0 or deployed_capital_usd <= 0:
        return 0.0
    period_return = total_return(pnls, deployed_capital_usd)
    return period_return * (365.0 / float(days_observed))


def bootstrap_ci(
    values: Sequence[float],
    statistic_fn: Callable[[Sequence[float]], float],
    n_resamples: int = 1000,
    ci: float = 0.95,
    seed: int | None = 42,
) -> tuple[float, float]:
    """Bootstrap confidence interval for an arbitrary statistic.

    Resamples ``values`` with replacement ``n_resamples`` times, applies
    ``statistic_fn`` to each resample, and returns the (lower, upper)
    bounds of the requested CI. Useful for putting a confidence band
    around an annualized-return point estimate when n is small.
    """
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    stats: list[float] = []
    n = len(values)
    for _ in range(n_resamples):
        sample = [values[rng.randint(0, n - 1)] for _ in range(n)]
        try:
            stats.append(float(statistic_fn(sample)))
        except (ValueError, ZeroDivisionError, OverflowError):
            continue
    if not stats:
        return (0.0, 0.0)
    stats.sort()
    alpha = (1.0 - ci) / 2.0
    lo_idx = int(alpha * len(stats))
    hi_idx = int((1.0 - alpha) * len(stats)) - 1
    hi_idx = max(hi_idx, lo_idx)
    return (stats[lo_idx], stats[hi_idx])


# ---------------------------------------------------------------------------
# Drawdown
# ---------------------------------------------------------------------------

def equity_curve(pnls: Sequence[float], starting_equity: float = 0.0) -> list[float]:
    """Cumulative equity from a sequence of trade PnLs."""
    eq: list[float] = []
    running = float(starting_equity)
    for p in pnls:
        running += float(p)
        eq.append(running)
    return eq


def max_drawdown(pnls: Sequence[float], starting_equity: float = 0.0) -> float:
    """Maximum peak-to-trough drawdown in dollars across the equity curve.
    Returns a non-negative float (the magnitude of the largest drawdown)."""
    if not pnls:
        return 0.0
    peak = starting_equity
    max_dd = 0.0
    running = float(starting_equity)
    for p in pnls:
        running += float(p)
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
    return max_dd


def return_over_drawdown(pnls: Sequence[float], starting_equity: float = 0.0) -> float:
    """Total PnL divided by max drawdown. A risk-adjusted ratio: how much
    profit per unit of drawdown experienced. Returns 0 if the strategy
    had no drawdown (typically because n is tiny and all wins)."""
    total = float(sum(pnls))
    dd = max_drawdown(pnls, starting_equity)
    if dd <= 0:
        return 0.0
    return total / dd


# ---------------------------------------------------------------------------
# Sortino + Information Ratio
# ---------------------------------------------------------------------------

def _mean(xs: Sequence[float]) -> float:
    if not xs:
        return 0.0
    return sum(xs) / len(xs)


def _stdev(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def downside_deviation(returns: Sequence[float], target: float = 0.0) -> float:
    """Std-dev of returns BELOW a target (typically zero or the risk-free
    rate). Sortino's denominator. Less harshly punishes upside variance
    than vanilla stdev."""
    if not returns:
        return 0.0
    below = [r - target for r in returns if r < target]
    if not below:
        return 0.0
    return math.sqrt(sum(r * r for r in below) / len(returns))


def sortino_ratio(
    returns: Sequence[float],
    target: float = 0.0,
    periods_per_year: int = 252,
    min_n: int = CONTINUATION_N,
) -> float | InsufficientSample:
    """Annualized Sortino. ``returns`` should be per-period (per-trade or
    per-day) returns expressed as a fraction of capital.

    ``min_n`` defaults to ``CONTINUATION_N`` (30); callers wanting a
    low-sample read for the smoke-test stage can pass ``SMOKE_N`` (10),
    but must flag the result downstream so it isn't treated as a serious
    metric.
    """
    if len(returns) < min_n:
        return InsufficientSample(actual_n=len(returns), required_n=min_n,
                                   note="sortino blocked below min_n")
    excess = [r - target for r in returns]
    dd = downside_deviation(returns, target)
    if dd <= 0:
        return 0.0
    return (_mean(excess) / dd) * math.sqrt(periods_per_year)


def information_ratio(
    strategy_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    periods_per_year: int = 252,
    min_n: int = CONTINUATION_N,
) -> float | InsufficientSample:
    """Annualized Information Ratio: active return per unit of tracking
    error. Positive IR means the strategy adds value over the benchmark
    on a risk-adjusted basis, *measured over the same windows*.

    ``min_n`` defaults to ``CONTINUATION_N`` (30). Pass ``SMOKE_N`` (10)
    only when the consumer will treat the result as a low-sample
    indicator suitable for the capital ladder's smoke stage, not as a
    serious promotion metric.
    """
    if len(strategy_returns) != len(benchmark_returns):
        raise ValueError(
            f"length mismatch: strategy={len(strategy_returns)} vs "
            f"benchmark={len(benchmark_returns)}"
        )
    if len(strategy_returns) < min_n:
        return InsufficientSample(actual_n=len(strategy_returns), required_n=min_n,
                                   note="IR blocked below min_n")
    active = [s - b for s, b in zip(strategy_returns, benchmark_returns)]
    te = _stdev(active)
    if te <= 0:
        return 0.0
    return (_mean(active) / te) * math.sqrt(periods_per_year)


# ---------------------------------------------------------------------------
# Trade-removed stress test
# ---------------------------------------------------------------------------

def trade_removed_stress(pnls: Sequence[float]) -> dict[str, float | int]:
    """Recompute net PnL after removing the best 1, best 3, worst 1, worst 3
    trades. Surfaces strategies whose edge is one-trade-luck.

    Returns a dict; keys ``net_remove_best_1`` / ``net_remove_best_3`` /
    ``net_remove_worst_1`` / ``net_remove_worst_3`` / ``concentration_score``
    (= remove_best_1 / total_pnl, the % of profit attributable to the
    single best trade — values close to 1.0 mean fragile).
    """
    out: dict[str, float | int] = {"n": len(pnls)}
    if not pnls:
        return out
    sorted_pnls = sorted(pnls)
    total = float(sum(pnls))
    best_1 = max(pnls)
    worst_1 = min(pnls)
    out["total_pnl"] = total
    out["net_remove_best_1"] = total - best_1
    out["net_remove_worst_1"] = total - worst_1
    out["net_remove_best_3"] = total - sum(sorted_pnls[-3:]) if len(pnls) >= 3 else total
    out["net_remove_worst_3"] = total - sum(sorted_pnls[:3]) if len(pnls) >= 3 else total
    if total > 0:
        out["concentration_score"] = round((total - out["net_remove_best_1"]) / total, 4)
    else:
        out["concentration_score"] = 0.0
    return out


# ---------------------------------------------------------------------------
# Friction model
# ---------------------------------------------------------------------------

# Per-trade friction estimates by asset class.
# Commission is round-trip (entry + exit); slippage is a fraction of notional
# applied once per trade as a proxy for spread + execution.
DEFAULT_FRICTION = {
    "stock":         {"commission_per_trade_usd": 1.30, "slippage_pct": 0.0008},
    "etf":           {"commission_per_trade_usd": 1.30, "slippage_pct": 0.0006},
    "leveraged_etf": {"commission_per_trade_usd": 1.30, "slippage_pct": 0.0015},
    "future":        {"commission_per_trade_usd": 4.50, "slippage_pct": 0.0001},
    "micro_future":  {"commission_per_trade_usd": 1.10, "slippage_pct": 0.0001},
    "forex":         {"commission_per_trade_usd": 0.00, "slippage_pct": 0.00012},
}


def friction_adjusted_pnls(
    pnls: Sequence[float],
    notionals: Sequence[float],
    asset_class: str,
    overrides: dict | None = None,
) -> list[float]:
    """Subtract estimated friction from each trade's gross PnL. Overrides
    let callers supply broker-specific commission/slippage. Notionals are
    per-trade (entry-side) USD-equivalents."""
    if len(pnls) != len(notionals):
        raise ValueError(
            f"pnls and notionals length mismatch: {len(pnls)} vs {len(notionals)}"
        )
    cfg = dict(DEFAULT_FRICTION.get(asset_class, DEFAULT_FRICTION["stock"]))
    if overrides:
        cfg.update(overrides)
    commission = float(cfg.get("commission_per_trade_usd", 0.0))
    slippage_pct = float(cfg.get("slippage_pct", 0.0))
    return [
        float(p) - commission - abs(float(n)) * slippage_pct
        for p, n in zip(pnls, notionals)
    ]


__all__ = [
    "SMOKE_N",
    "CONTINUATION_N",
    "SERIOUS_N",
    "CONVICTION_N",
    "InsufficientSample",
    "total_return",
    "annualized_return",
    "bootstrap_ci",
    "equity_curve",
    "max_drawdown",
    "return_over_drawdown",
    "downside_deviation",
    "sortino_ratio",
    "information_ratio",
    "trade_removed_stress",
    "DEFAULT_FRICTION",
    "friction_adjusted_pnls",
]
