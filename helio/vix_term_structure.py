"""VIX term-structure helper for the forge_vix_carry strategy.

The retail-accessible proxy for VIX1/VIX2 futures term-structure is:
  - ^VIX     — spot 30-day implied vol on SPX
  - ^VIX3M   — 90-day implied vol on SPX (CBOE 3-month index)

If VIX3M > VIX, the term structure is in CONTANGO (further-dated vol
priced higher than near-dated). This is the normal state ~80% of the
time and is what enables SVXY to capture carry (rolling down the curve).

If VIX3M < VIX, the structure is in BACKWARDATION — near-dated vol is
elevated by a current event. Historically a leading signal for vol
spikes and tail risk; SVXY positions should exit on backwardation.

The strategy reads these two indices, computes the ratio, and combines
with SPY realized-vol + VIX-level context to decide whether to hold
SVXY (short-vol exposure via inverse ETF).

This module performs network I/O when called. The forge runner caches
its results on disk so backtest mode reads from cache, not yfinance."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class VIXTermStructure:
    """Snapshot of VIX term structure at a point in time."""
    as_of: datetime
    vix: float           # spot 30-day
    vix3m: float         # 90-day
    contango_ratio: float  # vix3m / vix; >1 = contango

    @property
    def in_contango(self) -> bool:
        return self.contango_ratio > 1.0

    @property
    def in_backwardation(self) -> bool:
        return self.contango_ratio < 1.0


def compute_term_structure(vix_close: float, vix3m_close: float, as_of: datetime | None = None) -> VIXTermStructure:
    """Pure-function constructor. The vix_carry runner / backtest layer
    is responsible for sourcing the closes (live = yfinance, backtest = csv).
    Keeps the math testable without any I/O."""
    as_of = as_of or datetime.now(timezone.utc)
    if vix_close <= 0:
        raise ValueError(f"vix_close must be positive, got {vix_close!r}")
    ratio = float(vix3m_close) / float(vix_close)
    return VIXTermStructure(
        as_of=as_of,
        vix=float(vix_close),
        vix3m=float(vix3m_close),
        contango_ratio=ratio,
    )


def realized_vol_annualized(closes: list[float]) -> float:
    """Annualized realized vol from a window of daily closes. Returns
    pct (e.g. 15.5 = 15.5%). Needs at least 3 closes to be meaningful."""
    if len(closes) < 3:
        return 0.0
    import math
    rets = []
    for i in range(1, len(closes)):
        prev = float(closes[i - 1])
        curr = float(closes[i])
        if prev <= 0:
            continue
        rets.append(math.log(curr / prev))
    if not rets:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    std = math.sqrt(var)
    # 252 trading days * 100 to get % annualized
    return std * math.sqrt(252) * 100.0


@dataclass(frozen=True)
class CarrySignal:
    """Decision output from the strategy's entry-check logic."""
    action: str           # "ENTER_LONG_SVXY" | "EXIT" | "HOLD" | "WAIT"
    reason: str
    contango_ratio: float
    vix_level: float
    spy_realized_vol_pct: float


def evaluate_carry_signal(
    term_structure: VIXTermStructure,
    spy_realized_vol_pct: float,
    *,
    has_open_position: bool,
    entry_contango_min: float = 1.05,
    entry_vix_max: float = 20.0,
    entry_realized_vol_max: float = 15.0,
    exit_contango_max: float = 1.00,
    exit_vix_min: float = 25.0,
) -> CarrySignal:
    """Pure-function strategy logic. The runner calls this with current
    inputs and acts on the action label.

    ENTRY criteria (all three must hold):
      - Term structure in contango at >= entry_contango_min (default 5%)
      - VIX spot below entry_vix_max (default 20 — calm regime)
      - SPY 5-day realized vol below entry_realized_vol_max (default 15%)
    Rationale: short-vol works in calm-and-contango. Backwardation or
    elevated vol levels mean a vol spike is either imminent or in
    progress; reward-to-risk is poor.

    EXIT criteria (any one triggers):
      - Term structure flips to or below exit_contango_max (default 1.0).
      - VIX spot rises to >= exit_vix_min (default 25 — regime shift).
    Note: a hard -8% stop on the SVXY position is enforced separately at
    the position-manager layer (so this function is pure / data-driven).
    """
    ratio = term_structure.contango_ratio
    vix = term_structure.vix
    rv = spy_realized_vol_pct

    if has_open_position:
        if ratio <= exit_contango_max:
            return CarrySignal(
                action="EXIT",
                reason=f"contango_collapsed_to_{ratio:.3f}",
                contango_ratio=ratio, vix_level=vix, spy_realized_vol_pct=rv,
            )
        if vix >= exit_vix_min:
            return CarrySignal(
                action="EXIT",
                reason=f"vix_spike_to_{vix:.1f}",
                contango_ratio=ratio, vix_level=vix, spy_realized_vol_pct=rv,
            )
        return CarrySignal(
            action="HOLD",
            reason="holding_in_contango",
            contango_ratio=ratio, vix_level=vix, spy_realized_vol_pct=rv,
        )

    # Flat position — evaluate entry.
    if ratio < entry_contango_min:
        return CarrySignal(
            action="WAIT",
            reason=f"contango_insufficient_{ratio:.3f}<{entry_contango_min}",
            contango_ratio=ratio, vix_level=vix, spy_realized_vol_pct=rv,
        )
    if vix > entry_vix_max:
        return CarrySignal(
            action="WAIT",
            reason=f"vix_too_high_{vix:.1f}>{entry_vix_max}",
            contango_ratio=ratio, vix_level=vix, spy_realized_vol_pct=rv,
        )
    if rv > entry_realized_vol_max:
        return CarrySignal(
            action="WAIT",
            reason=f"realized_vol_too_high_{rv:.1f}>{entry_realized_vol_max}",
            contango_ratio=ratio, vix_level=vix, spy_realized_vol_pct=rv,
        )

    return CarrySignal(
        action="ENTER_LONG_SVXY",
        reason=f"contango={ratio:.3f}_vix={vix:.1f}_rv={rv:.1f}",
        contango_ratio=ratio, vix_level=vix, spy_realized_vol_pct=rv,
    )
