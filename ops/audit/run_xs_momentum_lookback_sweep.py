"""xs_momentum lookback-period sweep.

Live strategy uses lookback (long=252, short=21) — the canonical
"12-1 momentum" of AQR/Asness. Question: does a different lookback
window do better on our universes?

Test grid:
  (252, 21)   — 12-1 (production)
  (504, 21)   — 24-1 (longer-horizon momentum)
  (126, 21)   — 6-1 (medium)
  (63, 21)    — 3-1 (short)
  (252, 42)   — 12-2 (deeper recency exclusion)

Survivor: PF CI lower >= 1.20 at 10bp slippage over 20y.

USAGE
-----
    python -m ops.audit.run_xs_momentum_lookback_sweep
    python -m ops.audit.run_xs_momentum_lookback_sweep --universe style_factors_8
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"

SURVIVOR_PF_FLOOR = 1.20

# Lookback variants to test
LOOKBACK_GRID = [
    (252, 21, "12_1_production"),
    (504, 21, "24_1_long"),
    (126, 21, "6_1_medium"),
    (63, 21, "3_1_short"),
    (252, 42, "12_2_deep_exclude"),
]

# Universes to test each lookback on — use the surviving universes
# from the previous sweep (sectors_spdr_11, style_factors_8,
# legacy_sectors_countries_15) plus broad-8 for baseline.
UNIVERSES_TO_TEST = [
    ("broad_8",              None),
    ("sectors_spdr_11",      "sectors_spdr_11"),
    ("style_factors_8",      "style_factors_8"),
    ("legacy_15",            "legacy_sectors_countries_15"),
]


def _run_one(
    universe_label: str,
    universe_key: str | None,
    long_lb: int,
    short_lb: int,
    variant_label: str,
    *,
    period: str = "20y",
    slippage_bps: float = 10.0,
) -> dict:
    """Backtest one (universe, lookback) combo + score."""
    try:
        from forge.xs_momentum.runner import backtest
        from helio.xs_momentum_universes import get_universe
        from helio.xs_momentum import DEFAULT_UNIVERSE
        from helio.bootstrap_stats import bootstrap_profit_factor
    except Exception as exc:
        return {"universe": universe_label, "variant": variant_label,
                "error": f"import: {exc}"}

    universe_override = (
        list(get_universe(universe_key)) if universe_key
        else list(DEFAULT_UNIVERSE)
    )

    # Monkey-patch the lookback for this run only. The backtest
    # function reads from PARAMS, so save/restore the values.
    try:
        from forge.xs_momentum import runner as xs
    except Exception as exc:
        return {"universe": universe_label, "variant": variant_label,
                "error": f"import runner: {exc}"}
    saved_long = xs.PARAMS["long_lookback"]
    saved_short = xs.PARAMS["short_lookback"]
    try:
        xs.PARAMS["long_lookback"] = long_lb
        xs.PARAMS["short_lookback"] = short_lb
        result = backtest(
            period=period,
            universe_override=universe_override,
        )
    except Exception as exc:
        xs.PARAMS["long_lookback"] = saved_long
        xs.PARAMS["short_lookback"] = saved_short
        return {"universe": universe_label, "variant": variant_label,
                "error": f"backtest: {exc}"}
    finally:
        xs.PARAMS["long_lookback"] = saved_long
        xs.PARAMS["short_lookback"] = saved_short

    if "error" in result:
        return {"universe": universe_label, "variant": variant_label,
                "error": result["error"]}

    trades = result.get("trades_detail") or []
    if not trades:
        return {"universe": universe_label, "variant": variant_label,
                "error": "no trades"}

    drag = 2.0 * (slippage_bps / 100.0)
    pnls = [t["pnl_pct"] - drag for t in trades]

    try:
        boot = bootstrap_profit_factor(pnls, n_resamples=2000)
        ci_lo = boot.ci_lower
        ci_hi = boot.ci_upper
        pf_point = boot.point
    except Exception as exc:
        return {"universe": universe_label, "variant": variant_label,
                "error": f"bootstrap: {exc}"}

    return {
        "universe": universe_label,
        "variant": variant_label,
        "long_lookback": long_lb,
        "short_lookback": short_lb,
        "universe_size": result.get("universe_size"),
        "n_trades": result.get("trades"),
        "win_rate": result.get("win_rate"),
        "pf_point": round(pf_point, 2),
        "ci_95_lower": round(ci_lo, 2),
        "ci_95_upper": round(ci_hi, 2),
        "cagr_pct": result.get("cagr_pct"),
        "max_dd_pct": result.get("max_drawdown_pct"),
        "survivor": ci_lo >= SURVIVOR_PF_FLOOR,
    }


def _render_markdown(rows: list[dict]) -> str:
    out = [
        "# xs_momentum lookback sweep",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Slippage: 10 bps. Survivor floor: PF CI lower >= {SURVIVOR_PF_FLOOR}",
        "",
    ]
    survivors = [r for r in rows if r.get("survivor")]
    out.append(f"**Survivors**: {len(survivors)} / {len(rows)}")
    out.append("")
    out.append("| Universe | Lookback | n | PF | CI lower | CAGR | DD | Survivor |")
    out.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        if r.get("error"):
            out.append(f"| {r['universe']} | {r['variant']} | — | — | — | — | — | ERR {r['error'][:30]} |")
            continue
        out.append(
            f"| {r['universe']} | {r['variant']} ({r['long_lookback']}-{r['short_lookback']}) | "
            f"{r['n_trades']} | {r['pf_point']:.2f} | "
            f"{r['ci_95_lower']:.2f} | {r['cagr_pct']:.1f}% | "
            f"{r['max_dd_pct']:.1f}% | "
            f"{'YES' if r['survivor'] else 'no'} |"
        )
    if survivors:
        out.append("")
        out.append("## Survivors (deploy candidates)")
        out.append("")
        for r in survivors:
            out.append(
                f"- **{r['universe']} @ {r['variant']}**: "
                f"PF {r['pf_point']:.2f} CI [{r['ci_95_lower']:.2f}, "
                f"{r['ci_95_upper']:.2f}], CAGR {r['cagr_pct']:.1f}%, "
                f"DD {r['max_dd_pct']:.1f}%, n={r['n_trades']}"
            )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", default=None,
                        help="Run only one universe label")
    parser.add_argument("--period", default="20y")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.universe:
        universes = [u for u in UNIVERSES_TO_TEST if u[0] == args.universe]
        if not universes:
            print(f"ERROR: unknown universe label; "
                  f"known: {[u[0] for u in UNIVERSES_TO_TEST]}")
            return 2
    else:
        universes = UNIVERSES_TO_TEST

    rows: list[dict] = []
    for u_label, u_key in universes:
        for long_lb, short_lb, v_label in LOOKBACK_GRID:
            print(f"  {u_label} x {v_label} ({long_lb}-{short_lb})...",
                  file=sys.stderr, end=" ", flush=True)
            r = _run_one(u_label, u_key, long_lb, short_lb, v_label,
                         period=args.period)
            rows.append(r)
            if r.get("error"):
                print(f"ERR {r['error']}", file=sys.stderr)
            else:
                print(
                    f"n={r['n_trades']} PF={r['pf_point']:.2f} "
                    f"CI_lo={r['ci_95_lower']:.2f} "
                    f"{'SURVIVOR' if r['survivor'] else ''}",
                    file=sys.stderr,
                )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "xs_momentum_lookback_sweep.json").write_text(
        json.dumps(rows, indent=2, default=str), encoding="utf-8")

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
    else:
        print(_render_markdown(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
