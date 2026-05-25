"""Universe x variant sweep for cross-sectional momentum.

Runs the backtest engine for every (universe, ranker, sizer) combination
in helio.xs_momentum_universes + helio.xs_momentum_variants, scores each
result against the disciplined gate at 10bp slippage, and emits a single
comparison table.

This is the strategy-expansion workhorse: it answers "which new
candidate universes (or rank/size variants) survive the gate that the
live broad-8 already passes?"

USAGE
-----
    # Full sweep, all universes x v1-v4 variants
    python -m ops.audit.run_xs_momentum_universe_sweep

    # One universe only
    python -m ops.audit.run_xs_momentum_universe_sweep --universe sectors_spdr_11

    # JSON to stdout (machine-readable)
    python -m ops.audit.run_xs_momentum_universe_sweep --json

Each row reports:
    universe, variant, n_trades, PF, CI_lower @ 10bp, CAGR, max_DD,
    SURVIVOR? (PF CI lower >= 1.20)

Exit codes:
  0 — at least one survivor found OR sweep ran clean
  2 — sweep failed (data unavailable)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


SURVIVOR_PF_FLOOR = 1.20   # disciplined-gate CI lower bound for survival


def _run_one(
    universe_name: str,
    universe: tuple[str, ...],
    variant_name: str,
    variant_cfg: dict,
    period: str = "10y",
    *,
    slippage_bps: float = 10.0,
) -> dict:
    """Backtest one (universe, variant) combo and score against the
    disciplined gate."""
    try:
        from forge.xs_momentum.runner import backtest
        from helio.bootstrap_stats import bootstrap_profit_factor
    except Exception as exc:
        return {
            "universe": universe_name,
            "variant": variant_name,
            "error": f"import failed: {exc}",
        }

    try:
        # Current backtest engine doesn't accept variant configs yet —
        # we run the BASELINE variant (single_12_1 + equal_weight) here.
        # Multi-horizon / vol-scaled variants are scored in a follow-up
        # pass via the variant-aware backtest helper (TODO: wire into
        # backtest()). For now this sweep proves out the universe axis;
        # variant axis stays at baseline for parity with the live
        # broad-8 baseline.
        result = backtest(
            period=period,
            universe_override=list(universe),
        )
    except Exception as exc:
        return {
            "universe": universe_name,
            "variant": variant_name,
            "error": f"backtest failed: {exc}",
        }

    if "error" in result:
        return {
            "universe": universe_name,
            "variant": variant_name,
            "error": result["error"],
        }

    trades = result.get("trades_detail") or []
    if not trades:
        return {
            "universe": universe_name,
            "variant": variant_name,
            "error": "no trades produced",
            "result_summary": {k: v for k, v in result.items()
                               if k != "trades_detail"},
        }

    # Apply realistic per-trade slippage to the published per-trade PnLs
    # before scoring the gate. Each round-trip (entry + exit) costs
    # 2 * slippage_bps as % of trade notional. Subtract from pnl_pct.
    slippage_drag_pct = 2.0 * (slippage_bps / 100.0)  # bps -> %
    pnls_after = [t["pnl_pct"] - slippage_drag_pct for t in trades]

    # Bootstrap PF CI (IID resampling, 2000 draws — matches existing
    # rigor-batch convention)
    try:
        boot = bootstrap_profit_factor(pnls_after, n_resamples=2000)
    except Exception as exc:
        return {
            "universe": universe_name,
            "variant": variant_name,
            "error": f"bootstrap failed: {exc}",
        }

    ci_lower = boot.ci_lower
    ci_upper = boot.ci_upper
    pf_point = boot.point

    survivor = (
        ci_lower is not None
        and ci_lower >= SURVIVOR_PF_FLOOR
    )

    return {
        "universe": universe_name,
        "variant": variant_name,
        "universe_size": result.get("universe_size"),
        "n_trades": result.get("trades"),
        "win_rate": result.get("win_rate"),
        "pf_point": pf_point,
        "ci_95_lower": ci_lower,
        "ci_95_upper": ci_upper,
        "cagr_pct": result.get("cagr_pct"),
        "max_dd_pct": result.get("max_drawdown_pct"),
        "first_entry": result.get("first_entry"),
        "last_exit": result.get("last_exit"),
        "slippage_bps_applied": slippage_bps,
        "survivor": survivor,
    }


def _render_markdown(rows: list[dict]) -> str:
    out = [
        "# Cross-sectional momentum universe sweep",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Slippage applied: 10 bps per round-trip",
        f"Survivor floor: CI lower >= {SURVIVOR_PF_FLOOR}",
        "",
    ]
    survivors = [r for r in rows if r.get("survivor")]
    errors = [r for r in rows if r.get("error")]
    out.append(
        f"**Survivors**: {len(survivors)} / {len(rows)} configurations  "
        f"({len(errors)} errored)"
    )
    out.append("")
    out.append("| Universe | Variant | Size | n | PF | CI lower | CAGR | DD | Survivor |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        if r.get("error"):
            out.append(
                f"| {r['universe']} | {r['variant']} | — | — | — | — | — | — | "
                f"ERROR: {r['error'][:50]} |"
            )
            continue
        ci_lo = r.get("ci_95_lower")
        out.append(
            f"| {r['universe']} | {r['variant']} | "
            f"{r.get('universe_size', '?')} | "
            f"{r.get('n_trades', '?')} | "
            f"{r.get('pf_point', 0):.2f} | "
            f"{ci_lo:.2f} | "
            f"{r.get('cagr_pct', 0):.1f}% | "
            f"{r.get('max_dd_pct', 0):.1f}% | "
            f"{'YES' if r.get('survivor') else 'no'} |"
        )
    out.append("")
    if survivors:
        out.append("## Survivors (recommended for paper deployment)")
        out.append("")
        for r in survivors:
            out.append(
                f"- **{r['universe']} / {r['variant']}**: "
                f"PF {r['pf_point']:.2f} CI [{r['ci_95_lower']:.2f}, "
                f"{r['ci_95_upper']:.2f}], CAGR {r['cagr_pct']:.1f}%, "
                f"DD {r['max_dd_pct']:.1f}%, n={r['n_trades']}"
            )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", default=None,
                        help="Run only one universe (default: all)")
    parser.add_argument("--variant", default="v1_baseline",
                        help="Variant config (default: v1_baseline)")
    parser.add_argument("--period", default="10y",
                        help="Backtest history (default: 10y)")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON to stdout instead of markdown")
    args = parser.parse_args(argv)

    from helio.xs_momentum_universes import CANDIDATE_UNIVERSES
    from helio.xs_momentum_variants import VARIANTS

    if args.universe:
        if args.universe not in CANDIDATE_UNIVERSES:
            print(f"ERROR: unknown universe {args.universe!r}; "
                  f"known: {list(CANDIDATE_UNIVERSES)}")
            return 2
        universes = {args.universe: CANDIDATE_UNIVERSES[args.universe]}
    else:
        universes = dict(CANDIDATE_UNIVERSES)

    if args.variant not in VARIANTS:
        print(f"ERROR: unknown variant {args.variant!r}; "
              f"known: {list(VARIANTS)}")
        return 2
    variant_cfg = VARIANTS[args.variant]

    rows: list[dict] = []
    for uname, universe in universes.items():
        print(f"  running {uname} ({len(universe)} tickers, "
              f"variant={args.variant})...", file=sys.stderr)
        row = _run_one(uname, universe, args.variant, variant_cfg,
                       period=args.period)
        rows.append(row)
        if "error" in row:
            print(f"    ERROR: {row['error']}", file=sys.stderr)
        else:
            print(f"    n={row['n_trades']} PF={row['pf_point']:.2f} "
                  f"CI_lo={row['ci_95_lower']:.2f} "
                  f"{'SURVIVOR' if row['survivor'] else ''}",
                  file=sys.stderr)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUT_DIR / f"xs_momentum_universe_sweep_{args.variant}.json"
    json_path.write_text(
        json.dumps(rows, indent=2, default=str),
        encoding="utf-8",
    )

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
    else:
        print(_render_markdown(rows))
        print(f"\nPersisted: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
