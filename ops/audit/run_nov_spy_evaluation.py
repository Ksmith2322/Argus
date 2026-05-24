"""Evaluate forge.nov_spy through the disciplined gate + cross-era OOS.

Tests whether the November SPY effect (Bonferroni-significant in
tonight's seasonality research) survives the disciplined gate at n=30.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default="SPY")
    parser.add_argument("--period", default="30y")
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    args = parser.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[1] backtest ({args.ticker}, period={args.period})...")
    from forge.nov_spy.backtest import backtest, per_trade_pnls, per_trade_dates
    bt = backtest(ticker=args.ticker, period=args.period)
    if "error" in bt:
        print(f"backtest error: {bt['error']}")
        return 1
    print(f"  n_trades={bt['n_trades']}  PF={bt['profit_factor']}  "
          f"WR={bt['win_rate']*100:.1f}%  avg={bt['avg_pnl_pct']:+.3f}%")
    print(f"  CAGR(when-invested)={bt['cagr_when_invested_pct']}%  "
          f"max DD={bt['max_drawdown_pct']}%")
    print(f"  SPY buy-and-hold CAGR over same window: {bt['spy_buyhold_cagr_pct']}%")

    pnls = per_trade_pnls(bt)
    dates = per_trade_dates(bt)

    print()
    print(f"[2] disciplined gate at {args.slippage_bps}bp slippage...")
    from helio.promotion_panel import run_panel, render_panel_report
    report = run_panel(
        pnls,
        label=f"forge_nov_spy ({args.ticker} full November)",
        entry_dates=dates,
        slippage_levels_bps=(0.0, args.slippage_bps, 10.0),
        promotion_floor=1.20,
        min_n_per_half=5,
    )
    panel_text = render_panel_report(report)
    print(panel_text)

    print()
    print("[3] cross-era OOS slice (pre-2000 / 2000-2010 / 2010-2026)...")
    from helio.bootstrap_stats import bootstrap_profit_factor
    trades = bt.get("trades_detail", [])
    def _era(label: str, start: int, end: int) -> dict:
        era = [t for t in trades if start <= t["year"] < end]
        if len(era) < 5:
            return {"label": label, "n": len(era), "note": "insufficient"}
        era_pnls = [t["pnl_pct"] - args.slippage_bps / 100.0 for t in era]
        wins = sum(1 for p in era_pnls if p > 0)
        bs = bootstrap_profit_factor(era_pnls, n_resamples=3000, seed=42)
        return {
            "label": label,
            "n": len(era),
            "pf_point": round(abs(sum(p for p in era_pnls if p > 0) /
                                       sum(p for p in era_pnls if p < 0))
                                if any(p < 0 for p in era_pnls) else float("inf"), 3),
            "ci_lower": round(bs.ci_lower, 3),
            "ci_upper": round(bs.ci_upper, 3),
            "win_rate": round(wins / len(era_pnls), 3),
            "avg_pct_after_slip": round(sum(era_pnls) / len(era_pnls), 4),
            "passes_at_slip": bs.ci_lower >= 1.20,
        }

    eras = [_era("pre-2000", 1990, 2000),
              _era("2000-2010", 2000, 2010),
              _era("2010-2026", 2010, 2026)]
    for er in eras:
        if "note" in er:
            print(f"  {er['label']:<14} n={er.get('n', 0):>3}  {er['note']}")
            continue
        ok = "YES" if er["passes_at_slip"] else "no"
        print(f"  {er['label']:<14} n={er['n']:>3}  PF={er['pf_point']:>5.2f}  "
              f"CI=[{er['ci_lower']:>5.2f}, {er['ci_upper']:>5.2f}]  "
              f"WR={er['win_rate']*100:>5.1f}%  avg={er['avg_pct_after_slip']:+.2f}%  "
              f"pass@{args.slippage_bps:.0f}bp?={ok}")

    # Persist
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = OUT_DIR / f"nov_spy_evaluation_{ts}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "params": {
            "ticker": args.ticker,
            "period": args.period,
            "slippage_bps": args.slippage_bps,
        },
        "backtest_summary": {k: v for k, v in bt.items() if k != "trades_detail"},
        "disciplined_gate": report.to_dict(),
        "era_oos": eras,
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str),
                          encoding="utf-8")

    md_path = OUT_DIR / "nov_spy_evaluation.md"
    md_lines = [
        "# forge_nov_spy disciplined-gate evaluation",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        f"## Backtest summary ({args.ticker}, period={args.period})",
        "",
    ]
    for k, v in payload["backtest_summary"].items():
        md_lines.append(f"- **{k}**: {v}")
    md_lines.extend(["", "## Disciplined gate", "", "```", panel_text, "```", ""])
    md_lines.append("## Cross-era OOS")
    md_lines.append("")
    md_lines.append("| Era | n | PF | CI lower | CI upper | WR | avg | pass? |")
    md_lines.append("|---|---|---|---|---|---|---|---|")
    for er in eras:
        if "note" in er:
            continue
        md_lines.append(
            f"| {er['label']} | {er['n']} | {er['pf_point']:.2f} | "
            f"{er['ci_lower']:.2f} | {er['ci_upper']:.2f} | "
            f"{er['win_rate']*100:.1f}% | {er['avg_pct_after_slip']:+.2f}% | "
            f"{'YES' if er['passes_at_slip'] else 'no'} |"
        )
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    print()
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
