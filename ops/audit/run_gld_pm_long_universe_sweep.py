"""Universe sweep for the gld_pm_long ATR-breakout signal.

Replays the gld_pm_long 1h-bar signal logic on a panel of candidate
instruments to answer the question:

  "Is the PM-session intraday continuation edge gold-specific,
   commodity-specific, or just a general intraday momentum pattern
   that works on liquid 1h-tradable instruments?"

The signal is parameter-identical to the live gld_pm_long runner
(PARAMS imported), only the ticker changes.

USAGE
-----
    # Full sweep, all commodity + a few sanity tickers
    python -m ops.audit.run_gld_pm_long_universe_sweep

    # One ticker only
    python -m ops.audit.run_gld_pm_long_universe_sweep --ticker GDX

    # JSON to stdout
    python -m ops.audit.run_gld_pm_long_universe_sweep --json

Reports per ticker:
    n_trades, win_rate, profit_factor, CI lower @ 10bp slippage,
    avg_pnl_atr, max consecutive losers, SURVIVOR? (CI lower >= 1.05
    — DEFENSE role bar, not the OFFENSE 1.20)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


# ─── Candidate universe ──────────────────────────────────────────────

# Commodity-family ETFs — the natural extension of GLD.
COMMODITY_CANDIDATES = (
    "GLD",   # baseline
    "SLV",   # silver
    "GDX",   # gold miners (higher beta to gold price)
    "USO",   # crude oil
    "UNG",   # natural gas
    "DBC",   # broad commodity
    "DBA",   # agriculture
    "IAU",   # gold alternative (1/10 the price of GLD)
)

# Non-commodity sanity tickers. If the signal works HERE too, the edge
# isn't "PM gold strength" — it's "PM intraday continuation on any
# liquid 1h instrument." Important to know either way.
SANITY_CANDIDATES = (
    "SPY",   # broad equity
    "QQQ",   # tech equity
    "TLT",   # long bonds
    "XLE",   # energy sector
    "XLB",   # materials sector
)

DEFAULT_TICKERS = COMMODITY_CANDIDATES + SANITY_CANDIDATES


# DEFENSE-role survivor floor (gld_pm_long is DEFENSE).
SURVIVOR_PF_FLOOR = 1.05


def _run_one_ticker(ticker: str, *, period: str = "2y",
                    slippage_bps: float = 10.0) -> dict:
    """Replay the gld_pm_long signal on `ticker` and score against
    the disciplined gate."""
    try:
        import yfinance as yf
        import pandas as pd
        import numpy as np
        from forge.gld_pm_long.runner import PARAMS, atr
        from helio.bootstrap_stats import bootstrap_profit_factor
    except Exception as exc:
        return {"ticker": ticker, "error": f"import failed: {exc}"}

    try:
        df = yf.download(ticker, period=period, interval="1h",
                         progress=False, auto_adjust=False)
    except Exception as exc:
        return {"ticker": ticker, "error": f"yfinance failed: {exc}"}
    if df is None or df.empty:
        return {"ticker": ticker, "error": "yfinance empty"}
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)
    needed = ["Open", "High", "Low", "Close", "Volume"]
    if not all(c in df.columns for c in needed):
        return {"ticker": ticker, "error": f"missing OHLCV cols: {df.columns.tolist()}"}
    df = df[needed].dropna()
    df.index = pd.to_datetime(df.index, utc=True)

    a_arr = atr(df, PARAMS["atr_period"]).values
    H, L, C = df["High"].values, df["Low"].values, df["Close"].values

    trades: list[dict] = []
    open_until = -1
    for i in range(len(df)):
        if i <= open_until:
            continue
        if df.index[i].hour not in PARAMS["signal_hours_utc"]:
            continue
        a = a_arr[i]
        if not np.isfinite(a) or a <= 0:
            continue
        entry = C[i]
        target = entry + PARAMS["target_atr"] * a
        stop = entry - PARAMS["stop_atr"] * a
        exit_px = None; exit_idx = None
        for j in range(i + 1, min(i + 1 + PARAMS["hold_bars"], len(df))):
            if L[j] <= stop:
                exit_px = stop; exit_idx = j; break
            if H[j] >= target:
                exit_px = target; exit_idx = j; break
        if exit_px is None:
            exit_idx = min(i + PARAMS["hold_bars"], len(df) - 1)
            exit_px = C[exit_idx]
        # pnl as % of entry price (so cross-ticker comparable)
        pnl_pct = (exit_px - entry) / entry * 100.0
        trades.append({
            "entry_ts": str(df.index[i]),
            "exit_ts": str(df.index[exit_idx]),
            "entry": float(entry),
            "exit": float(exit_px),
            "pnl_pct": pnl_pct,
        })
        open_until = exit_idx

    if not trades:
        return {"ticker": ticker, "n_trades": 0, "error": "no trades"}

    # Apply 10bp round-trip slippage drag
    slip_drag_pct = 2.0 * (slippage_bps / 100.0)
    pnls_after = [t["pnl_pct"] - slip_drag_pct for t in trades]
    wins = [p for p in pnls_after if p > 0]
    losses = [p for p in pnls_after if p < 0]
    wr = len(wins) / len(pnls_after)
    pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")

    # Max consecutive losers
    max_consec = 0
    cur = 0
    for p in pnls_after:
        if p <= 0:
            cur += 1
            max_consec = max(max_consec, cur)
        else:
            cur = 0

    try:
        boot = bootstrap_profit_factor(pnls_after, n_resamples=2000)
        ci_lower = boot.ci_lower
        ci_upper = boot.ci_upper
    except Exception as exc:
        return {"ticker": ticker, "error": f"bootstrap failed: {exc}"}

    survivor = ci_lower >= SURVIVOR_PF_FLOOR

    return {
        "ticker": ticker,
        "n_trades": len(trades),
        "win_rate": round(wr, 3),
        "pf_point": round(pf, 2),
        "ci_95_lower": round(ci_lower, 2),
        "ci_95_upper": round(ci_upper, 2),
        "avg_pnl_pct": round(sum(pnls_after) / len(pnls_after), 3),
        "max_consec_losses": max_consec,
        "first_entry": trades[0]["entry_ts"],
        "last_exit": trades[-1]["exit_ts"],
        "slippage_bps_applied": slippage_bps,
        "survivor_defense_floor": survivor,
    }


def _render_markdown(rows: list[dict]) -> str:
    out = [
        "# GLD PM Long signal — universe sweep",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"Slippage applied: 10 bps per round-trip",
        f"Survivor floor: CI lower >= {SURVIVOR_PF_FLOOR}  (DEFENSE role)",
        "",
    ]
    survivors = [r for r in rows if r.get("survivor_defense_floor")]
    errors = [r for r in rows if r.get("error")]
    out.append(
        f"**Survivors**: {len(survivors)} / {len(rows)}  "
        f"({len(errors)} errored)"
    )
    out.append("")
    out.append("| Ticker | n | WR | PF | CI lower | Avg PnL% | Max consec L | Survivor |")
    out.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        if r.get("error"):
            out.append(f"| {r['ticker']} | — | — | — | — | — | — | "
                       f"ERROR: {r['error'][:40]} |")
            continue
        out.append(
            f"| {r['ticker']} | "
            f"{r['n_trades']} | "
            f"{r['win_rate']*100:.1f}% | "
            f"{r['pf_point']:.2f} | "
            f"{r['ci_95_lower']:.2f} | "
            f"{r['avg_pnl_pct']:+.3f}% | "
            f"{r['max_consec_losses']} | "
            f"{'YES' if r['survivor_defense_floor'] else 'no'} |"
        )
    out.append("")
    if survivors:
        out.append("## Survivors (DEFENSE-floor PF >= 1.05)")
        out.append("")
        for r in survivors:
            out.append(
                f"- **{r['ticker']}**: n={r['n_trades']} PF {r['pf_point']:.2f} "
                f"CI [{r['ci_95_lower']:.2f}, {r['ci_95_upper']:.2f}], "
                f"avg {r['avg_pnl_pct']:+.3f}%, max consec L {r['max_consec_losses']}"
            )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default=None,
                        help="Run only one ticker (default: all)")
    parser.add_argument("--period", default="2y",
                        help="History to fetch (default: 2y — yfinance 1h cap is 730d)")
    parser.add_argument("--slippage-bps", type=float, default=10.0,
                        help="Round-trip slippage in bps (default: 10)")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON to stdout instead of markdown")
    args = parser.parse_args(argv)

    tickers = (args.ticker,) if args.ticker else DEFAULT_TICKERS

    rows: list[dict] = []
    for t in tickers:
        print(f"  running {t}...", file=sys.stderr)
        row = _run_one_ticker(t, period=args.period,
                              slippage_bps=args.slippage_bps)
        rows.append(row)
        if "error" in row:
            print(f"    ERROR: {row['error']}", file=sys.stderr)
        else:
            print(f"    n={row['n_trades']} PF={row['pf_point']:.2f} "
                  f"CI_lo={row['ci_95_lower']:.2f} "
                  f"{'SURVIVOR' if row['survivor_defense_floor'] else ''}",
                  file=sys.stderr)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUT_DIR / "gld_pm_long_universe_sweep.json"
    json_path.write_text(json.dumps(rows, indent=2, default=str),
                         encoding="utf-8")

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
    else:
        print(_render_markdown(rows))
        print(f"\nPersisted: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
