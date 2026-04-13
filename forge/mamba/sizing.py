"""
Mamba Position Sizing
======================
Risk-based sizing for MNQ (Micro Nasdaq futures).
Adjusts by touch count (more touches = stronger trendline = more confidence).
"""

from __future__ import annotations


def compute_mamba_size(
    equity: float,
    stop_points: float,
    touch_count: int,
    conviction_multiplier: float = 1.0,
    point_value: float = 2.0,  # MNQ = $2 per point
) -> dict:
    """
    Compute position size for a Mamba trendline breakout trade.

    Parameters:
        equity: account equity in USD
        stop_points: NAS100 points from entry to stop
        touch_count: number of trendline touches (3+)
        conviction_multiplier: from fleet conviction scorer (0.25-2.0)
        point_value: dollars per point (MNQ = $2)

    Returns:
        {contracts: int, risk_usd: float, risk_pct: float}
    """
    if stop_points <= 0 or equity <= 0:
        return {"contracts": 0, "risk_usd": 0.0, "risk_pct": 0.0}

    # Base risk: 1% of equity
    base_risk_pct = 0.01
    base_risk_usd = equity * base_risk_pct

    # Touch count multiplier
    if touch_count >= 7:
        touch_mult = 1.5
    elif touch_count >= 5:
        touch_mult = 1.25
    else:
        touch_mult = 1.0  # 3-4 touches

    adjusted_risk = base_risk_usd * touch_mult * conviction_multiplier

    # Cap at 3% of equity
    max_risk = equity * 0.03
    adjusted_risk = min(adjusted_risk, max_risk)

    risk_per_contract = stop_points * point_value
    contracts = int(adjusted_risk / risk_per_contract)
    contracts = max(contracts, 0)

    actual_risk = contracts * risk_per_contract
    actual_pct = actual_risk / equity if equity > 0 else 0

    return {
        "contracts": contracts,
        "risk_usd": round(actual_risk, 2),
        "risk_pct": round(actual_pct * 100, 4),
    }
