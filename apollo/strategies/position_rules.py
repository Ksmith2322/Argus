"""apollo/strategies/position_rules.py -- Entry/exit rules for earnings plays.

Defines when to enter, how to size, and when to exit based on the scenario.

Entry Rules:
  - Enter BEFORE market close on earnings day (or day before)
  - Direction based on beat rate + catalyst consensus
  - Size based on conviction score

Exit Rules:
  - POST-EARNINGS GAP: If gap > 5% in your direction → ride with trailing stop
  - POST-EARNINGS GAP AGAINST: If gap > 3% against → cut immediately at open
  - DRIFT PLAY: Hold 5-20 days after positive surprise, trail at 20 EMA
  - TIMEOUT: Max hold 20 trading days regardless

Sizing Rules:
  - Score 80+: full position (2% risk)
  - Score 60-79: half position (1% risk)
  - Score < 60: no trade (watch only)

PDT Constraint (under $25K):
  - 3 day trades per 5 rolling business days
  - Strategy: enter BEFORE earnings, hold MINIMUM 2 days (avoids day trade)
  - Even if gap against you, hold through Day 2 then exit (not a day trade)
  - Alternative: enter day BEFORE earnings, sell day AFTER = 2-day hold = safe
  - Track day trade count to stay under 3/week
"""
from dataclasses import dataclass


@dataclass
class EntryPlan:
    """Pre-earnings entry plan."""
    symbol: str
    direction: str        # "long" or "short"
    entry_timing: str     # "before_close" or "after_open"
    conviction: str       # "high" (80+), "medium" (60-79), "low" (<60)
    risk_pct: float       # % of portfolio to risk
    entry_price: float    # approximate entry (current price)
    stop_pct: float       # stop as % from entry
    target_pct: float     # target as % from entry
    max_hold_days: int
    reason: str
    score: int


@dataclass
class ExitRule:
    """Post-news exit management rules."""
    scenario: str         # what happened
    action: str           # what to do
    timing: str           # when
    detail: str


def create_entry_plan(scored_market: dict) -> EntryPlan | None:
    """Generate entry plan from a scored Apollo market."""
    score = scored_market.get("score", 0)
    direction = scored_market.get("direction", "neutral")
    days_until = scored_market.get("days_until", 99)

    if score < 60 or direction == "neutral":
        return None

    # Conviction and sizing
    if score >= 80:
        conviction = "high"
        risk_pct = 0.02  # 2% of portfolio
        stop_pct = 8.0   # wider stop for high conviction
        target_pct = 20.0
    elif score >= 70:
        conviction = "medium"
        risk_pct = 0.01
        stop_pct = 5.0
        target_pct = 15.0
    else:
        conviction = "low"
        risk_pct = 0.005
        stop_pct = 4.0
        target_pct = 10.0

    # Entry timing
    if days_until <= 1:
        entry_timing = "before_close"
    elif days_until <= 3:
        entry_timing = "before_close"
    else:
        entry_timing = "watch"  # too early, revisit later

    # Adjust for direction
    if direction == "short":
        stop_pct = stop_pct * 1.2  # wider stops for shorts (upside unbounded)
        target_pct = target_pct * 0.8

    # Max hold based on play type
    if days_until < 0:
        # Post-earnings drift play
        max_hold = 20
    else:
        # Pre-earnings play (hold through earnings + drift)
        max_hold = abs(days_until) + 20

    signals = scored_market.get("signals", [])
    reason = "; ".join(signals[:3]) if signals else "score-based"

    return EntryPlan(
        symbol=scored_market["symbol"],
        direction=direction,
        entry_timing=entry_timing,
        conviction=conviction,
        risk_pct=risk_pct,
        entry_price=scored_market.get("price", 0),
        stop_pct=stop_pct,
        target_pct=target_pct,
        max_hold_days=max_hold,
        reason=reason,
        score=score,
    )


def get_exit_rules(direction: str) -> list[ExitRule]:
    """Return the exit decision tree for managing a position after news."""
    rules = []

    # ALL EXITS: minimum 2-day hold to avoid PDT (enter Day -1, exit Day +1 minimum)
    if direction == "long":
        rules = [
            ExitRule(
                "GAP_UP_BIG",
                "HOLD + trail stop at gap low (exit Day 2+)",
                "Day 1 (morning after ER): gap > 5% up",
                "DO NOT sell Day 1 (PDT). Set mental trailing stop at gap low. "
                "Day 2: if still above gap low, hold with trail. Sell when trail hit. Max 20d.",
            ),
            ExitRule(
                "GAP_UP_SMALL",
                "HOLD through Day 2, trail at 8 EMA",
                "Day 1: gap 1-5% up",
                "Positive but modest. Hold Day 1 and Day 2 minimum (PDT safe). "
                "Day 2 close: if above pre-ER close, set trail at 8 EMA. Else exit Day 2.",
            ),
            ExitRule(
                "FLAT_OPEN",
                "HOLD through Day 2, then decide",
                "Day 1: gap < 1%",
                "In-line surprise. Mandatory hold Day 1 (PDT). "
                "Day 2: if positive, hold with trail. If negative, exit Day 2 close.",
            ),
            ExitRule(
                "GAP_DOWN",
                "HOLD Day 1, exit Day 2 open (PDT safe)",
                "Day 1: gap > 2% down",
                "Thesis wrong BUT cannot sell Day 1 (PDT). Hold through Day 1. "
                "Day 2 open: exit immediately. Accept the 2-day loss.",
            ),
            ExitRule(
                "DRIFT_PLAY",
                "Hold 5-20 days with trailing stop",
                "Day 2+ if still in profit",
                "Post-ER drift = 3-8% over 20 days. Trail stop at 20-day EMA. "
                "Exit on first close below EMA. No PDT concern (multi-day hold).",
            ),
            ExitRule(
                "TIMEOUT",
                "Close regardless",
                "Day 20 after earnings",
                "Drift exhausted. Close position and redeploy capital.",
            ),
        ]
    else:  # short
        rules = [
            ExitRule(
                "GAP_DOWN_BIG",
                "HOLD short Day 1, trail from Day 2",
                "Day 1: gap > 5% down",
                "Miss confirmed. Hold Day 1 (PDT). Day 2+: trail stop at gap high. "
                "Cover when momentum stalls. Max hold 10d.",
            ),
            ExitRule(
                "GAP_DOWN_SMALL",
                "HOLD short through Day 2, tight trail",
                "Day 1: gap 1-5% down",
                "Modest miss. Hold Day 1-2 (PDT safe). Day 2: trail at 8 EMA. "
                "Cover if it reclaims pre-ER close.",
            ),
            ExitRule(
                "GAP_UP_AGAINST",
                "HOLD Day 1, cover Day 2 (PDT safe)",
                "Day 1: gap > 2% up",
                "Beat against your short. CANNOT cover Day 1 (PDT). "
                "Day 2 open: cover immediately. Accept 2-day loss. Set max loss alert.",
            ),
            ExitRule(
                "TIMEOUT",
                "Cover regardless",
                "Day 10 after earnings",
                "Shorter timeout for shorts. Cover and move on.",
            ),
        ]

    return rules


def format_trade_plan(plan: EntryPlan) -> str:
    """Format a trade plan for Discord / display."""
    lines = [
        f"**{plan.symbol} — {plan.direction.upper()} ({plan.conviction.upper()} conviction)**",
        f"  Score: {plan.score} | Entry: {plan.entry_timing}",
        f"  Price: ~${plan.entry_price:.2f} | Risk: {plan.risk_pct:.1%} of portfolio",
        f"  Stop: {plan.stop_pct:.1f}% | Target: {plan.target_pct:.1f}% | Max hold: {plan.max_hold_days}d",
        f"  Reason: {plan.reason[:80]}",
        "",
        "  **Exit Rules:**",
    ]

    for rule in get_exit_rules(plan.direction)[:4]:
        lines.append(f"    {rule.scenario}: {rule.action}")
        lines.append(f"      {rule.timing}: {rule.detail[:80]}")

    return "\n".join(lines)
