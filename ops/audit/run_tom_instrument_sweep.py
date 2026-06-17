"""TOM strategy sweep across instruments.

The live forge_tom_spy strategy buys at close of the 4th-to-last
trading day of each month and sells at close of the 3rd trading day
of the next month. Live universe is SPY only.

This sweep asks: does the same TOM logic produce edge on QQQ, IWM,
DIA, sectors, or international ETFs?

USAGE
-----
    python -m ops.audit.run_tom_instrument_sweep
    python -m ops.audit.run_tom_instrument_sweep --ticker QQQ --period 20y
    python -m ops.audit.run_tom_instrument_sweep --json

Survivor floor: PF CI lower >= 1.20 at 10bps slippage.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"

# Test universe — equity ETFs spanning size, sector, geography.
CANDIDATE_TICKERS = (
    "SPY",   # baseline (currently live PENDING_OPT_IN)
    "QQQ",   # tech-heavy
    "IWM",   # small-cap (5/19 found TOM-on-IWM DEAD; verify here at 20y)
    "DIA",   # large-cap industrials
    "EFA",   # international developed
    "EEM",   # emerging markets
    "GLD",   # gold (TOM via PM effect)
    "TLT",   # long bonds
    "XLK",   # tech sector
    "XLF",   # financials
)

SURVIVOR_PF_FLOOR = 1.20


def _backtest_tom(ticker: str, *, period: str = "20y",
                  entry_offset: int = 4, exit_offset: int = 3,
                  slippage_bps: float = 10.0) -> dict:
    """Simulate the TOM round-trip for one ticker over `period`."""
    try:
        import yfinance as yf
        from helio.bootstrap_stats import bootstrap_profit_factor
    except Exception as exc:
        return {"ticker": ticker, "error": f"import: {exc}"}

    try:
        df = yf.download(ticker, period=period, interval="1d",
                         progress=False, auto_adjust=False)
    except Exception as exc:
        return {"ticker": ticker, "error": f"yfinance: {exc}"}

    if df is None or df.empty:
        return {"ticker": ticker, "error": "empty"}
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if "Close" not in df.columns:
        return {"ticker": ticker, "error": f"no Close col: {list(df.columns)}"}
    closes = df["Close"].dropna()
    if len(closes) < 50:
        return {"ticker": ticker, "error": "insufficient history"}

    # Walk each month, identify entry + exit days, compute PnL.
    closes.index = pd.to_datetime(closes.index).tz_localize(None)
    by_month: dict[tuple, list[int]] = {}
    for i, idx in enumerate(closes.index):
        key = (idx.year, idx.month)
        by_month.setdefault(key, []).append(i)

    months = sorted(by_month.keys())
    trades: list[dict] = []
    for m in months:
        idxs = by_month[m]
        # Entry: close of 4th-to-last trading day of THIS month
        if len(idxs) <= entry_offset:
            continue
        entry_i = idxs[-(entry_offset + 1)]
        entry_px = float(closes.iloc[entry_i])

        # Exit: close of 3rd trading day of NEXT month
        # Find next month in our index
        y, mo = m
        if mo == 12:
            next_key = (y + 1, 1)
        else:
            next_key = (y, mo + 1)
        next_idxs = by_month.get(next_key)
        if not next_idxs or len(next_idxs) < exit_offset:
            continue
        exit_i = next_idxs[exit_offset - 1]
        exit_px = float(closes.iloc[exit_i])

        pnl_pct = (exit_px / entry_px - 1.0) * 100.0
        trades.append({
            "entry_date": str(closes.index[entry_i].date()),
            "exit_date": str(closes.index[exit_i].date()),
            "entry_px": entry_px,
            "exit_px": exit_px,
            "pnl_pct": pnl_pct,
        })

    if not trades:
        return {"ticker": ticker, "error": "no trades"}

    # Apply slippage drag
    drag = 2.0 * (slippage_bps / 100.0)
    pnls_after = [t["pnl_pct"] - drag for t in trades]
    wins = [p for p in pnls_after if p > 0]
    losses = [p for p in pnls_after if p < 0]
    wr = len(wins) / len(pnls_after)
    pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")

    try:
        boot = bootstrap_profit_factor(pnls_after, n_resamples=2000)
        ci_lo = boot.ci_lower
        ci_hi = boot.ci_upper
    except Exception as exc:
        return {"ticker": ticker, "error": f"bootstrap: {exc}"}

    # CAGR estimate
    cum = 1.0
    for p in pnls_after:
        cum *= (1.0 + p / 100.0)
    try:
        first = pd.to_datetime(trades[0]["entry_date"])
        last = pd.to_datetime(trades[-1]["exit_date"])
        days = max(1, (last - first).days)
        cagr = (cum ** (365.25 / days) - 1) * 100
    except Exception:
        cagr = 0.0

    # Max DD on the equity curve
    eq = 1.0
    peak = 1.0
    max_dd = 0.0
    for p in pnls_after:
        eq *= (1.0 + p / 100.0)
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)

    return {
        "ticker": ticker,
        "n_trades": len(trades),
        "win_rate": round(wr, 3),
        "pf_point": round(pf, 2),
        "ci_95_lower": round(ci_lo, 2),
        "ci_95_upper": round(ci_hi, 2),
        "avg_pnl_pct": round(sum(pnls_after) / len(pnls_after), 3),
        "cagr_pct": round(cagr, 1),
        "max_dd_pct": round(max_dd * 100, 1),
        "first_entry": trades[0]["entry_date"],
        "last_exit": trades[-1]["exit_date"],
        "slippage_bps_applied": slippage_bps,
        "survivor": ci_lo >= SURVIVOR_PF_FLOOR,
    }


def _render_markdown(rows: list[dict]) -> str:
    out = [
        "# TOM strategy — instrument sweep",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Slippage: 10 bps per round-trip. Survivor: CI lower >= {SURVIVOR_PF_FLOOR}",
        "",
    ]
    survivors = [r for r in rows if r.get("survivor")]
    out.append(f"**Survivors**: {len(survivors)} / {len(rows)}")
    out.append("")
    out.append("| Ticker | n | WR | PF | CI lower | Avg% | CAGR | DD | Survivor |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        if r.get("error"):
            out.append(f"| {r['ticker']} | — | — | — | — | — | — | — | ERR: {r['error'][:40]} |")
            continue
        out.append(
            f"| {r['ticker']} | {r['n_trades']} | "
            f"{r['win_rate']*100:.1f}% | {r['pf_point']:.2f} | "
            f"{r['ci_95_lower']:.2f} | {r['avg_pnl_pct']:+.2f}% | "
            f"{r['cagr_pct']:.1f}% | {r['max_dd_pct']:.1f}% | "
            f"{'YES' if r['survivor'] else 'no'} |"
        )
    if survivors:
        out.append("")
        out.append("## Survivors (deploy candidates)")
        for r in survivors:
            out.append(
                f"- **{r['ticker']}**: n={r['n_trades']} PF {r['pf_point']:.2f} "
                f"CI [{r['ci_95_lower']:.2f}, {r['ci_95_upper']:.2f}], "
                f"CAGR {r['cagr_pct']:.1f}%, DD {r['max_dd_pct']:.1f}%"
            )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default=None)
    parser.add_argument("--period", default="20y")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    tickers = (args.ticker,) if args.ticker else CANDIDATE_TICKERS
    rows = []
    for t in tickers:
        print(f"  {t}...", file=sys.stderr, end=" ", flush=True)
        r = _backtest_tom(t, period=args.period)
        rows.append(r)
        if r.get("error"):
            print(f"ERROR {r['error']}", file=sys.stderr)
        else:
            print(
                f"n={r['n_trades']} PF={r['pf_point']:.2f} "
                f"CI_lo={r['ci_95_lower']:.2f} "
                f"{'SURVIVOR' if r['survivor'] else ''}",
                file=sys.stderr,
            )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "tom_instrument_sweep.json").write_text(
        json.dumps(rows, indent=2, default=str), encoding="utf-8")

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
    else:
        print(_render_markdown(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
