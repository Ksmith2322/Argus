"""CLI driver for helio.capacity_stress.

Builds the live fleet's current notional snapshot from heartbeat files,
runs the 1x/2x/5x/10x stress against all cap layers, writes the report
to ops/reports/system_audit/capacity_stress.json.

Usage:
    python -m ops.audit.run_capacity_stress
    python -m ops.audit.run_capacity_stress --anchor 30000

The report is stamped with the current evidence epoch (Codex X4)."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from helio import capacity_stress as cs
from helio import evidence_epoch as ee
from helio.cluster_exposure import compute_cluster_exposure
from helio.fleet_sizing import get_sizing_anchor_usd

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


def _strategy_inventory_from_exposure(positions: list[dict]) -> list[dict]:
    """Convert compute_cluster_exposure's `positions` list into the
    stress_fleet input shape: {strategy, symbol, direction, current_notional_usd}.

    Each broker position appears as one row. Positions without a
    `system` tag are bucketed as "unattributed" (intent-based ownership
    isn't enforced yet — see Codex X3)."""
    rows: list[dict] = []
    for p in positions:
        strat = (p.get("system") or "").strip()
        if not strat:
            strat = "unattributed"
        elif not strat.startswith("forge_") and not strat.startswith("argus_"):
            strat = f"forge_{strat}"
        sym = (p.get("symbol") or p.get("ib_canonical") or "").upper()
        if not sym:
            continue
        size = float(p.get("size", 0) or 0)
        entry_px = float(p.get("entry_px", 0) or 0)
        if size <= 0 or entry_px <= 0:
            continue
        rows.append({
            "strategy": strat,
            "symbol": sym,
            "direction": ("long" if size > 0 else "short"),
            "current_notional_usd": abs(size) * entry_px,
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anchor", type=float, default=None,
                        help="Override broker anchor (USD). Default: live read via fleet_sizing.")
    parser.add_argument("--multipliers", nargs="+", type=float,
                        default=[1.0, 2.0, 5.0, 10.0],
                        help="Stress multipliers to evaluate.")
    args = parser.parse_args(argv)

    try:
        anchor = args.anchor if args.anchor is not None else float(get_sizing_anchor_usd())
    except Exception as exc:
        print(json.dumps({
            "error": f"anchor_unavailable: {exc}",
            "hint": "Pass --anchor <usd> to override.",
        }))
        return 1

    expo = compute_cluster_exposure()
    strategies = _strategy_inventory_from_exposure(expo.get("positions", []) or [])

    # Even when we have live positions, always evaluate the full active-
    # strategy roster — the headroom view ("if this strategy were sized to
    # its cap, what would survive?") is what promotion decisions need, not
    # just what's open right now. Append synthesized rows for any strategy
    # not represented in live positions.
    ALLOC = REPO / "argus_flow" / "configs" / "allocation_factors.json"
    SYMBOL_OF_STRAT = {
        "forge_gld_pm_long": ("GLD", "long"),
        "forge_nq_overnight": ("MNQ", "long"),
        "forge_jpy_pm_short": ("USDJPY", "short"),
        "forge_aud_asian_breakout": ("AUDUSD", "long"),
        "forge_wick_gbpusd": ("GBPUSD", "long"),
        "forge_spy_trend_follower": ("SPY", "long"),
        "forge_gdx_gld": ("GLD", "long"),
        "forge_fomc_drift": ("SPY", "long"),
        "forge_tom_international": ("EEM", "long"),
        "forge_mamba": ("YM", "long"),
        "forge_tori": ("YM", "long"),
        "forge_cuebanks": ("MYM", "long"),
        "forge_vix_revert": ("UVXY", "short"),
        "forge_rebalance": ("SPY", "long"),
        "argus_gbpusd": ("GBPUSD", "long"),
        "argus_usdjpy": ("USDJPY", "long"),
        "argus_cadjpy": ("CADJPY", "long"),
    }
    try:
        alloc = json.loads(ALLOC.read_text(encoding="utf-8"))
        factors = alloc.get("factors", {}) or {}
    except Exception:
        factors = {}

    from helio.cluster_exposure import (
        PER_STRATEGY_NOTIONAL_CAP_X, PER_STRATEGY_DEFAULT_CAP,
    )
    represented = {s["strategy"] for s in strategies}
    for strat, (sym, direction) in SYMBOL_OF_STRAT.items():
        if factors.get(strat, 1.0) == 0.0:
            continue  # killed strategies skip
        if strat in represented:
            continue  # already covered by a live position row
        cap_x = PER_STRATEGY_NOTIONAL_CAP_X.get(strat, PER_STRATEGY_DEFAULT_CAP)
        strategies.append({
            "strategy": strat,
            "symbol": sym,
            "direction": direction,
            "current_notional_usd": cap_x * anchor,
        })

    report = cs.stress_fleet(
        strategies=strategies,
        anchor_usd=anchor,
        multipliers=tuple(args.multipliers),
    )
    report["ts"] = datetime.now(timezone.utc).isoformat()

    try:
        epoch = ee.current_epoch()
        ee.stamp_report(report, epoch=epoch)
    except Exception as exc:
        report["epoch_warning"] = f"epoch_unreadable: {exc}"

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "capacity_stress.json"
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    # Compact stdout summary
    print(f"capacity_stress report written to {out_path}")
    print(f"anchor=${anchor:,.0f}  strategies={len(strategies)}  epoch={report.get('epoch_id', '?')}")
    print()
    print(f"{'strategy':30s}  {'symbol':10s}  {'current_$':>10s}  {'max_safe_mult':>13s}")
    print("-" * 70)
    for s in report["per_strategy"]:
        print(f"{s['strategy'][:30]:30s}  {s['symbol']:10s}  "
              f"${s['current_notional_usd']:>9,.0f}  "
              f"{s['max_safe_multiplier']:>13.1f}x")
    print()
    print("survivors by multiplier:")
    for m, names in report.get("fleet_survivors_by_multiplier", {}).items():
        print(f"  {m:.0f}x: {len(names)}/{len(strategies)} survive  {names}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
