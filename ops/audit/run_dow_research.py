"""Day-of-week (DOW) research — is there a systematic Mon-Fri pattern
in SPY daily returns?

Literature history:
  - French (1980): Monday returns significantly negative pre-1970s
  - Various: Friday returns positive (pre-weekend optimism)
  - Post-2000 literature: effects mostly arbitraged out
  - Mehdian-Perry (2001) and later: residual Monday weakness persists
    only after Fridays that closed strongly negative ("weekend effect"
    conditional on prior return)

This audit tests:
  1. Unconditional daily mean by DOW (Mon/Tue/Wed/Thu/Fri)
  2. Bonferroni-adjusted significance at 5 tests (α/5)
  3. Era-split: pre-2000 (when effect was strong) vs post-2000
  4. Conditional weekend effect: Mon returns following negative Fri
  5. Friday-effect strategy backtest: buy Thu close, sell Fri close

USAGE:
    python -m ops.audit.run_dow_research
    python -m ops.audit.run_dow_research --start-year 1995 --end-year 2025
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

DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri"]  # weekday() 0-4


def _fetch_spy_daily(start_year: int, end_year: int) -> pd.DataFrame:
    import yfinance as yf
    df = yf.download("SPY", start=f"{start_year}-01-01",
                       end=f"{end_year}-12-31", interval="1d",
                       progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Close"]].dropna()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df["ret"] = df["Close"].pct_change()
    df["dow"] = df.index.dayofweek
    return df.dropna()


def _normal_p_two_sided(t: float) -> float:
    return 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(t) / math.sqrt(2.0))))


def _summarize_by_dow(daily: pd.DataFrame, *, label: str,
                       alpha_bonferroni: float = 0.05 / 5) -> list[dict]:
    rows = []
    grouped = daily.groupby("dow")
    for d, name in enumerate(DOW_NAMES):
        try:
            group = grouped.get_group(d)
        except KeyError:
            continue
        vals = group["ret"].values
        n = len(vals)
        if n < 10:
            continue
        mean = float(np.mean(vals))
        std = float(np.std(vals, ddof=1)) if n > 1 else 0.0
        t_stat = mean / (std / math.sqrt(n)) if std > 0 else 0.0
        p_val = _normal_p_two_sided(t_stat)
        wr = int(np.sum(vals > 0)) / n
        rows.append({
            "label": label,
            "dow": name,
            "n": n,
            "mean_bps": round(mean * 10000, 2),
            "std_bps": round(std * 10000, 1),
            "t_stat": round(t_stat, 3),
            "p_value": round(p_val, 5),
            "bonferroni_significant": p_val < alpha_bonferroni,
            "win_rate": round(wr, 3),
        })
    return rows


def _print_dow_rows(rows: list[dict], label: str) -> None:
    print(f"\n  {label}")
    print(f"  {'dow':<4} {'n':>5} {'mean_bps':>9} {'std_bps':>9} "
          f"{'t':>6} {'p':>7} {'WR':>6} {'sig?':>5}")
    print("  " + "-" * 60)
    for r in rows:
        sig = "YES" if r["bonferroni_significant"] else "no"
        print(f"  {r['dow']:<4} {r['n']:>5} {r['mean_bps']:>+8.2f}  "
              f"{r['std_bps']:>8.1f}  {r['t_stat']:>+5.2f} "
              f"{r['p_value']:>6.4f} {r['win_rate']*100:>4.1f}%  {sig}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=1995)
    parser.add_argument("--end-year", type=int, default=2025)
    parser.add_argument("--slippage-bps", type=float, default=2.0,
                          help="SPY daily-trade slippage (default 2bp; "
                               "tight because SPY is highly liquid)")
    args = parser.parse_args(argv)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[1] fetching SPY daily {args.start_year}-{args.end_year}...")
    daily = _fetch_spy_daily(args.start_year, args.end_year)
    print(f"  {len(daily)} daily bars  "
          f"({daily.index.min().date()} → {daily.index.max().date()})")

    print()
    print("[2] unconditional DOW returns (full window):")
    rows_full = _summarize_by_dow(daily, label="full")
    _print_dow_rows(rows_full, label="full window")

    print()
    print("[3] era split: pre-2000 vs post-2000")
    pre = daily[daily.index.year < 2000]
    post = daily[daily.index.year >= 2000]
    rows_pre = _summarize_by_dow(pre, label="pre-2000")
    rows_post = _summarize_by_dow(post, label="post-2000")
    _print_dow_rows(rows_pre, label="pre-2000")
    _print_dow_rows(rows_post, label="post-2000")

    print()
    print("[4] conditional weekend effect — Monday returns following negative Friday:")
    # Build a series: Mon return | prior Friday return
    daily_sorted = daily.sort_index()
    mons = daily_sorted[daily_sorted["dow"] == 0]
    fris = daily_sorted[daily_sorted["dow"] == 4]
    fri_map = {fri_date: fri_ret for fri_date, fri_ret in
                zip(fris.index, fris["ret"].values)}
    cond_rows = []
    for mon_date, mon_ret in zip(mons.index, mons["ret"].values):
        # Find the most recent Friday strictly before this Monday
        prior_friday = mon_date - pd.Timedelta(days=3)
        if prior_friday in fri_map:
            cond_rows.append({
                "mon": mon_date,
                "mon_ret": mon_ret,
                "prior_fri_ret": fri_map[prior_friday],
            })
    if cond_rows:
        cond_df = pd.DataFrame(cond_rows).set_index("mon")
        mon_after_neg_fri = cond_df[cond_df["prior_fri_ret"] < 0]["mon_ret"].values
        mon_after_pos_fri = cond_df[cond_df["prior_fri_ret"] > 0]["mon_ret"].values
        for label, vals in (("after negative Fri", mon_after_neg_fri),
                             ("after positive Fri", mon_after_pos_fri)):
            if len(vals) == 0:
                continue
            n = len(vals)
            mean = float(np.mean(vals))
            std = float(np.std(vals, ddof=1))
            t = mean / (std / math.sqrt(n)) if std > 0 else 0.0
            print(f"  Mon {label:<22} n={n:>4}  mean_bps={mean*10000:+7.2f}  "
                  f"t={t:+5.2f}  p={_normal_p_two_sided(t):.4f}")

    print()
    print("[5] Friday-effect strategy: buy SPY Thu close, sell Fri close")
    fri_trades = []
    for thu_date in daily_sorted[daily_sorted["dow"] == 3].index:
        next_fri = thu_date + pd.Timedelta(days=1)
        if next_fri in daily_sorted.index:
            entry = float(daily_sorted.loc[thu_date, "Close"])
            exit_ = float(daily_sorted.loc[next_fri, "Close"])
            if entry > 0:
                pnl = (exit_ / entry - 1.0) * 100.0
                pnl_after_slip = pnl - args.slippage_bps / 100.0
                fri_trades.append({
                    "thu": thu_date.strftime("%Y-%m-%d"),
                    "fri": next_fri.strftime("%Y-%m-%d"),
                    "pnl_pct": round(pnl, 4),
                    "pnl_after_slip_pct": round(pnl_after_slip, 4),
                })
    if fri_trades:
        pnls_slip = [t["pnl_after_slip_pct"] for t in fri_trades]
        wins = [p for p in pnls_slip if p > 0]
        losses = [p for p in pnls_slip if p < 0]
        pf = abs(sum(wins) / sum(losses)) if losses else float("inf")
        wr = len(wins) / len(pnls_slip)
        mean = sum(pnls_slip) / len(pnls_slip)
        from helio.bootstrap_stats import bootstrap_profit_factor
        bs = bootstrap_profit_factor(pnls_slip, n_resamples=3000, seed=42)
        print(f"  n_trades={len(pnls_slip)} PF={pf:.2f} WR={wr*100:.1f}% "
              f"avg={mean:+.4f}%  CI=[{bs.ci_lower:.2f}, {bs.ci_upper:.2f}]")

    # Persist
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = OUT_DIR / f"dow_research_{ts}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "year_range": [args.start_year, args.end_year],
        "slippage_bps": args.slippage_bps,
        "n_daily_bars": int(len(daily)),
        "dow_full_window": rows_full,
        "dow_pre_2000": rows_pre,
        "dow_post_2000": rows_post,
        "fri_strategy_summary": {
            "n_trades": len(fri_trades) if fri_trades else 0,
            "profit_factor": round(pf, 3) if fri_trades else None,
            "win_rate": round(wr, 3) if fri_trades else None,
            "avg_pct_after_slip": round(mean, 4) if fri_trades else None,
            "ci_lower": round(bs.ci_lower, 3) if fri_trades else None,
            "ci_upper": round(bs.ci_upper, 3) if fri_trades else None,
        },
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str),
                          encoding="utf-8")

    md_lines: list[str] = []
    md_lines.append("# SPY day-of-week research")
    md_lines.append("")
    md_lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    md_lines.append(f"Period: {args.start_year}-{args.end_year}, "
                       f"{len(daily)} daily bars")
    md_lines.append("")
    for label, rows in (("Full window", rows_full),
                          ("Pre-2000", rows_pre),
                          ("Post-2000", rows_post)):
        md_lines.append(f"## {label}")
        md_lines.append("")
        md_lines.append("| DOW | n | mean_bps | std_bps | t | p | WR | Bonf-sig? |")
        md_lines.append("|---|---|---|---|---|---|---|---|")
        for r in rows:
            sig = "YES" if r["bonferroni_significant"] else "no"
            md_lines.append(
                f"| {r['dow']} | {r['n']} | {r['mean_bps']:+.2f} | "
                f"{r['std_bps']:.1f} | {r['t_stat']:+.2f} | "
                f"{r['p_value']:.4f} | {r['win_rate']*100:.1f}% | {sig} |"
            )
        md_lines.append("")
    md_lines.append(f"Bonferroni-adjusted α at 5 tests: 0.01000")
    md_path = OUT_DIR / "dow_research.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    print()
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
