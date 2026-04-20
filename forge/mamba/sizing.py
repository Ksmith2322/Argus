"""
Mamba Position Sizing (v2)
============================
Risk-based sizing for MNQ / MYM futures.
Adjusts by confluence count (more confluences = higher conviction = more size).

Confluence tiers:
  1 confluence: DON'T TRADE (skip)
  2 confluences: 1.0x base size
  3 confluences: 1.25x
  4+ confluences: 1.5x

Cap: 3% equity max risk per day.
"""

from __future__ import annotations


# Point values for micro futures
POINT_VALUES = {
    "MNQ": 2.0,   # $2 per point Micro Nasdaq
    "MYM": 0.50,  # $0.50 per point Micro Dow
}


def compute_mamba_size(
    equity: float,
    stop_points: float,
    confluence_count: int,
    conviction_multiplier: float = 1.0,
    instrument: str = "MNQ",
    daily_risk_used: float = 0.0,
    transcript_mode: bool = False,
) -> dict:
    """
    Compute position size for a Mamba trade.

    Parameters:
        equity: account equity in USD
        stop_points: index points from entry to stop
        confluence_count: number of confluences (2+ required to trade)
        conviction_multiplier: from fleet conviction scorer (0.25-2.0)
        instrument: "MNQ" or "MYM"
        daily_risk_used: risk already used today as fraction of equity
        transcript_mode: when True, use the risk tiers from 2026-04-19 transcript
            consolidation — base 2% (his stated "normal trade risk"), enhanced
            4% at 5 confluences (his stated "3-5% on enhanced pattern"). Daily
            cap raised to 8% to accommodate two enhanced trades per day.
            When False (default), preserves the v1 tiered sizing that was
            calibrated against the existing synthetic-1m backtest.

    Returns:
        {contracts: int, risk_usd: float, risk_pct: float, skip: bool, reason: str}
    """
    point_value = POINT_VALUES.get(instrument, 2.0)

    if stop_points <= 0 or equity <= 0:
        return {"contracts": 0, "risk_usd": 0.0, "risk_pct": 0.0,
                "skip": True, "reason": "invalid stop or equity"}

    # Hard rule: need 2+ confluences to trade
    if confluence_count < 2:
        return {"contracts": 0, "risk_usd": 0.0, "risk_pct": 0.0,
                "skip": True, "reason": f"only {confluence_count} confluence(s), need 2+"}

    if transcript_mode:
        # Transcript-stated tiers: 2% base, 4% at full 5-confluence ("enhanced pattern")
        base_risk_pct = 0.02
        base_risk_usd = equity * base_risk_pct
        if confluence_count >= 5:
            conf_mult = 2.0  # 4% total at 5-conf
        elif confluence_count >= 4:
            conf_mult = 1.5  # 3% at 4-conf
        else:
            conf_mult = 1.0  # 2% at 2-3 conf
        daily_cap_pct = 0.08
    else:
        # v1 calibrated sizing
        base_risk_pct = 0.01
        base_risk_usd = equity * base_risk_pct
        if confluence_count >= 4:
            conf_mult = 1.5
        elif confluence_count >= 3:
            conf_mult = 1.25
        else:
            conf_mult = 1.0
        daily_cap_pct = 0.03

    adjusted_risk = base_risk_usd * conf_mult * conviction_multiplier

    # Daily risk cap (3% v1, 8% transcript_mode)
    max_daily_risk = equity * daily_cap_pct
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
