"""Strategy role registry — every active strategy declares whether
it is OFFENSE / DEFENSE / HEDGE / RESEARCH, and is evaluated against
the gate appropriate for that role.

Per Codex gap #10: "Each active strategy should declare whether it is
offense, defense, hedge, or research. Evaluation should use sleeve-
specific gates: offense needs excess return; defense needs drawdown
protection and low correlation."

ROLE SEMANTICS
==============

OFFENSE
    Primary alpha sleeve. Must pass the disciplined gate at the full
    1.20 PF floor at realistic slippage. The strategy's job is to
    generate excess return — anything less is just paying for beta.
    Examples: forge_xs_momentum (the 1.0× concentrated holding).

DEFENSE
    Diversifier / drawdown buffer. Lower PF bar (1.05) because the
    role is risk-reduction, not return-maximization. Must demonstrate
    EITHER lower DD than the offense sleeves OR low/negative
    correlation. Acceptable to be slightly below break-even net of
    slippage IF the correlation benefit justifies it.
    Examples: forge_gld_pm_long (metals diversifier).

HEDGE
    Negative-correlation specialist. PF floor of 0.80 (intentionally
    permissive — a strategy that loses money 60% of the time is fine
    if its wins coincide with offense sleeve losses).
    REQUIREMENT: must demonstrate negative correlation with active
    offense sleeves over a meaningful sample.
    Examples: (none currently in the roster — this slot is for
    future short-vol / tail-protection strategies).

RESEARCH
    Paper-only. PF floor of infinity (always BLOCKED). Used for new
    strategies whose evidence we're still collecting. Allocation
    factor must stay at 0.0; running paper trades just to learn.
    Examples: forge_tom_spy + forge_nov_spy until operator opts them in.

DEFAULT
    Unclassified strategies default to OFFENSE (most conservative
    gate). This is intentional — a strategy that hasn't declared its
    role should be held to the highest bar until classified.
"""
from __future__ import annotations

from typing import Optional


# ─── Role registry ───────────────────────────────────────────────────

OFFENSE = "OFFENSE"
DEFENSE = "DEFENSE"
HEDGE = "HEDGE"
RESEARCH = "RESEARCH"

VALID_ROLES = frozenset({OFFENSE, DEFENSE, HEDGE, RESEARCH})


STRATEGY_ROLES: dict[str, str] = {
    # Active roster
    "forge_xs_momentum": OFFENSE,
    "forge_gld_pm_long": DEFENSE,
    # 2026-05-24 universe-expansion variants — same engine, same role
    # as baseline xs_momentum (OFFENSE, 1.20 floor). Each survived the
    # 20y disciplined gate on an alternative universe.
    "forge_xs_momentum_sectors": OFFENSE,
    "forge_xs_momentum_style": OFFENSE,
    "forge_xs_momentum_legacy15": OFFENSE,
    "forge_xs_momentum_style_top3": OFFENSE,
    "forge_xs_momentum_legacy15_regime": OFFENSE,
    # 2026-05-25: 43-ticker global variant (broad-8 + sectors + style
    # + countries + international). Passed walk-forward OOS with
    # STRENGTHENED verdict (IS CI 1.21 → OOS CI 2.33). OFFENSE role.
    "forge_xs_momentum_global47": OFFENSE,
    # 2026-05-25: first HEDGE-role strategy. Long GLD+TLT when SPY <
    # 200dma; flat otherwise. Backtest PF 2.91 (n=28); intentionally
    # placed in HEDGE bucket so the 0.80 PF floor applies.
    "forge_tail_hedge": HEDGE,
    # Pending-opt-in candidates (also classified so we know what bar
    # they'll be evaluated against if/when activated)
    "forge_tom_spy": OFFENSE,        # PARTIAL_PASS at 1.20 floor; alpha-equivalent
    "forge_nov_spy": OFFENSE,        # MARGINAL_PASS at 1.20 floor; alpha-equivalent
    # 2026-05-26: extends gld_pm_long pattern to USO. PF net 1.40 @ 5bps,
    # CI lower 1.20 at gate floor. Independent intraday edge family.
    "forge_uso_pm_long": OFFENSE,
    # 2026-05-26: 21-day breakout on EWZ. PF 1.73, CI lower 1.275, ~3.6 fills/yr.
    # Diversifier from xs_momentum and PM-pattern families.
    "forge_ewz_breakout": OFFENSE,
    # 2026-05-26: orthogonal calendar extensions (one trade per year each).
    # IEF July (PF 12.14, bond rally) + GLD January (PF 4.23, gold seasonal).
    # Picked from extension matrix sweep specifically because they are
    # uncorrelated with the US-equity-beta cohort.
    "forge_ief_jul_hold": OFFENSE,
    "forge_gld_jan_hold": OFFENSE,
    # 2026-05-26 v30: more orthogonal calendar adds from expanded-universe sweep.
    # USO June (PF 4.07, oil driving-season) + HYG April (PF 7.41, high-yield
    # risk-on). Both 1 trade/yr, orthogonal to bonds + gold + equity cohort.
    "forge_uso_jun_hold": OFFENSE,
    "forge_hyg_apr_hold": OFFENSE,
}


# Per-role PF floor for the disciplined gate. RESEARCH uses infinity
# so the check always fails — research strategies must not pass
# preflight, regardless of how high their PF is.
_PF_FLOOR_BY_ROLE: dict[str, float] = {
    OFFENSE:  1.20,
    DEFENSE:  1.05,
    HEDGE:    0.80,
    RESEARCH: float("inf"),
}


def get_role(strategy: str) -> str:
    """Return the strategy's role. Defaults to OFFENSE for unknown
    strategies (most conservative gate)."""
    return STRATEGY_ROLES.get(strategy, OFFENSE)


def get_pf_floor(strategy: str) -> float:
    """Return the disciplined-gate PF floor appropriate to this
    strategy's role."""
    return _PF_FLOOR_BY_ROLE[get_role(strategy)]


def role_description(role: str) -> str:
    """Human-readable one-liner for the role (used in audit reports)."""
    return {
        OFFENSE:  "primary alpha sleeve — needs excess return vs benchmark",
        DEFENSE:  "diversifier / DD buffer — lower PF bar, needs low correlation",
        HEDGE:    "negative-correlation specialist — PF can be < 1.0",
        RESEARCH: "paper-only; allocation must stay at 0.0",
    }.get(role, f"(unknown role: {role})")
