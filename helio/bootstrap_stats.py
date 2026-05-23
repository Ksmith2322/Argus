"""Bootstrap + Wilson CI helpers for strategy statistics.

The Skeptic's complaint about the 7/1 readiness gate was specifically:
"at n=20 with WR=60%, the 95% Wilson CI is roughly [36%, 81%] — the
data cannot distinguish a true edge from a coin flip at any useful
confidence." This module makes that test computable, so every PF /
win-rate claim can come with the honest CI attached."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class WilsonCI:
    """Wilson score interval for a binomial proportion (e.g. win rate)."""
    point: float
    lower: float
    upper: float
    n: int
    confidence: float


def wilson_ci(wins: int, n: int, confidence: float = 0.95) -> WilsonCI:
    """Wilson score interval — preferred over normal approximation for
    small n or proportions near 0/1.

    See Brown, Cai, DasGupta (2001) — "Interval Estimation for a Binomial
    Proportion" for why this beats the textbook normal approximation."""
    if n <= 0:
        return WilsonCI(0.0, 0.0, 0.0, 0, confidence)
    p = wins / n
    z = _normal_inverse_cdf(1 - (1 - confidence) / 2)
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    lo = max(0.0, center - half)
    hi = min(1.0, center + half)
    return WilsonCI(point=p, lower=lo, upper=hi, n=n, confidence=confidence)


def _normal_inverse_cdf(p: float) -> float:
    """Inverse standard normal CDF (z-score for p). Beasley-Springer-Moro
    approximation; accurate to ~7 decimals for p in (0.001, 0.999)."""
    # For our use (p around 0.975/0.95), the standard tables suffice.
    # Hardcode common confidence levels to avoid the full Acklam impl.
    if abs(p - 0.975) < 1e-6:
        return 1.959964
    if abs(p - 0.95) < 1e-6:
        return 1.644854
    if abs(p - 0.995) < 1e-6:
        return 2.575829
    # Acklam approximation for general p
    # Coefficients in rational approximations
    a = (-3.969683028665376e+01, 2.209460984245205e+02,
         -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02,
         -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01,
         2.445134137142996e+00, 3.754408661907416e+00)
    p_low = 0.02425
    p_high = 1 - p_low
    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
            ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


@dataclass(frozen=True)
class BootstrapResult:
    """One bootstrap-CI summary on a sample statistic (e.g. PF)."""
    point: float
    mean: float
    ci_lower: float
    ci_upper: float
    n_resamples: int
    n_sample: int
    confidence: float


def _profit_factor(pnls: list[float]) -> float:
    wins = sum(p for p in pnls if p > 0)
    losses = abs(sum(p for p in pnls if p < 0))
    if losses == 0:
        return float("inf") if wins > 0 else 0.0
    return wins / losses


def bootstrap_profit_factor(
    pnls: list[float],
    *,
    n_resamples: int = 5000,
    confidence: float = 0.95,
    seed: int | None = 42,
) -> BootstrapResult:
    """Bootstrap CI on the profit factor of a trade series.

    Resamples WITH REPLACEMENT from the trade-PnL list, computes PF on
    each resample, returns the empirical CI at the chosen confidence.
    `seed` makes the result deterministic for tests + reports."""
    if not pnls:
        return BootstrapResult(0.0, 0.0, 0.0, 0.0, 0, 0, confidence)
    rng = random.Random(seed)
    n = len(pnls)
    point = _profit_factor(pnls)
    samples: list[float] = []
    for _ in range(n_resamples):
        resample = [pnls[rng.randrange(n)] for _ in range(n)]
        pf = _profit_factor(resample)
        if math.isfinite(pf):
            samples.append(pf)
    if not samples:
        return BootstrapResult(point, point, point, point, n_resamples, n, confidence)
    samples.sort()
    alpha = (1 - confidence) / 2
    lo_idx = max(0, int(len(samples) * alpha))
    hi_idx = min(len(samples) - 1, int(len(samples) * (1 - alpha)))
    return BootstrapResult(
        point=point,
        mean=sum(samples) / len(samples),
        ci_lower=samples[lo_idx],
        ci_upper=samples[hi_idx],
        n_resamples=n_resamples,
        n_sample=n,
        confidence=confidence,
    )


def block_bootstrap_profit_factor(
    pnls: list[float],
    *,
    block_size: int = 5,
    n_resamples: int = 5000,
    confidence: float = 0.95,
    seed: int | None = 42,
) -> BootstrapResult:
    """Bootstrap PF CI using moving-block resampling.

    IID bootstrap (bootstrap_profit_factor) assumes each trade is
    independent. That's wrong for momentum / trend-following strategies
    where wins and losses cluster in regime blocks (5+ wins in a row
    during a sustained trend; 3 losses in a row during a regime change).

    Moving-block bootstrap resamples consecutive runs of `block_size`
    trades, preserving the within-block correlation structure. This
    typically WIDENS the CI vs IID — a more honest read.

    Recommended block_size: ~sqrt(n) to ~n^(1/3). For n=200 that's
    7-15 bars. Default 5 is a reasonable mid-range default; for known
    high-autocorrelation strategies, use larger blocks.

    See Politis & Romano (1994) for the moving-block bootstrap method.
    """
    if not pnls or block_size < 1:
        return BootstrapResult(0.0, 0.0, 0.0, 0.0, 0, 0, confidence)
    rng = random.Random(seed)
    n = len(pnls)
    if block_size >= n:
        # Block size exceeds sample — fall back to IID
        return bootstrap_profit_factor(
            pnls, n_resamples=n_resamples, confidence=confidence, seed=seed,
        )
    point = _profit_factor(pnls)
    # Number of blocks to glue together to reach length n
    n_blocks = (n + block_size - 1) // block_size
    samples: list[float] = []
    for _ in range(n_resamples):
        resample: list[float] = []
        for _b in range(n_blocks):
            start = rng.randrange(n - block_size + 1)
            resample.extend(pnls[start:start + block_size])
        resample = resample[:n]  # trim to exactly n
        pf = _profit_factor(resample)
        if math.isfinite(pf):
            samples.append(pf)
    if not samples:
        return BootstrapResult(point, point, point, point, n_resamples, n, confidence)
    samples.sort()
    alpha = (1 - confidence) / 2
    lo_idx = max(0, int(len(samples) * alpha))
    hi_idx = min(len(samples) - 1, int(len(samples) * (1 - alpha)))
    return BootstrapResult(
        point=point,
        mean=sum(samples) / len(samples),
        ci_lower=samples[lo_idx],
        ci_upper=samples[hi_idx],
        n_resamples=n_resamples,
        n_sample=n,
        confidence=confidence,
    )


def bootstrap_cagr(
    pnls_pct: list[float],
    *,
    position_size_fraction: float = 1.0,
    bars_per_year: float = 12.0,  # trades per year — caller knows
    n_resamples: int = 5000,
    confidence: float = 0.95,
    seed: int | None = 42,
) -> BootstrapResult:
    """Bootstrap CI on CAGR computed from a series of percentage returns.

    Caller specifies `bars_per_year` so we can annualize the compounded
    return regardless of trade frequency."""
    if not pnls_pct:
        return BootstrapResult(0.0, 0.0, 0.0, 0.0, 0, 0, confidence)
    rng = random.Random(seed)
    n = len(pnls_pct)

    def _cagr(returns: list[float]) -> float:
        eq = 1.0
        for r in returns:
            eq *= (1.0 + r / 100.0 * position_size_fraction)
        if eq <= 0:
            return -1.0
        years = len(returns) / bars_per_year
        if years <= 0:
            return 0.0
        return (eq ** (1.0 / years) - 1.0) * 100.0

    point = _cagr(pnls_pct)
    samples: list[float] = []
    for _ in range(n_resamples):
        resample = [pnls_pct[rng.randrange(n)] for _ in range(n)]
        samples.append(_cagr(resample))
    samples.sort()
    alpha = (1 - confidence) / 2
    lo_idx = max(0, int(len(samples) * alpha))
    hi_idx = min(len(samples) - 1, int(len(samples) * (1 - alpha)))
    return BootstrapResult(
        point=point,
        mean=sum(samples) / len(samples),
        ci_lower=samples[lo_idx],
        ci_upper=samples[hi_idx],
        n_resamples=n_resamples,
        n_sample=n,
        confidence=confidence,
    )
