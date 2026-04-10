"""apollo/strategies/position_rules.py -- Post-ER entry/exit rules.

BACKTEST PROVEN: Only post-ER drift on BIG confirmed moves works.
Pre-earnings plays (blind entry, run-up) are negative EV across 110+ stocks.

Strategy: "Day-After Big Gap" play
  - Wait for earnings to report (after hours / pre-market)
  - If gap >= 7% AND beat confirmed: enter LONG Day 2 morning
  - Hold 5-20 days riding the post-ER drift
  - Trail stop at 4% from peak after 3% profit
  - PDT safe by design (enter Day 2, exit Day 4+ minimum)

Backtest: gaps 7-15% = PF 1.27, 62% WR (the only profitable bucket)

What DOESN'T work (all backtested negative):
  - Blind entry before earnings: PF 0.32
  - 5-day run-up play: PF 0.37
  - Post-ER drift on all beats: PF 0.69
  - Post-ER drift on small gaps (<7%): PF 0.60
"""
from dataclasses import dataclass


@dataclass
class PostERPlan:
    """Post-earnings drift trade plan."""
    symbol: str
    direction: str          # "long" (beat + gap up) or "short" (miss + gap down)
    gap_pct: float          # Day 1 gap size
    surprise_pct: float     # earnings surprise %
    entry_timing: str       # "day2_open" — always
    entry_price: float      # approximate (Day 1 close)
    stop_pct: float         # 5% from entry
    trail_trigger_pct: float  # activate trail after 3%
    trail_pct: float        # 4% from peak
    max_hold_days: int      # 20 for longs, 10 for shorts
    score: int
    reason: str


# ── Entry Criteria ────────────────────────────────────────────

def should_enter_post_er(
    gap_pct: float,
    surprise_pct: float,
    beat_rate: float = 0.5,
    day1_volume_ratio: float = 1.0,
    score: int = 0,
) -> PostERPlan | None:
    """Evaluate whether to enter a post-ER drift trade.

    Only enters on BIG confirmed moves (gap >= 7%).
    Returns a trade plan or None.
    """
    # Must have meaningful gap
    abs_gap = abs(gap_pct)
    if abs_gap < 5:
        return None  # too small — drift won't persist

    direction = None
    reasons = []

    # LONG: beat + big gap up
    if gap_pct >= 7 and surprise_pct > 0:
        direction = "long"
        reasons.append(f"BIG_GAP_UP: +{gap_pct:.1f}% | Beat +{surprise_pct:.1f}%")

        # Extra confidence
        if gap_pct >= 10:
            reasons.append("MASSIVE_GAP: 10%+ = strong institutional buying")
        if beat_rate >= 0.75:
            reasons.append(f"SERIAL_BEATER: {beat_rate:.0%} historical beat rate")

    # LONG: moderate gap (5-7%) but huge surprise
    elif gap_pct >= 5 and surprise_pct > 10:
        direction = "long"
        reasons.append(f"BIG_SURPRISE: +{surprise_pct:.1f}% surprise, gap +{gap_pct:.1f}%")

    # SHORT: miss + big gap down (less reliable — use caution)
    elif gap_pct <= -7 and surprise_pct < -2:
        direction = "short"
        reasons.append(f"BIG_GAP_DOWN: {gap_pct:.1f}% | Miss {surprise_pct:.1f}%")

    if direction is None:
        return None

    # Volume confirmation (high Day 1 volume = real move, not thin)
    if day1_volume_ratio > 2.0:
        reasons.append(f"VOLUME_CONFIRMED: {day1_volume_ratio:.1f}x avg")
    elif day1_volume_ratio < 1.0:
        reasons.append("LOW_VOL: may fade — reduce size")

    # Sizing
    if abs_gap >= 10:
        stop = 5.0
        max_hold = 20 if direction == "long" else 10
    else:
        stop = 4.0
        max_hold = 15 if direction == "long" else 8

    return PostERPlan(
        symbol="",  # filled by caller
        direction=direction,
        gap_pct=gap_pct,
        surprise_pct=surprise_pct,
        entry_timing="day2_open",
        entry_price=0,  # filled by caller
        stop_pct=stop,
        trail_trigger_pct=3.0,
        trail_pct=4.0,
        max_hold_days=max_hold,
        score=score,
        reason=" | ".join(reasons),
    )


# ── Exit Rules ────────────────────────────────────────────────

EXIT_RULES = {
    "long": [
        {
            "name": "TRAILING_STOP",
            "trigger": "Unrealized profit >= 3%",
            "action": "Set trail at 4% below peak. Tighten to 3% after 8%+ profit.",
            "detail": "This captures the drift while locking in gains. Most winning trades exit here.",
        },
        {
            "name": "HARD_STOP",
            "trigger": "Price drops 5% from entry",
            "action": "Exit immediately at next open.",
            "detail": "The gap may have been a fakeout. Don't hold losers hoping for drift.",
        },
        {
            "name": "MOMENTUM_STALL",
            "trigger": "3 consecutive red days after Day 5",
            "action": "Exit at Day 3 close.",
            "detail": "Drift has stalled. Take whatever profit remains.",
        },
        {
            "name": "TIMEOUT",
            "trigger": "Day 20 (15 for moderate gaps)",
            "action": "Exit at close regardless.",
            "detail": "Post-ER drift exhausts after ~20 trading days. Capital is dead weight after.",
        },
    ],
    "short": [
        {
            "name": "TRAILING_STOP",
            "trigger": "Unrealized profit >= 3%",
            "action": "Set trail at 4% above trough.",
        },
        {
            "name": "HARD_STOP",
            "trigger": "Price rises 5% from entry",
            "action": "Cover immediately.",
        },
        {
            "name": "TIMEOUT",
            "trigger": "Day 10 (8 for moderate gaps)",
            "action": "Cover at close. Short drift is shorter than long drift.",
        },
    ],
}


def format_post_er_plan(plan: PostERPlan) -> str:
    """Format trade plan for Discord."""
    lines = [
        f"**{plan.symbol} — POST-ER {plan.direction.upper()}**",
        f"  Gap: {plan.gap_pct:+.1f}% | Surprise: {plan.surprise_pct:+.1f}%",
        f"  Entry: Day 2 open (~${plan.entry_price:.2f})",
        f"  Stop: {plan.stop_pct}% | Trail: {plan.trail_pct}% after {plan.trail_trigger_pct}% profit",
        f"  Max hold: {plan.max_hold_days}d | PDT safe (Day 2 entry)",
        f"  {plan.reason}",
        "",
        "  Exit Rules:",
    ]
    for rule in EXIT_RULES.get(plan.direction, []):
        lines.append(f"    {rule['name']}: {rule['trigger']} -> {rule['action']}")

    return "\n".join(lines)
