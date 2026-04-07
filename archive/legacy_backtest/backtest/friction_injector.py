#!/usr/bin/env python3
# backtest/friction_injector.py
#
# Phase 11 — Empirical Friction Injection into Backtest
#
# Loads a friction_report_*.json produced by analytics/friction_report.py
# and provides slippage sampling to replace the single SLIPPAGE_BPS constant
# in backtest runs.
#
# Three modes:
#   constant     — apply p50 entry/exit slippage to every fill
#   conditional  — apply regime/session bucket p50 (falls back to overall p50)
#   monte_carlo  — sample from the empirical distribution per fill
#
# Usage:
#   from backtest.friction_injector import FrictionInjector, load_latest_friction_report
#
#   injector = FrictionInjector(report_path, mode="monte_carlo", seed=42)
#   entry_bps = injector.sample_entry_slippage_bps(regime="TRENDING", session="NY")
#   exit_bps  = injector.sample_exit_slippage_bps()
#
#   # Override cfg for a backtest run:
#   cfg = injector.inject_into_cfg(cfg)   # sets SLIPPAGE_BPS to p50 constant
#
# NOTE: inject_into_cfg is a convenience for constant mode only.
# For conditional / monte_carlo modes, call sample_*_slippage_bps() per tick
# and apply the returned bps value directly to the fill price calculation.

from __future__ import annotations

import glob
import json
import math
import os
import random
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


MODES = ("constant", "conditional", "monte_carlo")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _get_pct(d: Dict, key: str, fallback: float) -> float:
    """Extract a percentile value from a distribution dict, with fallback."""
    v = d.get(key)
    if v is None:
        return fallback
    return _safe_float(v, fallback)


def _bps_to_decimal(bps: float) -> Decimal:
    return Decimal(str(bps)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)


# ─────────────────────────────────────────────────────────────────────────────
# Report loader
# ─────────────────────────────────────────────────────────────────────────────

def load_friction_report(path: str | Path) -> Dict[str, Any]:
    """Load a friction_report_*.json from disk."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Friction report not found: {p}")
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_latest_friction_report(log_dir: str | Path = "ops/logs") -> Dict[str, Any]:
    """Load the most recently modified friction_report_*.json from log_dir."""
    log_dir = Path(log_dir)
    pattern = str(log_dir / "friction_report_*.json")
    matches = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    if not matches:
        raise FileNotFoundError(
            f"No friction_report_*.json found in {log_dir}. "
            "Run: python -m analytics.friction_report"
        )
    return load_friction_report(matches[0])


# ─────────────────────────────────────────────────────────────────────────────
# Distribution reconstruction for Monte Carlo
# ─────────────────────────────────────────────────────────────────────────────

class _DistributionSampler:
    """
    Reconstruct an approximate empirical distribution from percentile summary
    and sample from it via linear interpolation.

    Uses the stored p50/p95/p99/min/max to approximate a piecewise-linear CDF.
    This is a reasonable approximation for Monte Carlo when the full observation
    list is not stored in the report JSON.
    """

    def __init__(self, dist: Dict[str, Any], fallback: float = 0.0) -> None:
        self._fallback = fallback
        self._n = int(dist.get("count", 0)) if dist else 0

        if not dist or self._n == 0:
            self._points: List[Tuple[float, float]] = []
            return

        lo = _safe_float(dist.get("min"), fallback)
        p50 = _safe_float(dist.get("p50"), fallback)
        p95 = _safe_float(dist.get("p95"), fallback)
        p99 = _safe_float(dist.get("p99"), fallback)
        hi = _safe_float(dist.get("max"), fallback)

        # (cdf_value, x_value) — CDF anchor points for piecewise interpolation
        self._points = [
            (0.0, lo),
            (0.50, p50),
            (0.95, p95),
            (0.99, p99),
            (1.0, hi),
        ]

    def sample(self, rng: random.Random) -> float:
        if not self._points or self._n == 0:
            return self._fallback
        u = rng.random()
        # Find bracket
        for i in range(len(self._points) - 1):
            c0, x0 = self._points[i]
            c1, x1 = self._points[i + 1]
            if c0 <= u <= c1:
                if c1 == c0:
                    return x0
                t = (u - c0) / (c1 - c0)
                return x0 + t * (x1 - x0)
        return self._points[-1][1]

    @property
    def p50(self) -> float:
        for cdf, v in self._points:
            if cdf == 0.50:
                return v
        return self._fallback

    @property
    def count(self) -> int:
        return self._n


# ─────────────────────────────────────────────────────────────────────────────
# FrictionInjector
# ─────────────────────────────────────────────────────────────────────────────

class FrictionInjector:
    """
    Empirical friction injector for backtest runs.

    Parameters
    ----------
    report : dict
        Loaded friction_report JSON (from load_friction_report / load_latest_friction_report).
    mode : str
        One of: "constant", "conditional", "monte_carlo".
    seed : int, optional
        RNG seed for monte_carlo mode (default: None = non-deterministic).
    """

    def __init__(
        self,
        report: Dict[str, Any],
        mode: str = "constant",
        seed: Optional[int] = None,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {repr(mode)}")
        self._mode = mode
        self._report = report
        self._rng = random.Random(seed)

        slip = report.get("slippage_bps", {})

        # Overall entry / exit distributions
        self._entry_dist = _DistributionSampler(slip.get("entry", {}))
        self._exit_dist = _DistributionSampler(slip.get("exit", {}))
        self._all_dist = _DistributionSampler(slip.get("all", {}))

        # Per-bucket distributions for conditional mode
        self._regime_entry: Dict[str, _DistributionSampler] = {}
        self._session_entry: Dict[str, _DistributionSampler] = {}

        for regime, d in (slip.get("by_regime") or {}).items():
            self._regime_entry[regime.upper()] = _DistributionSampler(d)
        for session, d in (slip.get("by_session") or {}).items():
            self._session_entry[session.upper()] = _DistributionSampler(d)

        # Data quality
        dq = report.get("data_quality", {})
        self.sufficient_data: bool = bool(dq.get("sufficient_data", False))
        self.warnings: List[str] = list(dq.get("warnings", []))

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def entry_p50_bps(self) -> float:
        return self._entry_dist.p50

    @property
    def exit_p50_bps(self) -> float:
        return self._exit_dist.p50

    @property
    def all_p50_bps(self) -> float:
        return self._all_dist.p50

    # ── Sampling ──────────────────────────────────────────────────────────────

    def sample_entry_slippage_bps(
        self,
        regime: Optional[str] = None,
        session: Optional[str] = None,
    ) -> float:
        """
        Return an entry slippage value in bps (positive = adverse = cost).

        constant mode:    always returns p50 entry slippage
        conditional mode: returns bucket p50 if available, else overall p50
        monte_carlo mode: samples from empirical CDF (bucket if available)
        """
        if self._mode == "constant":
            return self._entry_dist.p50

        if self._mode == "conditional":
            dist = self._best_entry_dist(regime, session)
            return dist.p50

        # monte_carlo
        dist = self._best_entry_dist(regime, session)
        return dist.sample(self._rng)

    def sample_exit_slippage_bps(
        self,
        regime: Optional[str] = None,
        session: Optional[str] = None,
    ) -> float:
        """
        Return an exit slippage value in bps (positive = adverse = cost).
        Exit slippage has no regime/session breakdown yet — falls back to overall.
        """
        if self._mode == "constant":
            return self._exit_dist.p50

        if self._mode == "conditional":
            return self._exit_dist.p50

        # monte_carlo
        return self._exit_dist.sample(self._rng)

    def _best_entry_dist(
        self, regime: Optional[str], session: Optional[str]
    ) -> _DistributionSampler:
        """Return the most specific entry distribution available."""
        if regime:
            d = self._regime_entry.get(regime.upper())
            if d and d.count >= 5:
                return d
        if session:
            d = self._session_entry.get(session.upper())
            if d and d.count >= 5:
                return d
        return self._entry_dist if self._entry_dist.count > 0 else self._all_dist

    # ── Config injection ───────────────────────────────────────────────────────

    def inject_into_cfg(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """
        Return a copy of cfg with SLIPPAGE_BPS overridden to the p50 combined
        slippage value (constant mode convenience).

        For conditional / monte_carlo modes, call sample_*_slippage_bps() per
        tick instead; this method only provides a useful constant approximation.
        """
        p50 = self._all_dist.p50
        new_cfg = dict(cfg)
        new_cfg["SLIPPAGE_BPS"] = Decimal(str(round(p50, 4)))
        new_cfg["_friction_injector_mode"] = self._mode
        new_cfg["_friction_injector_p50_bps"] = p50
        return new_cfg

    # ── Repr ──────────────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"FrictionInjector(mode={self._mode!r}, "
            f"entry_p50={self.entry_p50_bps} bps, "
            f"exit_p50={self.exit_p50_bps} bps, "
            f"sufficient_data={self.sufficient_data})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Convenience factory
# ─────────────────────────────────────────────────────────────────────────────

def from_latest_report(
    log_dir: str | Path = "ops/logs",
    mode: str = "constant",
    seed: Optional[int] = None,
) -> FrictionInjector:
    """Load the most recent friction report and return an injector."""
    report = load_latest_friction_report(log_dir)
    return FrictionInjector(report, mode=mode, seed=seed)


def from_report_path(
    path: str | Path,
    mode: str = "constant",
    seed: Optional[int] = None,
) -> FrictionInjector:
    """Load a specific friction report and return an injector."""
    report = load_friction_report(path)
    return FrictionInjector(report, mode=mode, seed=seed)


# ─────────────────────────────────────────────────────────────────────────────
# CLI smoke test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    log_dir = sys.argv[1] if len(sys.argv) > 1 else "ops/logs"

    try:
        injector = from_latest_report(log_dir, mode="monte_carlo", seed=42)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}")
        print("Run: python -m analytics.friction_report first.")
        sys.exit(1)

    print(injector)
    print(f"  sufficient_data: {injector.sufficient_data}")
    for w in injector.warnings:
        print(f"  WARNING: {w}")
    print()

    for regime in (None, "TRENDING", "RANGING"):
        for session in (None, "NY", "LONDON"):
            e = injector.sample_entry_slippage_bps(regime=regime, session=session)
            x = injector.sample_exit_slippage_bps(regime=regime, session=session)
            print(f"  regime={regime} session={session}  entry={e:.4f} bps  exit={x:.4f} bps")
