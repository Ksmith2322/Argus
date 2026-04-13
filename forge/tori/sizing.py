"""
Tori Trades — Position Sizing
===============================
Risk-based sizing for commodity/futures swing trades.
1.5% base risk per trade, adjusted by trendline grade and setup type.

Micro contract point values:
  MPL (Micro Platinum):  $10/point
  MCL (Micro Crude Oil): $100/point
  MGC (Micro Gold):      $10/point
  MYM (Micro Dow):       $0.50/point
"""

from __future__ import annotations


POINT_VALUES = {
    "MPL": 10.0,     # Micro Platinum — $10/point
    "MCL": 100.0,    # Micro Crude Oil — $100/point
    "MGC": 10.0,     # Micro Gold — $10/point
    "MYM": 0.50,     # Micro Dow — $0.50/point
    # Full-size (for reference / backtest)
    "PL": 50.0,
    "CL": 1000.0,
    "GC": 100.0,
    "YM": 5.0,
}

# Map yfinance tickers to micro contract symbols
TICKER_TO_MICRO = {
    "PL=F": "MPL",
    "CL=F": "MCL",
    "GC=F": "MGC",
    "YM=F": "MYM",
}

# Grade multipliers — A+ gets more size, B gets less
GRADE_MULTIPLIERS = {
    "A+": 1.25,
    "A": 1.0,
    "B": 0.75,
    "C": 0.5,
}

# Setup multipliers — break_retest is lowest risk, break is highest
SETUP_MULTIPLIERS = {
    "break_retest": 1.25,
    "bounce": 1.0,
    "break": 0.9,
}


def compute_tori_size(
    equity: float,
    entry_price: float,
    stop_price: float,
    grade: str = "A",
    setup_type: str = "bounce",
    conviction_multiplier: float = 1.0,
    point_value: float = 1.0,
) -> dict:
    """
    Compute position size for a Tori swing trade.

    Parameters:
        equity:                 Account equity in USD
        entry_price:            Entry price (points)
        stop_price:             Stop-loss price (points)
        grade:                  Trendline grade ('A+', 'A', 'B', 'C')
        setup_type:             'bounce', 'break', or 'break_retest'
        conviction_multiplier:  From fleet conviction scorer (0.25-2.0)
        point_value:            Dollar value per point for the contract

    Returns:
        {contracts, risk_usd, risk_pct, skip, reason}
    """
    if equity <= 0 or point_value <= 0:
        return {"contracts": 0, "risk_usd": 0.0, "risk_pct": 0.0,
                "skip": True, "reason": "invalid equity or point value"}

    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0:
        return {"contracts": 0, "risk_usd": 0.0, "risk_pct": 0.0,
                "skip": True, "reason": "zero stop distance"}

    # Base risk: 1.5% of equity (Tori uses 1-2%, we pick midpoint)
    base_risk_pct = 0.015
    base_risk_usd = equity * base_risk_pct

    # Adjustments
    grade_mult = GRADE_MULTIPLIERS.get(grade, 1.0)
    setup_mult = SETUP_MULTIPLIERS.get(setup_type, 1.0)

    adjusted_risk = base_risk_usd * grade_mult * setup_mult * conviction_multiplier

    # Risk per contract = stop distance in points * point value
    risk_per_contract = stop_distance * point_value
    if risk_per_contract <= 0:
        return {"contracts": 0, "risk_usd": 0.0, "risk_pct": 0.0,
                "skip": True, "reason": "zero risk per contract"}

    contracts = int(adjusted_risk / risk_per_contract)
    contracts = max(contracts, 0)

    actual_risk = contracts * risk_per_contract
    actual_pct = (actual_risk / equity * 100) if equity > 0 else 0.0

    return {
        "contracts": contracts,
        "risk_usd": round(actual_risk, 2),
        "risk_pct": round(actual_pct, 4),
        "skip": contracts == 0,
        "reason": "ok" if contracts > 0 else "size rounds to 0 contracts",
    }
