#!/usr/bin/env python3
# backtest/stress_runner.py
#
# Phase 12 — Stress Scenario Battery
#
# Runs a defined set of stress scenarios against the full candle dataset and
# compares each to the baseline backtest.  Scenarios cover:
#
#   Friction tiers:
#     baseline      — configured SLIPPAGE_BPS, no extra friction
#     friction_p50  — p50 slippage from friction report (requires report)
#     friction_p95  — p95 slippage from friction report
#     friction_p99  — p99 slippage from friction report
#
#   Spread widening (BT_SYNTH_SPREAD_BPS override):
#     spread_1x     — baseline spread
#     spread_1_5x   — 1.5× spread
#     spread_2x     — 2× spread
#     spread_3x     — 3× spread
#
#   Regime distortion (DamageProfile.force_regime):
#     regime_forced_TRENDING   — all ticks read as TRENDING
#     regime_forced_RANGING    — all ticks read as RANGING
#     regime_forced_UNKNOWN    — all ticks read as UNKNOWN
#
#   Signal distortion (DamageProfile.signal_flip_pct):
#     signal_flip_5pct   — 5% of signals flipped
#     signal_flip_10pct  — 10% of signals flipped
#     signal_flip_25pct  — 25% of signals flipped
#
# Usage:
#   python -m backtest.stress_runner
#   python -m backtest.stress_runner --scenarios baseline spread_1_5x spread_2x
#   python -m backtest.stress_runner --friction-report-latest
#   python -m backtest.stress_runner --out-dir ops/logs --run-id stress_20260312

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional


REPO_ROOT = Path(__file__).parent.parent


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(str(v).strip())
    except Exception:
        return default


# ─────────────────────────────────────────────────────────────────────────────
# Scenario definitions
# ─────────────────────────────────────────────────────────────────────────────

def _build_scenarios(
    friction_p50: Optional[float],
    friction_p95: Optional[float],
    friction_p99: Optional[float],
    baseline_spread_bps: float,
) -> Dict[str, Dict[str, Any]]:
    """
    Returns a dict of scenario_name -> {cfg_overrides, damage_kwargs, description}.
    cfg_overrides are merged into the backtest cfg.
    damage_kwargs are passed to DamageProfile(**kwargs).
    """
    scenarios: Dict[str, Dict[str, Any]] = {}

    # ── Baseline ──
    scenarios["baseline"] = {
        "description": "Baseline — configured slippage, baseline spread",
        "cfg_overrides": {},
        "damage_kwargs": None,
    }

    # ── Friction tiers ──
    if friction_p50 is not None and friction_p50 > 0:
        scenarios["friction_p50"] = {
            "description": f"Friction p50 — SLIPPAGE_BPS={friction_p50:.2f}",
            "cfg_overrides": {"SLIPPAGE_BPS": Decimal(str(round(friction_p50, 4)))},
            "damage_kwargs": None,
        }
    if friction_p95 is not None and friction_p95 > 0:
        scenarios["friction_p95"] = {
            "description": f"Friction p95 — SLIPPAGE_BPS={friction_p95:.2f}",
            "cfg_overrides": {"SLIPPAGE_BPS": Decimal(str(round(friction_p95, 4)))},
            "damage_kwargs": None,
        }
    if friction_p99 is not None and friction_p99 > 0:
        scenarios["friction_p99"] = {
            "description": f"Friction p99 — SLIPPAGE_BPS={friction_p99:.2f}",
            "cfg_overrides": {"SLIPPAGE_BPS": Decimal(str(round(friction_p99, 4)))},
            "damage_kwargs": None,
        }

    # ── Spread widening ──
    base = baseline_spread_bps
    for mult, label in [(1.5, "1_5x"), (2.0, "2x"), (3.0, "3x")]:
        scenarios[f"spread_{label}"] = {
            "description": f"Spread {mult}× baseline ({base * mult:.1f} bps)",
            "cfg_overrides": {"BT_SYNTH_SPREAD_BPS": float(base * mult)},
            "damage_kwargs": None,
        }

    # ── Regime distortion ──
    for regime in ("TRENDING", "RANGING", "UNKNOWN"):
        scenarios[f"regime_forced_{regime}"] = {
            "description": f"All ticks forced to regime={regime}",
            "cfg_overrides": {},
            "damage_kwargs": {"name": f"regime_{regime.lower()}", "force_regime": regime, "seed": 1337},
        }

    # ── Signal flip ──
    for pct, label in [(5, "5pct"), (10, "10pct"), (25, "25pct")]:
        scenarios[f"signal_flip_{label}"] = {
            "description": f"Signal flip {pct}% — {pct}% of signals randomised",
            "cfg_overrides": {},
            "damage_kwargs": {"name": f"signal_flip_{label}", "signal_flip_pct": float(pct), "seed": 1337},
        }

    return scenarios


# ─────────────────────────────────────────────────────────────────────────────
# Per-scenario backtest runner
# ─────────────────────────────────────────────────────────────────────────────

def _run_scenario(
    name: str,
    spec: Dict[str, Any],
    candles_csv: str,
    base_cfg: Optional[Dict],
) -> Dict[str, Any]:
    from backtest.runner import run_backtest, DamageProfile
    from config import load_config

    cfg = dict(base_cfg) if base_cfg else load_config()
    cfg.update(spec.get("cfg_overrides") or {})

    damage = None
    dkwargs = spec.get("damage_kwargs")
    if dkwargs:
        damage = DamageProfile(**dkwargs)

    try:
        res = run_backtest(
            candles_csv=candles_csv,
            csv_format_hint="auto",
            limit=None,
            write_logs=False,
            cfg_override=cfg,
            damage=damage,
        )
        row: Dict[str, Any] = {"scenario": name, "status": "ok"}
        # Use summary() — canonical field names from BacktestResults
        try:
            sd = res.summary() if hasattr(res, "summary") else {}
            for k, v in sd.items():
                try:
                    row[k] = float(v) if isinstance(v, Decimal) else v
                except Exception:
                    row[k] = str(v)
        except Exception:
            pass
    except Exception as exc:
        row = {"scenario": name, "status": "error", "error": str(exc)}

    return row


# ─────────────────────────────────────────────────────────────────────────────
# Load friction percentiles
# ─────────────────────────────────────────────────────────────────────────────

def _load_friction_percentiles(
    report_path: Optional[str],
    report_latest: bool,
    log_dir: str,
) -> Dict[str, Optional[float]]:
    try:
        from backtest.friction_injector import load_friction_report, load_latest_friction_report
        if report_path and os.path.exists(report_path):
            report = load_friction_report(report_path)
        elif report_latest:
            report = load_latest_friction_report(log_dir)
        else:
            return {"p50": None, "p95": None, "p99": None}
        slip = report.get("slippage_bps", {}).get("all", {})
        return {
            "p50": slip.get("p50"),
            "p95": slip.get("p95"),
            "p99": slip.get("p99"),
        }
    except Exception:
        return {"p50": None, "p95": None, "p99": None}


# ─────────────────────────────────────────────────────────────────────────────
# Main runner
# ─────────────────────────────────────────────────────────────────────────────

def run_stress(
    candles_csv: str,
    selected_scenarios: Optional[List[str]] = None,
    out_dir: str = "ops/logs",
    run_id: Optional[str] = None,
    friction_report_path: Optional[str] = None,
    friction_report_latest: bool = False,
    base_cfg: Optional[Dict] = None,
) -> Dict[str, Any]:
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    log_dir = str(Path(out_dir))

    # Load base cfg once
    if base_cfg is None:
        from config import load_config
        base_cfg = load_config()

    baseline_spread = float(base_cfg.get("BT_SYNTH_SPREAD_BPS", 8.0) or 8.0)

    friction = _load_friction_percentiles(friction_report_path, friction_report_latest, log_dir)
    scenarios = _build_scenarios(
        friction_p50=friction.get("p50"),
        friction_p95=friction.get("p95"),
        friction_p99=friction.get("p99"),
        baseline_spread_bps=baseline_spread,
    )

    if selected_scenarios:
        scenarios = {k: v for k, v in scenarios.items() if k in selected_scenarios}
        missing = [s for s in selected_scenarios if s not in scenarios]
        if missing:
            print(f"  WARNING: unknown scenarios ignored: {missing}", flush=True)

    print(f"Stress runner: {len(scenarios)} scenarios on {candles_csv}")
    results: List[Dict[str, Any]] = []
    for name, spec in scenarios.items():
        print(f"  [{name}] {spec['description']} ...", end=" ", flush=True)
        row = _run_scenario(name, spec, candles_csv, base_cfg)
        results.append(row)
        status = row.get("status", "?")
        pnl = row.get("pnl_usd", row.get("total_pnl", row.get("pnl", "?")))
        trades = row.get("trades_closed", row.get("total_trades", "?"))
        print(f"status={status}  pnl={pnl}  trades={trades}")

    # Compute erosion vs baseline
    baseline_row = next((r for r in results if r.get("scenario") == "baseline"), None)
    erosion = _compute_erosion(results, baseline_row)

    summary = {
        "version": "1.0",
        "generated_at": _utc_now(),
        "run_id": run_id,
        "candles_csv": candles_csv,
        "friction": friction,
        "baseline_spread_bps": baseline_spread,
        "scenarios_run": len(results),
        "scenarios_ok": sum(1 for r in results if r.get("status") == "ok"),
        "results": results,
        "erosion": erosion,
    }

    out_path = Path(out_dir) / f"stress_summary_{run_id}.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\n  Summary -> {out_path}")

    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Erosion computation
# ─────────────────────────────────────────────────────────────────────────────

def _compute_erosion(
    results: List[Dict[str, Any]],
    baseline: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    if not baseline or baseline.get("status") != "ok":
        return {}

    pnl_key = next((k for k in ("pnl_usd", "total_pnl", "net_pnl", "pnl") if k in baseline), None)
    if not pnl_key:
        return {}

    baseline_pnl = _safe_float(baseline.get(pnl_key))
    erosion: Dict[str, Any] = {}

    for row in results:
        name = row.get("scenario", "?")
        if name == "baseline" or row.get("status") != "ok":
            continue
        scenario_pnl = _safe_float(row.get(pnl_key))
        delta = scenario_pnl - baseline_pnl
        pct = (delta / abs(baseline_pnl) * 100) if baseline_pnl != 0 else None
        erosion[name] = {
            "baseline_pnl": round(baseline_pnl, 8),
            "scenario_pnl": round(scenario_pnl, 8),
            "delta_pnl": round(delta, 8),
            "erosion_pct": round(pct, 2) if pct is not None else None,
            "edge_survived": scenario_pnl > 0 if baseline_pnl > 0 else None,
        }

    return erosion


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Phase 12 stress scenario battery — baseline + spread/friction/regime distortion"
    )
    parser.add_argument("--candles-csv", default=None)
    parser.add_argument("--scenarios", nargs="*", default=None,
                        help="Specific scenarios to run (default: all)")
    parser.add_argument("--out-dir", default="ops/logs")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--friction-report-path", default=None)
    parser.add_argument("--friction-report-latest", action="store_true")
    parser.add_argument("--list-scenarios", action="store_true",
                        help="Print all available scenario names and exit")
    args = parser.parse_args(argv)

    if args.list_scenarios:
        scenarios = _build_scenarios(None, None, None, baseline_spread_bps=8.0)
        print("Available scenarios:")
        for name, spec in scenarios.items():
            print(f"  {name:35s}  {spec['description']}")
        return

    candles_csv = args.candles_csv or str(REPO_ROOT / "data" / "eth_usd_1m.csv")

    summary = run_stress(
        candles_csv=candles_csv,
        selected_scenarios=args.scenarios,
        out_dir=str(Path(args.out_dir).resolve()),
        run_id=args.run_id,
        friction_report_path=args.friction_report_path,
        friction_report_latest=args.friction_report_latest,
    )

    erosion = summary.get("erosion", {})
    if erosion:
        print("\nErosion vs baseline:")
        for name, e in erosion.items():
            survived = e.get("edge_survived")
            print(f"  {name:35s}  delta={e.get('delta_pnl'):+.4f}  "
                  f"erosion={e.get('erosion_pct')}%  survived={survived}")


if __name__ == "__main__":
    main()
