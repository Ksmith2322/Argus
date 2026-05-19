"""Killed-strategy invariant — once a strategy is killed, it stays killed.

A strategy is "killed" when it's listed in `helio/roi_filter.KILLED_STRATEGY_CUTOFFS`
(the canonical kill registry used by ROI tooling to quarantine post-cutoff
rows). The kill must be enforced in three layers so that a partial revert
cannot accidentally resurrect a thesis-exhausted strategy:

  1. **Capital layer** — `argus_flow/configs/allocation_factors.json` has
     `factors[strategy] == 0.0`. Even if the runner were started by hand,
     `compute_risk_usd()` multiplies by 0.0 and the strategy gets zero
     capital.

  2. **Process layer** — `helio/fleet_monitor.SYSTEMS[strategy]` has
     `no_restart: True`. The fleet monitor will not auto-restart a dead
     process for a killed strategy.

  3. **Real-money layer** — the strategy must not appear in
     `argus_flow/configs/real_money_allowlist.json` `strategies`.

If any of these three is missing for a killed strategy, the test fails —
that is the regression signal."""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

ALLOCATION_FACTORS_PATH = REPO / "argus_flow" / "configs" / "allocation_factors.json"
REAL_MONEY_ALLOWLIST_PATH = REPO / "argus_flow" / "configs" / "real_money_allowlist.json"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_every_killed_strategy_has_allocation_factor_zero():
    """Capital layer: kill registry ↔ allocation_factors.json must agree."""
    from helio.roi_filter import KILLED_STRATEGY_CUTOFFS

    factors = _load_json(ALLOCATION_FACTORS_PATH).get("factors", {})
    offenders: list[str] = []
    for strategy in KILLED_STRATEGY_CUTOFFS:
        f = factors.get(strategy)
        if f is None:
            offenders.append(f"{strategy}: missing from allocation_factors.json factors")
        elif float(f) != 0.0:
            offenders.append(f"{strategy}: factor={f} (should be 0.0)")
    assert not offenders, (
        "Killed strategy has non-zero allocation factor — capital can still "
        "flow to it on restart. Set factor=0.0 in "
        "argus_flow/configs/allocation_factors.json:\n  " + "\n  ".join(offenders)
    )


def test_every_killed_strategy_has_no_restart_in_fleet_monitor():
    """Process layer: kill registry ↔ fleet_monitor SYSTEMS must agree."""
    from helio.roi_filter import KILLED_STRATEGY_CUTOFFS
    from helio.fleet_monitor import SYSTEMS

    offenders: list[str] = []
    for strategy in KILLED_STRATEGY_CUTOFFS:
        cfg = SYSTEMS.get(strategy)
        if cfg is None:
            # Strategy not monitored — fine; means it's not on the auto-restart
            # path anyway. (e.g., scheduled-task style runners.)
            continue
        if not cfg.get("no_restart"):
            offenders.append(
                f"{strategy}: SYSTEMS entry missing no_restart=True"
            )
    assert not offenders, (
        "Killed strategy could be auto-restarted by fleet_monitor. Add "
        "no_restart=True to its SYSTEMS entry in helio/fleet_monitor.py:\n  "
        + "\n  ".join(offenders)
    )


def test_no_killed_strategy_in_real_money_allowlist():
    """Real-money layer: a killed strategy must never be allowlisted."""
    from helio.roi_filter import KILLED_STRATEGY_CUTOFFS

    allowlist = _load_json(REAL_MONEY_ALLOWLIST_PATH)
    strategies = {str(s).lower() for s in allowlist.get("strategies", [])}
    offenders = [s for s in KILLED_STRATEGY_CUTOFFS if s.lower() in strategies]
    assert not offenders, (
        "Killed strategy is on the real-money allowlist — remove it from "
        "argus_flow/configs/real_money_allowlist.json:\n  " + "\n  ".join(offenders)
    )


def test_kill_registry_dates_are_iso():
    """Cutoff values must be ISO YYYY-MM-DD so roi_filter can compare timestamps."""
    from helio.roi_filter import KILLED_STRATEGY_CUTOFFS
    import datetime

    bad: list[str] = []
    for strategy, cutoff in KILLED_STRATEGY_CUTOFFS.items():
        try:
            datetime.date.fromisoformat(cutoff)
        except (TypeError, ValueError):
            bad.append(f"{strategy}: cutoff={cutoff!r}")
    assert not bad, (
        "KILLED_STRATEGY_CUTOFFS values must be ISO YYYY-MM-DD:\n  "
        + "\n  ".join(bad)
    )
