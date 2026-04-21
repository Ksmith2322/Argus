"""Strategy-agnostic exit policy layer — reference implementation.

Each runner has bespoke exit logic (stop/target/time). This module offers
a composable set of exit policies that any runner can adopt incrementally.
Not wired into runner_unified by default — adopting it is a cohort-reset
decision per strategy (see COHORT_SPEC.md).

Design principles:
  1. Pure functions — take (trade_state, current_bar, params) → ExitDecision.
  2. Stateless between bars (state lives in trade_state dict, persisted by runner).
  3. Composable — stack multiple policies; first to return ExitDecision wins.
  4. Well-tested (argus_flow/tests/test_exit_policy.py — to be added).

Exit policies provided:
  - MFEAwareTrail: lock profit once MFE crosses N*R, trail at M*R below MFE.
  - TimeOfDayExit: force-close at a specific wall-clock hour (e.g., 21:00 UTC
    to avoid overnight carry).
  - RegimeConditionalHold: reduce max_hold_bars in high-volatility regimes,
    extend in trending regimes.

Usage (example for a runner):
    from helio.exit_policy import MFEAwareTrail, TimeOfDayExit

    trail = MFEAwareTrail(lock_at_r=1.0, trail_distance_r=0.5)
    cutoff = TimeOfDayExit(close_at_utc_hour=21)

    for policy in (trail, cutoff):
        decision = policy.evaluate(trade_state, bar)
        if decision.should_exit:
            runner.exit(reason=decision.reason, price=decision.exit_price)
            break
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: str = ""
    exit_price: float | None = None  # None → use bar close


class ExitPolicy(Protocol):
    def evaluate(self, trade_state: dict, bar: dict) -> ExitDecision: ...


@dataclass
class MFEAwareTrail:
    """Once MFE (max favorable excursion) crosses `lock_at_r` in R-multiples,
    move the effective stop to entry + (MFE - `trail_distance_r`) * R.

    Rationale: a trade that goes +2R then back to break-even is a missed
    opportunity. MFE-aware trails lock profit as the trade matures.

    Params:
      lock_at_r: MFE threshold (in R) to activate the trail
      trail_distance_r: how far behind MFE to set the new stop
    """
    lock_at_r: float = 1.0
    trail_distance_r: float = 0.5

    def evaluate(self, trade_state: dict, bar: dict) -> ExitDecision:
        entry_px = trade_state.get("entry_price", 0)
        initial_stop = trade_state.get("initial_stop", 0)
        direction = trade_state.get("direction", "LONG")
        if not entry_px or not initial_stop:
            return ExitDecision(False)
        r = abs(entry_px - initial_stop)
        if r <= 0:
            return ExitDecision(False)

        high = bar.get("high", bar.get("High"))
        low = bar.get("low", bar.get("Low"))
        close = bar.get("close", bar.get("Close"))
        if None in (high, low, close):
            return ExitDecision(False)

        # Evaluate trail HIT against the prior bar's trailing_stop before
        # updating MFE on this bar. Otherwise the bar's low/high that set the
        # new stop gets checked against itself (intrabar ordering unknown).
        prior_trail = trade_state.get("trailing_stop")

        if direction.upper() == "LONG":
            if prior_trail is not None and low <= prior_trail:
                return ExitDecision(True, reason="mfe_trail_hit", exit_price=prior_trail)
            mfe_px = max(trade_state.get("mfe_price", entry_px), high)
            mfe_r = (mfe_px - entry_px) / r
            trade_state["mfe_price"] = mfe_px
            if mfe_r < self.lock_at_r:
                return ExitDecision(False)
            new_stop = mfe_px - self.trail_distance_r * r
            trade_state["trailing_stop"] = max(trade_state.get("trailing_stop", initial_stop), new_stop)
        else:  # SHORT
            if prior_trail is not None and high >= prior_trail:
                return ExitDecision(True, reason="mfe_trail_hit", exit_price=prior_trail)
            mfe_px = min(trade_state.get("mfe_price", entry_px), low)
            mfe_r = (entry_px - mfe_px) / r
            trade_state["mfe_price"] = mfe_px
            if mfe_r < self.lock_at_r:
                return ExitDecision(False)
            new_stop = mfe_px + self.trail_distance_r * r
            trade_state["trailing_stop"] = min(trade_state.get("trailing_stop", initial_stop), new_stop)
        return ExitDecision(False)


@dataclass
class TimeOfDayExit:
    """Force-close any open position when the bar's timestamp hour crosses
    `close_at_utc_hour`. Useful for strategies that want to avoid overnight
    carry or session-transition volatility.

    Param:
      close_at_utc_hour: UTC hour (0-23). Exit on the first bar whose hour
                        is >= this value (within the same calendar day).
    """
    close_at_utc_hour: int = 21

    def evaluate(self, trade_state: dict, bar: dict) -> ExitDecision:
        ts = bar.get("ts") or bar.get("timestamp")
        if not ts:
            return ExitDecision(False)
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                return ExitDecision(False)
        if ts.hour >= self.close_at_utc_hour:
            close_px = bar.get("close", bar.get("Close"))
            return ExitDecision(True, reason="time_of_day_cutoff", exit_price=close_px)
        return ExitDecision(False)


@dataclass
class RegimeConditionalHold:
    """Modulate the effective max_hold_bars based on detected regime.

    In CHOPPY regimes, close faster (edge decays). In TRENDING regimes,
    extend the hold (let runners run). Regime is read from bar["regime"]
    if present (set upstream by the runner's regime detector).

    Params:
      base_max_bars: default hold limit
      choppy_multiplier: factor applied to base when regime == "CHOPPY"
      trending_multiplier: factor applied to base when regime == "TRENDING"
    """
    base_max_bars: int = 20
    choppy_multiplier: float = 0.5
    trending_multiplier: float = 1.5

    def evaluate(self, trade_state: dict, bar: dict) -> ExitDecision:
        bars_held = trade_state.get("bars_held", 0)
        regime = (bar.get("regime") or trade_state.get("entry_regime") or "").upper()
        if regime == "CHOPPY":
            limit = int(self.base_max_bars * self.choppy_multiplier)
        elif regime in ("TRENDING", "TREND"):
            limit = int(self.base_max_bars * self.trending_multiplier)
        else:
            limit = self.base_max_bars
        if bars_held >= limit:
            close_px = bar.get("close", bar.get("Close"))
            return ExitDecision(True, reason=f"regime_hold_limit({regime or 'UNKNOWN'}:{limit})",
                                exit_price=close_px)
        return ExitDecision(False)


def evaluate_chain(policies: list, trade_state: dict, bar: dict) -> ExitDecision:
    """Run policies in order; first to fire wins."""
    for p in policies:
        d = p.evaluate(trade_state, bar)
        if d.should_exit:
            return d
    return ExitDecision(False)


# ------------------ Self-test ------------------
if __name__ == "__main__":
    # Quick smoke test
    ts = {"entry_price": 100, "initial_stop": 99, "direction": "LONG", "bars_held": 0}
    trail = MFEAwareTrail(lock_at_r=1.0, trail_distance_r=0.5)
    # Bar 1: price ramps to 101.5 (+1.5R), trail arms at 100.75
    b1 = {"High": 101.5, "Low": 100.5, "Close": 101.4}
    print("bar1:", trail.evaluate(ts, b1))
    print("  trailing_stop:", ts.get("trailing_stop"))
    # Bar 2: price pulls to 100.7, below 100.75 trail → exit
    b2 = {"High": 100.9, "Low": 100.7, "Close": 100.75}
    print("bar2:", trail.evaluate(ts, b2))
