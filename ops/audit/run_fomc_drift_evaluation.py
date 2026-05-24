"""Evaluate forge.fomc_drift through the disciplined gate + cross-era OOS.

forge_fomc_drift was killed in the 5/20 sunset batch for OPERATIONAL
reasons (zero live fires — the runner wasn't wired correctly), NOT
because the edge failed. The legacy backtest reports PF 1.58 over 215
trades (2000-2026) but predates the disciplined gate methodology.

This audit runs the same per-trade returns through promotion_panel
(IID + block bootstrap, period stability, multi-testing) and through
a cross-era split. If the strategy passes, it's a candidate for
resurrection — a third diversifying strategy after tom_spy.

USAGE:
    python -m ops.audit.run_fomc_drift_evaluation
    python -m ops.audit.run_fomc_drift_evaluation --start-year 2000
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
    parser.add_argument("--start-year", type=int, default=2000)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--slippage-bps", type=float, default=5.0,
                          help="SPY ETF slippage assumption (default 5bp)")
    args = parser.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[1] running FOMC backtest {args.start_year}-{args.end_year}...")
    from forge.fomc_drift_backtest import run_backtest
    trades_df = run_backtest(args.start_year, args.end_year)
    print(f"  n_trades = {len(trades_df)}")
    if len(trades_df) < 30:
        print("ERROR: insufficient trades for meaningful disciplined-gate analysis")
        return 1

    wins = trades_df[trades_df["return_pct"] > 0]
    losses = trades_df[trades_df["return_pct"] <= 0]
    pf = (wins["return_pct"].sum() / abs(losses["return_pct"].sum())
            if len(losses) > 0 else float("inf"))
    wr = len(wins) / len(trades_df)
    print(f"  point PF = {pf:.3f}, WR = {wr*100:.1f}%, "
          f"avg = {trades_df['return_pct'].mean():+.4f}%")

    pnls = trades_df["return_pct"].astype(float).tolist()
    dates = trades_df["entry_date"].astype(str).tolist()

    print()
    print(f"[2] disciplined gate (promotion_panel) at {args.slippage_bps}bp...")
    from helio.promotion_panel import run_panel, render_panel_report
    report = run_panel(
        pnls,
        label="forge_fomc_drift (SPY 24h pre-FOMC)",
        entry_dates=dates,
        slippage_levels_bps=(0.0, args.slippage_bps, 10.0),
        promotion_floor=1.20,
    )
    panel_text = render_panel_report(report)
    print(panel_text)

    print()
    print(f"[3] cross-era OOS stability (3 sub-samples)...")
    from helio.bootstrap_stats import bootstrap_profit_factor

    def _era(label: str, start: str, end: str) -> dict:
        era_trades = trades_df[(trades_df["entry_date"] >= start)
                                  & (trades_df["entry_date"] < end)]
        if len(era_trades) < 10:
            return {"label": label, "n": len(era_trades),
                     "note": "insufficient"}
        era_pnls = (era_trades["return_pct"]
                    - args.slippage_bps / 100.0).tolist()
        wins = sum(1 for p in era_pnls if p > 0)
        bs = bootstrap_profit_factor(era_pnls, n_resamples=3000, seed=42)
        return {
            "label": label,
            "n": len(era_trades),
            "pf_point": round(abs(sum(p for p in era_pnls if p > 0) /
                                       sum(p for p in era_pnls if p < 0))
                                if any(p < 0 for p in era_pnls) else float("inf"), 3),
            "ci_lower": round(bs.ci_lower, 3),
            "ci_upper": round(bs.ci_upper, 3),
            "win_rate": round(wins / len(era_trades), 3),
            "avg_pct_after_slip": round(sum(era_pnls) / len(era_pnls), 4),
            "passes_at_slip": bs.ci_lower >= 1.20,
        }

    eras = [
        _era("2000-2010 (pre-QE)",  "2000-01-01", "2010-01-01"),
        _era("2010-2018 (QE era)",  "2010-01-01", "2018-01-01"),
        _era("2018-2026 (post-QE)", "2018-01-01", "2026-12-31"),
    ]
    for er in eras:
        if "note" in er:
            print(f"  {er['label']:<22} n={er.get('n', 0):>3}  {er['note']}")
            continue
        ok = "YES" if er["passes_at_slip"] else "no"
        print(f"  {er['label']:<22} n={er['n']:>3}  PF={er['pf_point']:>5.2f}  "
              f"CI=[{er['ci_lower']:>5.2f}, {er['ci_upper']:>5.2f}]  "
              f"WR={er['win_rate']*100:>5.1f}%  "
              f"avg={er['avg_pct_after_slip']:+.3f}%  "
              f"pass@{args.slippage_bps:.0f}bp?={ok}")

    # Persist
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = OUT_DIR / f"fomc_drift_evaluation_{ts}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "params": {
            "start_year": args.start_year,
            "end_year": args.end_year,
            "slippage_bps": args.slippage_bps,
        },
        "backtest_summary": {
            "n_trades": len(trades_df),
            "profit_factor": round(pf, 3),
            "win_rate": round(wr, 3),
            "avg_return_pct": round(float(trades_df["return_pct"].mean()), 4),
            "total_return_pct": round(float(trades_df["return_pct"].sum()), 2),
            "best_year": int(trades_df.groupby("year")["return_pct"].sum().idxmax()),
            "worst_year": int(trades_df.groupby("year")["return_pct"].sum().idxmin()),
        },
        "disciplined_gate": report.to_dict(),
        "era_oos": eras,
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str),
                          encoding="utf-8")

    md_path = OUT_DIR / "fomc_drift_evaluation.md"
    md_lines: list[str] = []
    md_lines.append("# forge_fomc_drift disciplined-gate evaluation")
    md_lines.append("")
    md_lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    md_lines.append("")
    md_lines.append("## Backtest summary")
    md_lines.append("")
    for k, v in payload["backtest_summary"].items():
        md_lines.append(f"- **{k}**: {v}")
    md_lines.append("")
    md_lines.append("## Disciplined gate")
    md_lines.append("")
    md_lines.append("```")
    md_lines.append(panel_text)
    md_lines.append("```")
    md_lines.append("")
    md_lines.append("## Cross-era OOS")
    md_lines.append("")
    md_lines.append("| Era | n | PF | CI lower | CI upper | WR | avg/trade | pass @ slip? |")
    md_lines.append("|---|---|---|---|---|---|---|---|")
    for er in eras:
        if "note" in er:
            continue
        md_lines.append(
            f"| {er['label']} | {er['n']} | {er['pf_point']:.2f} | "
            f"{er['ci_lower']:.2f} | {er['ci_upper']:.2f} | "
            f"{er['win_rate']*100:.1f}% | {er['avg_pct_after_slip']:+.3f}% | "
            f"{'YES' if er['passes_at_slip'] else 'no'} |"
        )
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    print()
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
