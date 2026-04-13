"""
Cue Banks Position Sizing
===========================
Risk-based sizing for MYM (Micro Dow) futures.
Adjusts by confluence score — more confluence = higher conviction = more size.

Confluence tiers (per rulebook, 3+ required):
  score 3: 1.0x base risk
  score 4: 1.25x (sniper setup)
  score 5+: 1.5x (maximum confluence)

Base risk: 1% equity per trade.
High R:R (1:5-1:8) means small risk per trade is fine.
Cap: 3% equity max risk per day.
"""

from __future__ import annotations


POINT_VALUE_MYM = 0.50  # $0.50 per point Micro Dow


def compute_cuebanks_size(
    equity: float,
    entry_price: float,
    stop_price: float,
    confluence_score: int,
    conviction_multiplier: float = 1.0,
    point_value: float = POINT_VALUE_MYM,
    daily_risk_used: float = 0.0,
) -> dict:
    """
    Compute position size for a Cue Banks trade.

    Parameters:
        equity: account equity in USD
        entry_price: planned entry price
        stop_price: stop-loss price (just outside confluence zone)
        confluence_score: number of confluence factors (3+ required)
        conviction_multiplier: from fleet conviction scorer (0.25-2.0)
        point_value: dollar value per point (MYM=0.50)
        daily_risk_used: risk already used today as fraction of equity

    Returns:
        {contracts, risk_usd, risk_pct, skip, reason}
    """
    stop_points = abs(entry_price - stop_price)

    if stop_points <= 0 or equity <= 0:
        return {"contracts": 0, "risk_usd": 0.0, "risk_pct": 0.0,
                "skip": True, "reason": "invalid stop or equity"}

    # Hard rule: need 3+ confluences to trade
    if confluence_score < 3:
        return {"contracts": 0, "risk_usd": 0.0, "risk_pct": 0.0,
                "skip": True, "reason": f"only {confluence_score} confluence(s), need 3+"}

    # Base risk: 1% of equity
    base_risk_pct = 0.01
    base_risk_usd = equity * base_risk_pct

    # Confluence multiplier
    if confluence_score >= 5:
        conf_mult = 1.5
    elif confluence_score >= 4:
        conf_mult = 1.25
    else:
        conf_mult = 1.0  # score == 3

    adjusted_risk = base_risk_usd * conf_mult * conviction_multiplier

    # Cap at 3% equity per day
    max_daily_risk = equity * 0.03
    remaining_risk = max_daily_risk - (daily_risk_used * equity)
    if remaining_risk <= 0:
        return {"contracts": 0, "risk_usd": 0.0, "risk_pct": 0.0,
                "skip": True, "reason": "daily 3% risk cap hit"}

    adjusted_risk = min(adjusted_risk, remaining_risk)

    risk_per_contract = stop_points * point_value
    if risk_per_contract <= 0:
        return {"contracts": 0, "risk_usd": 0.0, "risk_pct": 0.0,
                "skip": True, "reason": "zero risk per contract"}

    contracts = int(adjusted_risk / risk_per_contract)
    contracts = max(contracts, 0)

    actual_risk = contracts * risk_per_contract
    actual_pct = actual_risk / equity if equity > 0 else 0

    return {
        "contracts": contracts,
        "risk_usd": round(actual_risk, 2),
        "risk_pct": round(actual_pct * 100, 4),
        "skip": contracts == 0,
        "reason": "ok" if contracts > 0 else "size rounds to 0 contracts",
    }
