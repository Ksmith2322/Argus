"""Pure-function PEAD decision logic.

Separated from helio/pead_research.py (the universe-scanning + offline
analysis tooling) so the live runner has a small, testable surface.
PEAD: Post-Earnings Announcement Drift — academic effect documented
since Bernard-Thomas 1989. Stocks that surprise on earnings drift in
the direction of the surprise for ~1-60 days.

Edge thesis (Apollo watchlist evidence + academic literature):
  - Positive earnings surprise (actual EPS > consensus by >=5%)
  - Confirmed by volume (announcement-day volume >= 1.5x 30-day average)
  - Optional: gap-up at next-day open (entry confirmation)
  → Long the stock at next-day open, hold 20 trading days, stop -1.5 ATR.

This module performs no I/O. The runner fetches earnings + price data
and feeds them into evaluate_pead_signal()."""
from __future__ import annotations

from dataclasses import dataclass


SURPRISE_PCT_MIN = 5.0   # consensus literature threshold
VOLUME_RATIO_MIN = 1.5   # announcement-day volume vs 30d avg
GAP_PCT_MIN = 0.0        # require non-negative gap (no gap-down on beat)
HOLD_DAYS = 20
ATR_STOP_MULT = 1.5


@dataclass(frozen=True)
class PEADCandidate:
    """One earnings event being evaluated for entry."""
    ticker: str
    announcement_date: str        # YYYY-MM-DD
    surprise_pct: float
    eps_actual: float | None = None
    eps_estimate: float | None = None
    announce_volume: float | None = None
    avg_volume_30d: float | None = None
    next_day_open: float | None = None
    prior_close: float | None = None
    atr_at_entry: float | None = None  # 14-period ATR on entry day


@dataclass(frozen=True)
class PEADDecision:
    action: str           # "ENTER_LONG" | "SKIP"
    reason: str
    qualifying_surprise: bool
    qualifying_volume: bool
    qualifying_gap: bool


def evaluate_pead_signal(
    c: PEADCandidate,
    *,
    surprise_pct_min: float = SURPRISE_PCT_MIN,
    volume_ratio_min: float = VOLUME_RATIO_MIN,
    gap_pct_min: float = GAP_PCT_MIN,
) -> PEADDecision:
    """Pure-function entry decision. The runner separately handles exit
    logic (20-day hold + -1.5 ATR stop) since that depends on position
    state, not just the announcement event."""
    # Required: positive surprise above threshold
    surprise_ok = c.surprise_pct is not None and c.surprise_pct >= surprise_pct_min

    # Required: confirming volume
    vol_ratio = None
    if c.announce_volume and c.avg_volume_30d and c.avg_volume_30d > 0:
        vol_ratio = c.announce_volume / c.avg_volume_30d
    volume_ok = vol_ratio is not None and vol_ratio >= volume_ratio_min

    # Required: non-negative gap (market confirming the beat)
    gap_pct = None
    if c.next_day_open and c.prior_close and c.prior_close > 0:
        gap_pct = (c.next_day_open / c.prior_close - 1.0) * 100.0
    gap_ok = gap_pct is not None and gap_pct >= gap_pct_min

    if not surprise_ok:
        return PEADDecision(
            "SKIP", f"surprise_below_min_{c.surprise_pct}<{surprise_pct_min}",
            surprise_ok, volume_ok, gap_ok,
        )
    if not volume_ok:
        return PEADDecision(
            "SKIP", f"volume_ratio_below_min_{vol_ratio}<{volume_ratio_min}",
            surprise_ok, volume_ok, gap_ok,
        )
    if not gap_ok:
        return PEADDecision(
            "SKIP", f"gap_negative_{gap_pct}<{gap_pct_min}",
            surprise_ok, volume_ok, gap_ok,
        )

    return PEADDecision(
        "ENTER_LONG",
        f"surprise={c.surprise_pct:.1f}%_vol_ratio={vol_ratio:.2f}_gap={gap_pct:.2f}%",
        surprise_ok, volume_ok, gap_ok,
    )


def should_exit(
    *,
    entry_px: float,
    entry_date_idx: int,
    today_idx: int,
    today_low: float,
    today_close: float,
    atr_at_entry: float,
    hold_days: int = HOLD_DAYS,
    stop_mult: float = ATR_STOP_MULT,
) -> tuple[bool, str]:
    """Position-state exit check. Two reasons to exit:
      - Today's low hit the -stop_mult × ATR stop level.
      - Bars-held >= hold_days (time stop)."""
    stop_px = entry_px - stop_mult * atr_at_entry
    if today_low <= stop_px:
        return True, "atr_stop"
    if (today_idx - entry_date_idx) >= hold_days:
        return True, "hold_period_end"
    return False, ""
