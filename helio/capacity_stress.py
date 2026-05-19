"""Capacity / margin stress test (Codex audit 2026-05-18 fix-by-5/31 #8).

For each strategy, simulate scaling its notional by [1x, 2x, 5x, 10x] and
report which cap layer trips at each level. Outputs a maximum safe scale
factor per strategy — useful for promotion review (a strategy with 5x
headroom has room to grow; one tripping caps at 1x has none).

This is read-only: nothing is submitted, nothing on disk is changed
except optional output files.

Cap layers checked (all from helio/cluster_exposure.py + fleet_sizing.json):
  1. Per-strategy notional cap (PER_STRATEGY_NOTIONAL_CAP_X)
  2. Asset-class single-instrument cap (SINGLE_INSTRUMENT_CAP_X /
     FUTURES_SINGLE_INSTRUMENT_CAP_X / FX_SINGLE_INSTRUMENT_CAP_X)
  3. Cluster caps (CLUSTER_CAPS — FX_USD / EQUITY_BETA / METALS / etc.)
  4. Fleet total notional cap (TOTAL_NOTIONAL_CAP_X)
  5. Fleet maintenance-margin cap (MAX_FLEET_MAINT_MARGIN_PCT)
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

from helio.cluster_exposure import (
    CLUSTER_CAPS,
    CLUSTER_MAP,
    FUTURES_SINGLE_INSTRUMENT_CAP_X,
    FX_SINGLE_INSTRUMENT_CAP_X,
    FX_SYMBOLS,
    FUTURES_SYMBOLS,
    MAINT_MARGIN_RATIOS,
    MAX_FLEET_MAINT_MARGIN_PCT,
    PER_STRATEGY_DEFAULT_CAP,
    PER_STRATEGY_NOTIONAL_CAP_X,
    SINGLE_INSTRUMENT_CAP_X,
    TOTAL_NOTIONAL_CAP_X,
    _maint_margin_ratio,
)

DEFAULT_MULTIPLIERS: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0)


@dataclass(frozen=True)
class CapBreach:
    cap_layer: str        # "PER_STRATEGY" | "SINGLE_INSTRUMENT" | "CLUSTER:..." | "TOTAL_NOTIONAL" | "MAINT_MARGIN"
    cap_usd: float
    proposed_usd: float


@dataclass(frozen=True)
class StressLevel:
    multiplier: float
    proposed_notional_usd: float
    breaches: tuple[CapBreach, ...]

    @property
    def passed(self) -> bool:
        return not self.breaches


def _single_instrument_cap_x(symbol: str) -> float:
    s = symbol.upper()
    if s in FUTURES_SYMBOLS:
        return FUTURES_SINGLE_INSTRUMENT_CAP_X
    if s in FX_SYMBOLS:
        return FX_SINGLE_INSTRUMENT_CAP_X
    return SINGLE_INSTRUMENT_CAP_X


def _check_level(
    *,
    strategy_label: str,
    symbol: str,
    direction: str,
    proposed_notional: float,
    anchor_usd: float,
    existing_cluster_exposure_usd: Optional[dict[str, float]] = None,
    existing_total_notional_usd: float = 0.0,
    existing_fleet_margin_usd: float = 0.0,
) -> tuple[CapBreach, ...]:
    """Evaluate every cap against `proposed_notional`. Returns a tuple of
    breaches; empty tuple means the level passes.

    `existing_*` params describe the rest of the fleet at evaluation time
    (so a 5× scale of strategy X is checked against current exposure for
    strategies Y, Z, etc.). Default zero treats this strategy as isolated."""
    breaches: list[CapBreach] = []

    # 1. Per-strategy
    per_strat_x = PER_STRATEGY_NOTIONAL_CAP_X.get(strategy_label, PER_STRATEGY_DEFAULT_CAP)
    per_strat_cap = per_strat_x * anchor_usd
    if per_strat_cap > 0 and proposed_notional > per_strat_cap:
        breaches.append(CapBreach("PER_STRATEGY", per_strat_cap, proposed_notional))

    # 2. Single instrument
    sym_cap = _single_instrument_cap_x(symbol) * anchor_usd
    if proposed_notional > sym_cap:
        breaches.append(CapBreach("SINGLE_INSTRUMENT", sym_cap, proposed_notional))

    # 3. Cluster caps
    clusters = (CLUSTER_MAP.get(symbol.upper(), {}) or {}).get(direction.lower(), [])
    for c in clusters:
        c_cap = CLUSTER_CAPS.get(c, float("inf")) * anchor_usd
        existing = (existing_cluster_exposure_usd or {}).get(c, 0.0)
        if existing + proposed_notional > c_cap:
            breaches.append(CapBreach(f"CLUSTER:{c}", c_cap, existing + proposed_notional))

    # 4. Total notional
    total_cap = TOTAL_NOTIONAL_CAP_X * anchor_usd
    if existing_total_notional_usd + proposed_notional > total_cap:
        breaches.append(CapBreach(
            "TOTAL_NOTIONAL", total_cap, existing_total_notional_usd + proposed_notional,
        ))

    # 5. Fleet maintenance margin
    proposed_margin = proposed_notional * _maint_margin_ratio(symbol)
    total_margin_after = existing_fleet_margin_usd + proposed_margin
    margin_cap = MAX_FLEET_MAINT_MARGIN_PCT * anchor_usd
    if total_margin_after > margin_cap:
        breaches.append(CapBreach("MAINT_MARGIN", margin_cap, total_margin_after))

    return tuple(breaches)


def stress_strategy(
    *,
    strategy_label: str,
    symbol: str,
    direction: str,
    current_notional_usd: float,
    anchor_usd: float,
    multipliers: tuple[float, ...] = DEFAULT_MULTIPLIERS,
    existing_cluster_exposure_usd: Optional[dict[str, float]] = None,
    existing_total_notional_usd: float = 0.0,
    existing_fleet_margin_usd: float = 0.0,
) -> dict:
    """Return per-multiplier breach report for one strategy.

    Output shape::

        {
          "strategy": "forge_gld_pm_long",
          "symbol": "GLD",
          "direction": "long",
          "anchor_usd": 30000.0,
          "current_notional_usd": 12000.0,
          "levels": [
            {"multiplier": 1.0, "proposed_notional_usd": 12000.0, "breaches": [], "passed": true},
            {"multiplier": 2.0, "proposed_notional_usd": 24000.0,
             "breaches": [{"cap_layer": "PER_STRATEGY", "cap_usd": 12000.0, ...}],
             "passed": false},
            ...
          ],
          "max_safe_multiplier": 1.0,
        }"""
    levels: list[StressLevel] = []
    for m in multipliers:
        proposed = float(current_notional_usd) * float(m)
        br = _check_level(
            strategy_label=strategy_label,
            symbol=symbol,
            direction=direction,
            proposed_notional=proposed,
            anchor_usd=anchor_usd,
            existing_cluster_exposure_usd=existing_cluster_exposure_usd,
            existing_total_notional_usd=existing_total_notional_usd,
            existing_fleet_margin_usd=existing_fleet_margin_usd,
        )
        levels.append(StressLevel(
            multiplier=float(m),
            proposed_notional_usd=proposed,
            breaches=br,
        ))

    max_safe = max(
        (lvl.multiplier for lvl in levels if lvl.passed),
        default=0.0,
    )

    return {
        "strategy": strategy_label,
        "symbol": symbol,
        "direction": direction,
        "anchor_usd": float(anchor_usd),
        "current_notional_usd": float(current_notional_usd),
        "levels": [
            {
                "multiplier": lvl.multiplier,
                "proposed_notional_usd": lvl.proposed_notional_usd,
                "breaches": [asdict(b) for b in lvl.breaches],
                "passed": lvl.passed,
            }
            for lvl in levels
        ],
        "max_safe_multiplier": max_safe,
    }


def stress_fleet(
    *,
    strategies: list[dict],
    anchor_usd: float,
    multipliers: tuple[float, ...] = DEFAULT_MULTIPLIERS,
) -> dict:
    """Run stress on a list of strategy dicts. Each dict needs:
        {strategy, symbol, direction, current_notional_usd}.

    Returns the fleet-level summary plus per-strategy reports. Per-strategy
    evaluation here is INDEPENDENT (does not stack the scaled-up strategies
    against each other). That keeps interpretation simple: each row answers
    "if THIS strategy scaled up but the rest stayed flat, where does it
    trip?" Joint scaling is a separate analysis."""
    per: list[dict] = []
    for row in strategies:
        per.append(stress_strategy(
            strategy_label=row["strategy"],
            symbol=row["symbol"],
            direction=row.get("direction", "long"),
            current_notional_usd=float(row.get("current_notional_usd", 0) or 0),
            anchor_usd=anchor_usd,
            multipliers=multipliers,
        ))

    survivors = {
        m: [r["strategy"] for r in per if r["max_safe_multiplier"] >= m]
        for m in multipliers
    }

    return {
        "anchor_usd": float(anchor_usd),
        "multipliers": list(multipliers),
        "per_strategy": per,
        "fleet_survivors_by_multiplier": survivors,
    }
