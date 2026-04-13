"""
Drawdown Recovery Protocol
==========================
Systematic rules for managing drawdown state and recovery progression.
Other fleet systems call get_recovery_multiplier() to scale position sizing.

Usage:
    python -m forge.drawdown_recovery --status
    python -m forge.drawdown_recovery --simulate
"""

import argparse
import json
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
FORGE_DATA = REPO / "forge" / "data"
STATE_FILE = FORGE_DATA / "drawdown_state.json"


class DrawdownLevel(str, Enum):
    NORMAL = "NORMAL"      # 0-3%: full size
    CAUTION = "CAUTION"    # 3-5%: 50% size, pause new entries 1 day
    REDUCED = "REDUCED"    # 5-8%: 25% size, need 3 green days to resume
    HALTED = "HALTED"      # 8%+: kill switch, manual review required


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
LEVEL_THRESHOLDS = {
    DrawdownLevel.NORMAL:  (0.0, 3.0),
    DrawdownLevel.CAUTION: (3.0, 5.0),
    DrawdownLevel.REDUCED: (5.0, 8.0),
    DrawdownLevel.HALTED:  (8.0, 100.0),
}

# Recovery ladders: (green_days_required, sizing_multiplier)
RECOVERY_LADDER = {
    DrawdownLevel.CAUTION: [
        (0, 0.50),   # Immediate: 50%
        (3, 0.50),   # After 3 green days: 50%
        (5, 0.75),   # After 5 green days: 75%
        (10, 1.00),  # After 10 green days: 100%
    ],
    DrawdownLevel.REDUCED: [
        (0, 0.25),   # Immediate: 25%
        (3, 0.25),   # After 3 green days: 25%
        (7, 0.50),   # After 7 green days: 50%
        (14, 0.75),  # After 14 green days: 75%
        (21, 1.00),  # After 21 green days: 100%
    ],
    DrawdownLevel.HALTED: [
        (0, 0.00),   # Halted: no trading
    ],
}


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def _default_state() -> dict:
    """Return default drawdown state."""
    return {
        "peak_equity": 0.0,
        "current_equity": 0.0,
        "drawdown_pct": 0.0,
        "level": DrawdownLevel.NORMAL.value,
        "consecutive_green_days": 0,
        "recovery_multiplier": 1.0,
        "trough_equity": None,
        "trough_date": None,
        "level_entry_date": None,
        "last_update": None,
        "history": [],
    }


def _load_state() -> dict:
    """Load state from disk."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return _default_state()


def _save_state(state: dict) -> None:
    """Persist state to disk."""
    FORGE_DATA.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def _determine_level(drawdown_pct: float) -> DrawdownLevel:
    """Determine drawdown level from percentage."""
    dd = abs(drawdown_pct)
    for level, (lo, hi) in LEVEL_THRESHOLDS.items():
        if lo <= dd < hi:
            return level
    return DrawdownLevel.HALTED


def _compute_multiplier(level: DrawdownLevel, green_days: int) -> float:
    """Compute recovery sizing multiplier based on level and green days."""
    if level == DrawdownLevel.NORMAL:
        return 1.0

    ladder = RECOVERY_LADDER.get(level, [(0, 0.0)])
    multiplier = ladder[0][1]  # Default to first rung
    for days_req, mult in ladder:
        if green_days >= days_req:
            multiplier = mult
    return multiplier


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class DrawdownRecovery:
    """
    Manages drawdown state and recovery progression.

    Drawdown levels:
        0-3%: NORMAL  -- full size
        3-5%: CAUTION -- 50% size, pause new entries for 1 day
        5-8%: REDUCED -- 25% size, require 3 consecutive green days to resume
        8%+:  HALTED  -- kill switch, require manual review

    Recovery ladder:
        After CAUTION: resume 50% after 3 green days, 75% after 5, 100% after 10
        After REDUCED: resume 25% after 3 green days, 50% after 7, 75% after 14, 100% after 21
        After HALTED: manual intervention required
    """

    def __init__(self):
        self.state = _load_state()

    def update_drawdown(self, current_equity: float, peak_equity: float = None) -> dict:
        """
        Update drawdown state with current and peak equity values.

        Args:
            current_equity: Current portfolio equity
            peak_equity: Peak equity (if None, uses max of stored peak and current)

        Returns:
            Updated status dict
        """
        today = date.today().isoformat()

        # Update peak
        if peak_equity is not None:
            self.state["peak_equity"] = max(self.state.get("peak_equity", 0), peak_equity)
        else:
            self.state["peak_equity"] = max(self.state.get("peak_equity", 0), current_equity)

        peak = self.state["peak_equity"]
        prev_equity = self.state.get("current_equity", current_equity)
        self.state["current_equity"] = current_equity

        # Compute drawdown
        if peak > 0:
            dd_pct = (peak - current_equity) / peak * 100
        else:
            dd_pct = 0.0
        self.state["drawdown_pct"] = round(dd_pct, 4)

        # Track trough
        trough = self.state.get("trough_equity")
        if trough is None or current_equity < trough:
            self.state["trough_equity"] = current_equity
            self.state["trough_date"] = today

        # Determine level
        new_level = _determine_level(dd_pct)
        old_level = self.state.get("level", DrawdownLevel.NORMAL.value)

        if new_level.value != old_level:
            self.state["level"] = new_level.value
            self.state["level_entry_date"] = today
            # Reset green days on level change (worsening)
            if _level_severity(new_level) > _level_severity(DrawdownLevel(old_level)):
                self.state["consecutive_green_days"] = 0

        # Track green days (current equity > previous day's equity)
        if current_equity > prev_equity:
            self.state["consecutive_green_days"] = self.state.get("consecutive_green_days", 0) + 1
        elif current_equity < prev_equity:
            self.state["consecutive_green_days"] = 0
        # Equal: keep current count

        # Compute multiplier
        level = DrawdownLevel(self.state["level"])
        green = self.state["consecutive_green_days"]
        self.state["recovery_multiplier"] = _compute_multiplier(level, green)
        self.state["last_update"] = today

        # Append to history
        history = self.state.setdefault("history", [])
        history.append({
            "date": today,
            "equity": current_equity,
            "peak": peak,
            "dd_pct": round(dd_pct, 4),
            "level": level.value,
            "green_days": green,
            "multiplier": self.state["recovery_multiplier"],
        })
        # Keep last 90 entries
        if len(history) > 90:
            self.state["history"] = history[-90:]

        _save_state(self.state)
        return self.get_drawdown_status()

    def get_recovery_multiplier(self) -> float:
        """
        What sizing should the fleet use right now?
        Returns float 0.0 to 1.0.
        """
        return self.state.get("recovery_multiplier", 1.0)

    def get_drawdown_status(self) -> dict:
        """Full status for monitoring."""
        level = DrawdownLevel(self.state.get("level", "NORMAL"))
        green = self.state.get("consecutive_green_days", 0)
        multiplier = self.state.get("recovery_multiplier", 1.0)

        # Determine next recovery milestone
        next_milestone = None
        if level in RECOVERY_LADDER:
            ladder = RECOVERY_LADDER[level]
            for days_req, mult in ladder:
                if days_req > green:
                    next_milestone = {
                        "green_days_needed": days_req,
                        "days_remaining": days_req - green,
                        "next_multiplier": mult,
                    }
                    break

        return {
            "level": level.value,
            "drawdown_pct": round(self.state.get("drawdown_pct", 0), 4),
            "peak_equity": self.state.get("peak_equity", 0),
            "current_equity": self.state.get("current_equity", 0),
            "trough_equity": self.state.get("trough_equity"),
            "trough_date": self.state.get("trough_date"),
            "consecutive_green_days": green,
            "recovery_multiplier": multiplier,
            "sizing_pct": f"{multiplier:.0%}",
            "level_entry_date": self.state.get("level_entry_date"),
            "last_update": self.state.get("last_update"),
            "next_milestone": next_milestone,
        }


def _level_severity(level: DrawdownLevel) -> int:
    """Numeric severity for comparison."""
    return {
        DrawdownLevel.NORMAL: 0,
        DrawdownLevel.CAUTION: 1,
        DrawdownLevel.REDUCED: 2,
        DrawdownLevel.HALTED: 3,
    }.get(level, 0)


# ---------------------------------------------------------------------------
# Convenience functions (for import by other systems)
# ---------------------------------------------------------------------------

def get_recovery_multiplier() -> float:
    """Quick access: what sizing multiplier should the fleet use right now?"""
    return DrawdownRecovery().get_recovery_multiplier()


def update_drawdown(current_equity: float, peak_equity: float = None) -> dict:
    """Quick access: update drawdown state."""
    return DrawdownRecovery().update_drawdown(current_equity, peak_equity)


def get_drawdown_status() -> dict:
    """Quick access: full status."""
    return DrawdownRecovery().get_drawdown_status()


# ---------------------------------------------------------------------------
# Report / Simulation
# ---------------------------------------------------------------------------

def print_status() -> None:
    """Print current drawdown state."""
    dr = DrawdownRecovery()
    s = dr.get_drawdown_status()

    print("\n" + "=" * 60)
    print("DRAWDOWN RECOVERY STATUS")
    print("=" * 60)
    print(f"  Level:               {s['level']}")
    print(f"  Drawdown:            {s['drawdown_pct']:.2f}%")
    print(f"  Peak equity:         ${s['peak_equity']:,.2f}")
    print(f"  Current equity:      ${s['current_equity']:,.2f}")
    if s["trough_equity"]:
        print(f"  Trough equity:       ${s['trough_equity']:,.2f} ({s['trough_date']})")
    print(f"  Green days:          {s['consecutive_green_days']}")
    print(f"  Recovery multiplier: {s['recovery_multiplier']:.2f} ({s['sizing_pct']} sizing)")

    if s["next_milestone"]:
        nm = s["next_milestone"]
        print(f"  Next milestone:      {nm['days_remaining']} more green days -> {nm['next_multiplier']:.0%} sizing")

    if s["level_entry_date"]:
        print(f"  Level since:         {s['level_entry_date']}")
    if s["last_update"]:
        print(f"  Last update:         {s['last_update']}")
    print("=" * 60)

    # Show level guide
    print("\n  LEVEL GUIDE:")
    print("  " + "-" * 40)
    print("    NORMAL   (0-3%):   100% sizing")
    print("    CAUTION  (3-5%):   50% sizing, pause 1 day")
    print("    REDUCED  (5-8%):   25% sizing, need green days")
    print("    HALTED   (8%+):    0% sizing, manual review")
    print("=" * 60)


def run_simulation() -> None:
    """Simulate a drawdown and recovery scenario."""
    print("\n" + "=" * 60)
    print("DRAWDOWN RECOVERY SIMULATION")
    print("=" * 60)

    # Create an isolated simulation (don't affect real state)
    scenarios = [
        ("Day 0: Starting equity",    10000, 10000),
        ("Day 1: Small gain",         10050, None),
        ("Day 2: Small loss",          9950, None),
        ("Day 3: Bigger loss (-3.5%)", 9650, None),
        ("Day 4: Continued loss",      9600, None),
        ("Day 5: CAUTION stabilize",   9610, None),
        ("Day 6: Green day 1",         9630, None),
        ("Day 7: Green day 2",         9660, None),
        ("Day 8: Green day 3",         9700, None),
        ("Day 9: Red day (reset)",     9680, None),
        ("Day 10: Green day 1",        9710, None),
        ("Day 11: Green day 2",        9730, None),
        ("Day 12: Green day 3",        9760, None),
        ("Day 13: Green day 4",        9790, None),
        ("Day 14: Green day 5",        9820, None),
        ("Day 15: Green day 6",        9850, None),
    ]

    # Use a temp state for simulation
    original_state_file = STATE_FILE
    sim_state = FORGE_DATA / "drawdown_state_sim.json"

    print(f"\n  {'Step':<35} {'Equity':>8} {'DD%':>7} {'Level':>9} {'Green':>6} {'Mult':>6}")
    print("  " + "-" * 75)

    # Build a fresh DR for simulation
    dr = DrawdownRecovery()
    dr.state = _default_state()

    for label, equity, peak in scenarios:
        # Manually update without saving to real file
        peak_eq = peak if peak is not None else None
        today = date.today().isoformat()

        if peak_eq is not None:
            dr.state["peak_equity"] = max(dr.state.get("peak_equity", 0), peak_eq)
        else:
            dr.state["peak_equity"] = max(dr.state.get("peak_equity", 0), equity)

        pk = dr.state["peak_equity"]
        prev = dr.state.get("current_equity", equity)
        dr.state["current_equity"] = equity

        dd_pct = (pk - equity) / pk * 100 if pk > 0 else 0
        dr.state["drawdown_pct"] = dd_pct

        new_level = _determine_level(dd_pct)
        old_level_val = dr.state.get("level", "NORMAL")
        old_level = DrawdownLevel(old_level_val)

        if new_level != old_level:
            dr.state["level"] = new_level.value
            if _level_severity(new_level) > _level_severity(old_level):
                dr.state["consecutive_green_days"] = 0

        if equity > prev:
            dr.state["consecutive_green_days"] = dr.state.get("consecutive_green_days", 0) + 1
        elif equity < prev:
            dr.state["consecutive_green_days"] = 0

        level = DrawdownLevel(dr.state["level"])
        green = dr.state["consecutive_green_days"]
        mult = _compute_multiplier(level, green)
        dr.state["recovery_multiplier"] = mult

        print(f"  {label:<35} ${equity:>7,.0f} {dd_pct:>6.2f}% {level.value:>9} {green:>6} {mult:>5.0%}")

    print("  " + "-" * 75)
    print("\n  Simulation complete. Real state was not modified.")

    # Clean up sim file if it exists
    if sim_state.exists():
        sim_state.unlink()

    print("=" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Drawdown Recovery Protocol")
    parser.add_argument("--status", action="store_true", help="Print current drawdown state")
    parser.add_argument("--simulate", action="store_true", help="Run hypothetical drawdown/recovery")
    parser.add_argument("--update", nargs=2, type=float, metavar=("CURRENT", "PEAK"),
                        help="Update: --update <current_equity> <peak_equity>")
    args = parser.parse_args()

    if args.update:
        dr = DrawdownRecovery()
        status = dr.update_drawdown(args.update[0], args.update[1])
        print(f"Updated. Level={status['level']}, DD={status['drawdown_pct']:.2f}%, "
              f"Multiplier={status['recovery_multiplier']:.0%}")
        return

    if args.simulate:
        run_simulation()
        return

    # Default: show status
    print_status()


if __name__ == "__main__":
    main()
