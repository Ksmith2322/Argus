"""Paper-mode entry-threshold multiplier (stress / data-collection knob).

Single-purpose helper: scale a strategy's entry threshold so the strategy
fires more often in paper, exposing execution-path bugs faster.

  mult < 1.0 → looser threshold → more activity (the data-collection use)
  mult > 1.0 → tighter threshold → less activity
  mult = 1.0 → unchanged (default)

Safety contract:
  1. Refuses to apply unless IBKR_PORT == "7497" (paper port)
  2. Refuses to apply when REAL_MONEY_ENABLED is set
  3. Clamps multiplier to [0.1, 10.0]; outside that range falls back to 1.0
  4. Logs WARNING on every active application so operators see the mode

Intentionally writes WARNING (not INFO) so any active stress is visible at
default log levels. Caller decides which threshold knob to scale.
"""
from __future__ import annotations

import logging
import os

_log = logging.getLogger(__name__)

_MIN_MULT = 0.1
_MAX_MULT = 10.0

# TWS paper = 7497; IB Gateway paper = 4002. Both are paper modes;
# paper_stress applies to either. Keeping PAPER_PORT as a single
# string for backward compatibility with anything that imports it.
PAPER_PORT = "7497"
PAPER_PORTS = {"7497", "4002"}


def _is_paper() -> bool:
    if os.environ.get("IBKR_PORT", "") not in PAPER_PORTS:
        return False
    if os.environ.get("REAL_MONEY_ENABLED", "").strip().lower() in ("1", "true", "yes"):
        return False
    return True


def apply(base_value: float, multiplier: float, *, strategy: str, knob: str) -> float:
    """Return the threshold to actually use.

    base_value : strategy's configured threshold (unchanged when mult==1.0)
    multiplier : per-strategy paper_stress_multiplier from config
    strategy   : strategy name, for log line
    knob       : threshold name, for log line
    """
    if multiplier == 1.0:
        return base_value
    if not _is_paper():
        _log.warning(
            "paper_stress IGNORED %s:%s — not paper (IBKR_PORT=%s real_money=%s); mult=%s",
            strategy, knob,
            os.environ.get("IBKR_PORT", ""),
            os.environ.get("REAL_MONEY_ENABLED", ""),
            multiplier,
        )
        return base_value
    if not (_MIN_MULT <= multiplier <= _MAX_MULT):
        _log.warning(
            "paper_stress multiplier %s outside [%s, %s] for %s:%s — using base %s",
            multiplier, _MIN_MULT, _MAX_MULT, strategy, knob, base_value,
        )
        return base_value
    effective = base_value * multiplier
    _log.warning(
        "PAPER_STRESS_ACTIVE %s:%s base=%s mult=%s effective=%s",
        strategy, knob, base_value, multiplier, effective,
    )
    return effective
