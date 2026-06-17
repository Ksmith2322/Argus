"""Test xs_momentum on alternative universes vs the broad-8 baseline.

The 5/22 backtest factory finding established that the broad-8 universe
(SPY/QQQ/IWM/DIA/EFA/EEM/GLD/TLT) is the only universe configuration
that passes the disciplined gate at 10bps slippage. But two sub-universes
deserve direct comparison:

  - Sector-only (10 US sectors): XLK, XLF, XLE, XLV, XLI, XLY, XLP,
    XLU, XLB, XLRE. Tests whether intra-equity rotation captures the
    same return more cheaply (no bond / international / commodity
    exposure).

  - Country-only (8 international + US): SPY, EFA, EEM, EWG, EWJ, EWU,
    EWZ, FXI. Tests whether cross-country dispersion is the actual
    driver.

Each is run through the disciplined gate (promotion_panel) at 10bp
slippage. Compared against the published broad-8 baseline (PF 3.30,
CI [1.86, 6.30]).
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


SECTOR_UNIVERSE = ["XLK", "XLF", "XLE", "XLV", "XLI",
                    "XLY", "XLP", "XLU", "XLB", "XLRE"]
COUNTRY_UNIVERSE = ["SPY", "EFA", "EEM", "EWG", "EWJ",
                     "EWU", "EWZ", "FXI"]
BROAD_8 = ["SPY", "QQQ", "IWM", "DIA", "EFA", "EEM", "GLD", "TLT"]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", default="10y")
    parser.add_argument("--slippage-bps", type=float, default=10.0)
    args = parser.parse_args(argv)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    from forge.xs_momentum.runner import backtest
    from helio.promotion_panel import run_panel, render_panel_report

    universes = {
        "broad_8 (current xs_momentum)": BROAD_8,
        "sector_only (10 US sectors)":   SECTOR_UNIVERSE,
        "country_only (8 markets)":      COUNTRY_UNIVERSE,
    }

    results: list[dict] = []
    for name, tickers in universes.items():
        print(f"\n[{name}] running 10y backtest ({len(tickers)} tickers)...")
        try:
            bt = backtest(period=args.period, universe_override=tickers)
        except Exception as exc:
            print(f"  ERROR: {exc}")
            results.append({"name": name, "error": str(exc)})
            continue
        if "error" in bt:
            print(f"  ERROR: {bt['error']}")
            results.append({"name": name, "error": bt["error"]})
            continue
        n_trades = bt.get("trades", 0)
        pf_point = bt.get("profit_factor")
        cagr = bt.get("cagr_pct")
        max_dd = bt.get("max_drawdown_pct")
        wr = bt.get("win_rate")
        print(f"  n_trades={n_trades}  PF={pf_point}  WR={wr*100:.1f}%  "
              f"CAGR={cagr}%  max DD={max_dd}%")

        # Disciplined gate on PER-TRADE pnls (apples-to-apples with the
        # published broad-8 baseline). trades_detail is always populated
        # post 2026-05-24 patch.
        print(f"  [running promotion_panel on per-trade pnls...]")
        trades_list = bt.get("trades_detail") or []
        pnls_pct = [float(t["pnl_pct"]) for t in trades_list
                      if isinstance(t, dict) and "pnl_pct" in t]
        if len(pnls_pct) < 10:
            print(f"  insufficient pnls ({len(pnls_pct)}) for disciplined gate")
            results.append({
                "name": name, "n_trades": n_trades,
                "pf_point": pf_point, "cagr_pct": cagr,
                "max_dd_pct": max_dd, "win_rate": wr,
                "note": "insufficient pnls for promotion_panel",
            })
            continue
        report = run_panel(
            pnls_pct,
            label=f"xs_momentum on {name}",
            slippage_levels_bps=(0.0, args.slippage_bps),
            promotion_floor=1.20,
        )
        panel_text = render_panel_report(report)
        print(panel_text)
        results.append({
            "name": name,
            "n_tickers": len(tickers),
            "n_trades": n_trades,
            "n_monthly_returns": len(pnls_pct),
            "pf_point": pf_point,
            "cagr_pct": cagr,
            "max_dd_pct": max_dd,
            "win_rate": wr,
            "disciplined_gate": report.to_dict(),
        })

    # Comparison summary
    print("\n" + "=" * 88)
    print("UNIVERSE COMPARISON SUMMARY")
    print("=" * 88)
    print(f"{'universe':<35} {'PF':>6} {'CAGR':>7} {'maxDD':>7} {'verdict':>22}")
    print("-" * 88)
    for r in results:
        if "error" in r:
            print(f"{r['name']:<35}  ERROR: {r['error']}")
            continue
        verdict = r.get("disciplined_gate", {}).get(
            "headline_verdict", r.get("note", "n/a"))
        print(f"{r['name']:<35} {r.get('pf_point', 0):>6.2f} "
              f"{r.get('cagr_pct', 0):>6.2f}% "
              f"{r.get('max_dd_pct', 0):>6.2f}% "
              f"{verdict:>22}")

    # Persist
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = OUT_DIR / f"xs_momentum_universe_comparison_{ts}.json"
    json_path.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": args.period,
        "slippage_bps": args.slippage_bps,
        "results": results,
    }, indent=2, default=str), encoding="utf-8")

    md_lines = [
        "# xs_momentum alternative-universe comparison",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Summary table",
        "",
        "| Universe | n_tickers | PF | CAGR | Max DD | Headline verdict |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        if "error" in r:
            continue
        verdict = r.get("disciplined_gate", {}).get(
            "headline_verdict", r.get("note", "n/a"))
        md_lines.append(
            f"| {r['name']} | {r.get('n_tickers', '?')} | "
            f"{r.get('pf_point', 0):.2f} | {r.get('cagr_pct', 0):.2f}% | "
            f"{r.get('max_dd_pct', 0):.2f}% | {verdict} |"
        )
    md_path = OUT_DIR / "xs_momentum_universe_comparison.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    print(f"\nJSON: {json_path}")
    print(f"Markdown: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
