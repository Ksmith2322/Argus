"""Evaluate forge.dual_trend through the disciplined gate + cross-era OOS."""
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
    parser.add_argument("--period", default="20y")
    parser.add_argument("--lookback", type=int, default=12)
    parser.add_argument("--slippage-bps", type=float, default=10.0)
    args = parser.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[1] running dual_trend backtest (period={args.period}, "
          f"lookback={args.lookback}mo)...")
    from forge.dual_trend.backtest import (
        backtest, per_trade_pnls, per_trade_dates,
    )
    bt = backtest(period=args.period, lookback_months=args.lookback)
    if "error" in bt:
        print(f"backtest error: {bt['error']}")
        return 1
    print(f"  n_trades(asset-months)={bt['n_trades_assetmonths']}  "
          f"n_portfolio_months={bt['n_portfolio_months']}  "
          f"fraction_invested={bt['fraction_time_invested']:.2f}")
    print(f"  PF={bt['profit_factor']}  WR={bt['win_rate']*100:.1f}%  "
          f"avg_per_assetmonth={bt['avg_pct_per_assetmonth']:+.3f}%")
    print(f"  CAGR={bt['cagr_pct']}%  max DD={bt['max_drawdown_pct']}%")

    print()
    print(f"[2] disciplined gate at {args.slippage_bps}bp slippage...")
    from helio.promotion_panel import run_panel, render_panel_report
    pnls = per_trade_pnls(bt)
    dates = per_trade_dates(bt)
    report = run_panel(
        pnls,
        label=f"forge_dual_trend (broad-8 lookback={args.lookback}mo)",
        entry_dates=dates,
        slippage_levels_bps=(0.0, args.slippage_bps),
        promotion_floor=1.20,
    )
    text = render_panel_report(report)
    print(text)

    # Cross-era OOS using full max history
    print()
    print("[3] cross-decade OOS (full max history)...")
    bt_max = backtest(period="max", lookback_months=args.lookback)
    trades_max = bt_max.get("trades_detail") or []
    from helio.bootstrap_stats import bootstrap_profit_factor

    def _era(label: str, start: str, end: str) -> dict:
        era = [t for t in trades_max
                 if start <= t["month"] < end]
        if len(era) < 10:
            return {"label": label, "n": len(era), "note": "insufficient"}
        era_pnls = [t["pnl_pct"] - args.slippage_bps / 100.0 for t in era]
        bs = bootstrap_profit_factor(era_pnls, n_resamples=3000, seed=42)
        wins = sum(1 for p in era_pnls if p > 0)
        return {
            "label": label,
            "n": len(era),
            "pf_point": round(abs(sum(p for p in era_pnls if p > 0) /
                                       sum(p for p in era_pnls if p < 0))
                                if any(p < 0 for p in era_pnls) else float("inf"), 3),
            "ci_lower": round(bs.ci_lower, 3),
            "ci_upper": round(bs.ci_upper, 3),
            "win_rate": round(wins / len(era_pnls), 3),
            "passes_at_slip": bs.ci_lower >= 1.20,
        }

    eras = [_era("2006-2016", "2006-01", "2016-01"),
              _era("2016-2026", "2016-01", "2026-12")]
    for er in eras:
        if "note" in er:
            print(f"  {er['label']:<14} {er.get('note', 'n/a')} (n={er.get('n', 0)})")
            continue
        ok = "YES" if er["passes_at_slip"] else "no"
        print(f"  {er['label']:<14} n={er['n']:>5}  PF={er['pf_point']:>5.2f}  "
              f"CI=[{er['ci_lower']:>5.2f}, {er['ci_upper']:>5.2f}]  "
              f"WR={er['win_rate']*100:>5.1f}%  pass@{args.slippage_bps:.0f}bp?={ok}")

    # Persist
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = OUT_DIR / f"dual_trend_evaluation_{ts}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "params": {"period": args.period, "lookback_months": args.lookback,
                     "slippage_bps": args.slippage_bps},
        "backtest_summary": {k: v for k, v in bt.items()
                                if k not in ("trades_detail",
                                              "monthly_portfolio_returns")},
        "disciplined_gate": report.to_dict(),
        "era_oos": eras,
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str),
                          encoding="utf-8")

    md_path = OUT_DIR / "dual_trend_evaluation.md"
    lines = ["# forge_dual_trend disciplined-gate evaluation", "",
              f"Generated: {datetime.now(timezone.utc).isoformat()}", "",
              "## Backtest summary", ""]
    for k, v in payload["backtest_summary"].items():
        lines.append(f"- **{k}**: {v}")
    lines.extend(["", "## Disciplined gate", "", "```", text, "```", "",
                    "## Cross-era OOS", "",
                    "| Era | n | PF | CI lower | CI upper | WR | pass? |",
                    "|---|---|---|---|---|---|---|"])
    for er in eras:
        if "note" in er:
            continue
        lines.append(
            f"| {er['label']} | {er['n']} | {er['pf_point']:.2f} | "
            f"{er['ci_lower']:.2f} | {er['ci_upper']:.2f} | "
            f"{er['win_rate']*100:.1f}% | "
            f"{'YES' if er['passes_at_slip'] else 'no'} |"
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")

    print()
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
