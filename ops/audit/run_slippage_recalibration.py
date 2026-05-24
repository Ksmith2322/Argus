"""Slippage recalibration audit (2026-05-23 rigor sprint).

The committed promotion_gate_baseline v2 recorded both
`slippage_bps_used` (the bps the published CI was computed at) and
`_slippage_bps_realistic` (Agent 2's calibration to actual venue/instrument
characteristics). For PEAD (10→40 bps) and NQ overnight (3→7 bps) the
realistic figure is materially higher than what the baseline used.

This script reruns each strategy's backtest, then evaluates per-trade
PnL through the disciplined gate at BOTH the published and the realistic
slippage levels. Output reveals whether the edge survives more honest
execution assumptions.

USAGE:
    python -m ops.audit.run_slippage_recalibration                   # both
    python -m ops.audit.run_slippage_recalibration --only pead       # just one
    python -m ops.audit.run_slippage_recalibration --no-nq           # skip

OUTPUT:
    ops/reports/system_audit/slippage_recalibration_<UTC>.json
    ops/reports/system_audit/slippage_recalibration_summary.md
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from helio.promotion_panel import run_panel, render_panel_report


REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


# ─── strategy adapters: return (label, gross_pnls_pct, entry_dates) ────

def _pead_pnls(period: str = "5y") -> tuple[str, list[float], list]:
    from forge.pead.runner import backtest as pead_backtest
    print("[pead] running 5y backtest on Apollo curated universe...")
    result = pead_backtest(period=period, verbose=False, return_trades=True)
    trades = result.get("trades_detail") or []
    pnls = [float(t["pnl_pct"]) for t in trades]
    dates = [t.get("entry_date") for t in trades]
    print(f"[pead] {len(pnls)} trades collected (point PF={result.get('profit_factor')})")
    return "forge_pead (curated Apollo 16)", pnls, dates


def _nq_pnls(period: str = "2y") -> tuple[str, list[float], list]:
    from forge.nq_overnight.runner import backtest as nq_backtest
    print(f"[nq_overnight] running {period} backtest on NQ=F 1h...")
    trades = nq_backtest(period=period, return_trades=True)
    if trades is None:
        trades = []
    pnls = [float(t["pnl_pct"]) for t in trades]
    dates = [str(t.get("date")) for t in trades]
    print(f"[nq_overnight] {len(pnls)} trades collected")
    return "forge_nq_overnight (NQ=F 1h)", pnls, dates


# ─── runner ────────────────────────────────────────────────────────────

def _evaluate(label: str, pnls: list[float], dates: list,
              slippage_levels_bps: tuple[float, ...]) -> dict:
    """Run promotion_panel at the given slippage levels; return summary."""
    if not pnls:
        return {
            "label": label, "n_trades": 0,
            "slippage_levels_bps": list(slippage_levels_bps),
            "note": "no trades collected — backtest returned empty",
            "verdicts_per_slip": {},
        }
    report = run_panel(
        pnls,
        label=label,
        entry_dates=dates if any(dates) else None,
        slippage_levels_bps=slippage_levels_bps,
    )
    layers = report.to_dict()["layers"]
    # Group layers by slippage. The panel emits layers named
    # iid_bootstrap_slip_{N}bp + block_bootstrap_b{B}_slip_{N}bp +
    # period_stability_{H}_slip_{N}bp + p_value_block_bootstrap.
    # For each slippage level we report the IID + block layers and the
    # H1/H2 stability if it was computed at that slip.
    per_slip: dict = {}
    for slip in slippage_levels_bps:
        slip_layers = [l for l in layers
                        if f"_slip_{slip:.0f}bp" in l["name"]]
        if not slip_layers:
            continue
        per_slip[f"{slip:.0f}bp"] = {
            "layers": slip_layers,
            "iid_ci_lower": next(
                (l["ci_lower"] for l in slip_layers
                 if l["name"].startswith("iid_bootstrap_")), None),
            "min_layer_passes": all(l["passes"] for l in slip_layers),
        }
    return {
        "label": label,
        "n_trades": len(pnls),
        "slippage_levels_bps": list(slippage_levels_bps),
        "headline_verdict": report.headline_verdict,
        "n_layers_passed": report.n_layers_passed,
        "n_layers_total": report.n_layers_total,
        "p_value_full_block": report.p_value,
        "verdicts_per_slip": per_slip,
        "report_text": render_panel_report(report),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=["pead", "nq"], default=None,
                        help="Run only the named strategy")
    parser.add_argument("--no-pead", action="store_true")
    parser.add_argument("--no-nq", action="store_true")
    parser.add_argument("--pead-period", default="5y")
    parser.add_argument("--nq-period", default="2y")
    args = parser.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []

    if (args.only in (None, "pead")) and not args.no_pead:
        try:
            label, pnls, dates = _pead_pnls(period=args.pead_period)
            # Published baseline used 10bp; realistic is 40bp per
            # promotion_gate_baseline.json v2 (PEAD entries hit
            # earnings-week wide spreads).
            results.append(_evaluate(label, pnls, dates,
                                       slippage_levels_bps=(10.0, 40.0)))
        except Exception as exc:
            print(f"[pead] failed: {exc}")
            results.append({"label": "forge_pead", "error": str(exc)})

    if (args.only in (None, "nq")) and not args.no_nq:
        try:
            label, pnls, dates = _nq_pnls(period=args.nq_period)
            # Published baseline used 3bp; realistic is 7bp per
            # promotion_gate_baseline.json v2 (overnight MNQ spread
            # widens into ETH; MNQ wider than full NQ).
            results.append(_evaluate(label, pnls, dates,
                                       slippage_levels_bps=(3.0, 7.0)))
        except Exception as exc:
            print(f"[nq_overnight] failed: {exc}")
            results.append({"label": "forge_nq_overnight", "error": str(exc)})

    # JSON + Markdown report
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_json = OUT_DIR / f"slippage_recalibration_{ts}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "Re-run published-baseline strategies at the realistic "
                     "slippage levels recorded in promotion_gate_baseline v2",
        "strategies": results,
    }
    out_json.write_text(json.dumps(payload, indent=2, default=str),
                          encoding="utf-8")

    md_lines: list[str] = []
    md_lines.append("# Slippage Recalibration Report")
    md_lines.append("")
    md_lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    md_lines.append("")
    md_lines.append(
        "Compares the disciplined gate verdict at the published "
        "`slippage_bps_used` vs the realistic `_slippage_bps_realistic` "
        "recorded in `argus_flow/configs/promotion_gate_baseline.json` (v2)."
    )
    md_lines.append("")
    for r in results:
        if "error" in r:
            md_lines.append(f"## {r['label']} — ERROR\n\n`{r['error']}`\n")
            continue
        md_lines.append(f"## {r['label']}\n")
        md_lines.append(f"- n_trades: **{r['n_trades']}**")
        md_lines.append(f"- headline verdict (combined): "
                          f"**{r.get('headline_verdict', 'n/a')}** "
                          f"({r.get('n_layers_passed', 0)}/{r.get('n_layers_total', 0)} layers)")
        md_lines.append(f"- p-value (block bootstrap, P[PF<=1.20]): "
                          f"{r.get('p_value_full_block')}")
        md_lines.append("")
        md_lines.append("| Slippage | IID CI lower | All layers pass? |")
        md_lines.append("|---|---|---|")
        for slip_key, slip_info in r.get("verdicts_per_slip", {}).items():
            md_lines.append(
                f"| {slip_key} | {slip_info.get('iid_ci_lower')} | "
                f"{'YES' if slip_info.get('min_layer_passes') else 'NO'} |"
            )
        md_lines.append("")
        md_lines.append("<details><summary>Full panel report</summary>\n")
        md_lines.append("```")
        md_lines.append(r.get("report_text", "(no text report)"))
        md_lines.append("```")
        md_lines.append("</details>\n")

    out_md = OUT_DIR / "slippage_recalibration_summary.md"
    out_md.write_text("\n".join(md_lines), encoding="utf-8")

    # Stdout summary
    print()
    print("=" * 72)
    print("SLIPPAGE RECALIBRATION SUMMARY")
    print("=" * 72)
    for r in results:
        if "error" in r:
            print(f"  {r['label']:40s}  ERROR: {r['error']}")
            continue
        per_slip = r.get("verdicts_per_slip", {})
        for slip_key, slip_info in per_slip.items():
            ci_lower = slip_info.get("iid_ci_lower")
            passes = "PASS" if slip_info.get("min_layer_passes") else "FAIL"
            print(f"  {r['label']:40s}  slip={slip_key:>5s}  "
                  f"IID CI lower={ci_lower:.3f}  layers={passes}")
    print()
    print(f"JSON:     {out_json}")
    print(f"Markdown: {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
