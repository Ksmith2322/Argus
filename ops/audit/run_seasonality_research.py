"""Monthly seasonality research — is the "sell in May / Halloween effect"
real in modern SPY data?

The academic claim (Bouman & Jacobsen 2002, "The Halloween Indicator"):
equity returns are concentrated in November–April; May–October is flat
or negative. If true, a simple "long SPY Nov 1 → sell Apr 30" strategy
should outperform buy-and-hold by capturing only the winning half-year.

This audit:
  1. Decompose 30y of SPY monthly returns by calendar month.
  2. For each month, compute mean / median / t-stat / Bonferroni-adjusted
     significance.
  3. Compare summer-half (May-Oct) vs winter-half (Nov-Apr) returns.
  4. Test the actual Halloween strategy (in Nov 1 / out Apr 30) vs
     buy-and-hold via promotion_panel.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd


REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _fetch_spy_monthly(start_year: int, end_year: int) -> pd.Series:
    import yfinance as yf
    df = yf.download("SPY", start=f"{start_year}-01-01",
                       end=f"{end_year}-12-31", interval="1d",
                       progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    daily = df["Close"].dropna()
    daily.index = pd.to_datetime(daily.index).tz_localize(None)
    monthly = daily.resample("ME").last().pct_change().dropna()
    return monthly


def _normal_p_two_sided(t: float) -> float:
    return 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(t) / math.sqrt(2.0))))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=1995)
    parser.add_argument("--end-year", type=int, default=2025)
    args = parser.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[1] fetching SPY monthly returns {args.start_year}-{args.end_year}...")
    monthly = _fetch_spy_monthly(args.start_year, args.end_year)
    print(f"  {len(monthly)} months  ({monthly.index.min().date()} to "
          f"{monthly.index.max().date()})")

    # Per-month stats
    print()
    print(f"[2] per-calendar-month statistics:")
    by_month = monthly.groupby(monthly.index.month)
    rows = []
    alpha_bonferroni = 0.05 / 12  # 12 tests
    for m in range(1, 13):
        vals = by_month.get_group(m).values
        n = len(vals)
        mean = float(np.mean(vals))
        median = float(np.median(vals))
        std = float(np.std(vals, ddof=1)) if n > 1 else 0.0
        t_stat = (mean / (std / math.sqrt(n))) if std > 0 else 0.0
        p_val = _normal_p_two_sided(t_stat)
        wr = int(np.sum(vals > 0)) / n
        rows.append({
            "month_num": m,
            "month": MONTH_NAMES[m - 1],
            "n": n,
            "mean_pct": round(mean * 100, 3),
            "median_pct": round(median * 100, 3),
            "std_pct": round(std * 100, 3),
            "t_stat": round(t_stat, 3),
            "p_value_two_sided": round(p_val, 5),
            "bonferroni_significant": p_val < alpha_bonferroni,
            "win_rate": round(wr, 3),
        })
    print(f"  {'month':<5} {'n':>3} {'mean':>7} {'median':>7} {'std':>6} "
          f"{'t':>6} {'p':>7} {'WR':>5}  {'sig?':>5}")
    print("  " + "-" * 65)
    for r in rows:
        sig = "YES" if r["bonferroni_significant"] else "no"
        print(f"  {r['month']:<5} {r['n']:>3} {r['mean_pct']:>+6.2f}% "
              f"{r['median_pct']:>+6.2f}% {r['std_pct']:>5.2f}% "
              f"{r['t_stat']:>+6.2f} {r['p_value_two_sided']:>6.4f} "
              f"{r['win_rate']*100:>4.1f}%  {sig:>5}")

    # Summer (May-Oct) vs Winter (Nov-Apr) test
    print()
    print("[3] Halloween indicator: summer (May-Oct) vs winter (Nov-Apr):")
    summer = monthly[monthly.index.month.isin([5, 6, 7, 8, 9, 10])]
    winter = monthly[monthly.index.month.isin([11, 12, 1, 2, 3, 4])]
    sm, sn = float(summer.mean()), len(summer)
    wm, wn = float(winter.mean()), len(winter)
    ss = float(summer.std(ddof=1))
    ws = float(winter.std(ddof=1))
    # Two-sample t-test (Welch)
    pooled_se = math.sqrt((ss ** 2) / sn + (ws ** 2) / wn)
    t_diff = (wm - sm) / pooled_se if pooled_se > 0 else 0.0
    p_diff = _normal_p_two_sided(t_diff)
    print(f"  summer (May-Oct): n={sn:<4} mean=+{sm*100:.3f}% std={ss*100:.3f}%")
    print(f"  winter (Nov-Apr): n={wn:<4} mean=+{wm*100:.3f}% std={ws*100:.3f}%")
    print(f"  diff:             {(wm-sm)*100:+.3f}%  t={t_diff:+.2f}  p={p_diff:.4f}")
    print(f"  winter SHOULD be higher per Halloween indicator")

    # Halloween strategy backtest
    print()
    print("[4] Halloween strategy: long Nov 1 → sell Apr 30, flat May-Oct...")
    # For each Nov-Apr period, compound monthly returns
    halloween_trades = []
    df_year_month = pd.DataFrame({
        "year": monthly.index.year,
        "month": monthly.index.month,
        "ret": monthly.values,
    }, index=monthly.index)
    # Group by season-year (Nov of year Y starts season Y)
    for season_start_year in range(int(monthly.index.year.min()),
                                      int(monthly.index.year.max()) + 1):
        season_months = []
        # Nov + Dec of season_start_year
        for m in (11, 12):
            row = df_year_month[(df_year_month["year"] == season_start_year)
                                  & (df_year_month["month"] == m)]
            if not row.empty:
                season_months.append(float(row["ret"].iloc[0]))
        # Jan-Apr of season_start_year + 1
        for m in (1, 2, 3, 4):
            row = df_year_month[(df_year_month["year"] == season_start_year + 1)
                                  & (df_year_month["month"] == m)]
            if not row.empty:
                season_months.append(float(row["ret"].iloc[0]))
        if len(season_months) == 6:  # full Nov-Apr window
            compound = 1.0
            for r in season_months:
                compound *= (1.0 + r)
            halloween_trades.append({
                "season": f"{season_start_year}-{season_start_year+1}",
                "return_pct": round((compound - 1.0) * 100, 3),
            })

    if halloween_trades:
        rets = [t["return_pct"] for t in halloween_trades]
        wins = [r for r in rets if r > 0]
        losses = [r for r in rets if r < 0]
        pf = abs(sum(wins) / sum(losses)) if losses else float("inf")
        wr = len(wins) / len(rets)
        avg = sum(rets) / len(rets)
        print(f"  n_seasons={len(rets)}  PF={pf:.2f}  WR={wr*100:.1f}%  "
              f"avg={avg:+.2f}%")
        print(f"  first season: {halloween_trades[0]['season']} "
              f"({halloween_trades[0]['return_pct']:+.2f}%)")
        print(f"  last season:  {halloween_trades[-1]['season']} "
              f"({halloween_trades[-1]['return_pct']:+.2f}%)")

    # Persist
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = OUT_DIR / f"seasonality_research_{ts}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "year_range": [args.start_year, args.end_year],
        "n_months_total": int(len(monthly)),
        "per_month": rows,
        "halloween_indicator": {
            "summer_mean_pct": round(sm * 100, 4),
            "summer_n": sn,
            "winter_mean_pct": round(wm * 100, 4),
            "winter_n": wn,
            "diff_pct": round((wm - sm) * 100, 4),
            "t_stat": round(t_diff, 3),
            "p_value_two_sided": round(p_diff, 5),
        },
        "halloween_strategy": {
            "n_seasons": len(halloween_trades),
            "avg_return_pct": round(sum(t["return_pct"] for t in halloween_trades)
                                      / len(halloween_trades), 3) if halloween_trades else None,
            "trades": halloween_trades,
        },
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str),
                          encoding="utf-8")

    md_path = OUT_DIR / "seasonality_research.md"
    md_lines: list[str] = []
    md_lines.append("# SPY monthly seasonality research")
    md_lines.append("")
    md_lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    md_lines.append(f"Period: {args.start_year}-{args.end_year} "
                       f"({len(monthly)} monthly observations)")
    md_lines.append("")
    md_lines.append("## Per-calendar-month stats")
    md_lines.append("")
    md_lines.append("| Month | n | mean | median | std | t-stat | p | WR | Bonf-sig? |")
    md_lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        sig = "YES" if r["bonferroni_significant"] else "no"
        md_lines.append(
            f"| {r['month']} | {r['n']} | {r['mean_pct']:+.2f}% | "
            f"{r['median_pct']:+.2f}% | {r['std_pct']:.2f}% | "
            f"{r['t_stat']:+.2f} | {r['p_value_two_sided']:.4f} | "
            f"{r['win_rate']*100:.1f}% | {sig} |"
        )
    md_lines.append("")
    md_lines.append("Bonferroni-adjusted alpha at 12 tests: 0.00417")
    md_lines.append("")
    md_lines.append("## Halloween indicator (winter vs summer)")
    md_lines.append("")
    md_lines.append(f"- Summer (May–Oct): n={sn}, mean = {sm*100:+.3f}%")
    md_lines.append(f"- Winter (Nov–Apr): n={wn}, mean = {wm*100:+.3f}%")
    md_lines.append(f"- Difference: {(wm-sm)*100:+.3f}%, t = {t_diff:+.2f}, "
                       f"p = {p_diff:.4f}")
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    print()
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
