"""CLI: screen many specs across many tickers, emit a ranked JSON+CSV.

Usage:
    python -m helio.backtest_factory.cli screen \\
        --specs helio/backtest_factory/configs/factory_v1.json \\
        --universe SPY DIA EEM ES_F EURUSD \\
        --data-dir helio/data \\
        --suffix _daily \\
        --output argus_flow/data/backtest_results/factory_v1_<date>.json \\
        --walk-forward 4

The screen-result JSON has the shape:
    {
        "generated_at": "2026-05-22T...Z",
        "specs_count": N,
        "universe": [...],
        "results": [
            {
                "spec_name": ...,
                "ticker": ...,
                "metrics": {...},
                "wf": {...}    # only if --walk-forward > 0
            },
            ...
        ],
        "ranking": [...]   # sorted by `rank_metric` desc
    }
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from helio.backtest_factory import VERSION
from helio.backtest_factory.engine import run_spec, BacktestResult
from helio.backtest_factory.walk_forward import walk_forward
from helio import massive_data
from helio import yfinance_data


def _load_specs(specs_path: Path) -> List[Dict[str, Any]]:
    text = specs_path.read_text(encoding="utf-8")
    data = json.loads(text)
    if isinstance(data, dict) and "specs" in data:
        return list(data["specs"])
    if isinstance(data, list):
        return data
    raise ValueError(f"specs file must be a list or have a 'specs' key, got: {type(data).__name__}")


def _load_bars(ticker: str, data_dir: Path, suffix: str) -> Optional[pd.DataFrame]:
    """Load a single ticker's bars from CSV. Returns None on miss."""
    path = data_dir / f"{ticker}{suffix}.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path, parse_dates=["Date"])
    df = df.set_index("Date").sort_index()
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=[c for c in ("Open", "High", "Low", "Close") if c in df.columns])
    return df


def _rank_value(metrics: Dict[str, Any], rank_metric: str) -> float:
    v = metrics.get(rank_metric)
    if v is None:
        return float("-inf")
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("-inf")


def cmd_screen(args: argparse.Namespace) -> int:
    specs_path = Path(args.specs)
    data_dir = Path(args.data_dir)
    if not specs_path.exists():
        print(f"FAIL: specs file not found: {specs_path}", file=sys.stderr)
        return 2
    if not data_dir.exists():
        print(f"FAIL: data dir not found: {data_dir}", file=sys.stderr)
        return 2

    specs = _load_specs(specs_path)
    universe: List[str] = list(args.universe)
    if not universe:
        print("FAIL: --universe is empty", file=sys.stderr)
        return 2

    t0 = time.perf_counter()
    results: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for ticker in universe:
        df = _load_bars(ticker, data_dir, args.suffix)
        if df is None or len(df) < 60:
            skipped.append({"ticker": ticker, "reason": "missing_or_too_short",
                            "n_rows": 0 if df is None else len(df)})
            continue
        for spec in specs:
            try:
                br: BacktestResult = run_spec(spec, df, ticker=ticker)
            except Exception as e:
                skipped.append({"spec_name": spec.get("name", "?"), "ticker": ticker,
                                "reason": "engine_error", "error": str(e)})
                continue
            row: Dict[str, Any] = {
                "spec_name": br.spec_name,
                "ticker": ticker,
                "bars_used": br.bars_used,
                "elapsed_ms": round(br.elapsed_ms, 2),
                "metrics": br.metrics,
            }
            if args.walk_forward > 0:
                try:
                    folds, wf_agg = walk_forward(spec, df, n_folds=int(args.walk_forward))
                    row["wf"] = wf_agg
                    if args.include_folds:
                        row["wf_folds"] = [f.to_dict() for f in folds]
                except Exception as e:
                    row["wf"] = {"error": str(e)}
            results.append(row)

    # Ranking — by user-specified metric, then PF, then n
    rank_metric = args.rank_metric
    ranked = sorted(
        results,
        key=lambda r: (
            _rank_value(r["metrics"], rank_metric),
            _rank_value(r["metrics"], "pf"),
            _rank_value(r["metrics"], "n"),
        ),
        reverse=True,
    )
    ranking_summary = [
        {
            "spec_name": r["spec_name"], "ticker": r["ticker"],
            "n": r["metrics"].get("n"),
            "pf": r["metrics"].get("pf"),
            "wr": r["metrics"].get("wr"),
            "cagr_pct": r["metrics"].get("cagr_pct"),
            "max_dd_pct": r["metrics"].get("max_dd_pct"),
            "sharpe": r["metrics"].get("sharpe"),
            "wf_pass": (r.get("wf", {}).get("passes_promotion_floor") if "wf" in r else None),
        }
        for r in ranked
    ]

    elapsed_s = time.perf_counter() - t0
    output = {
        "factory_version": VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "specs_path": str(specs_path),
        "specs_count": len(specs),
        "universe": universe,
        "data_dir": str(data_dir),
        "suffix": args.suffix,
        "walk_forward_folds": int(args.walk_forward),
        "rank_metric": rank_metric,
        "n_results": len(results),
        "n_skipped": len(skipped),
        "skipped": skipped,
        "elapsed_s": round(elapsed_s, 3),
        "ranking": ranking_summary,
        "results": ranked,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")

    # Operator-facing summary on stdout
    print(f"=== backtest_factory screen complete ===")
    print(f"specs={len(specs)} universe={len(universe)} results={len(results)} "
          f"skipped={len(skipped)} elapsed={elapsed_s:.2f}s")
    print(f"output: {output_path}")
    print()
    print(f"Top 10 by {rank_metric}:")
    for row in ranking_summary[:10]:
        wf_tag = ""
        if row["wf_pass"] is True:
            wf_tag = " [WF PASS]"
        elif row["wf_pass"] is False:
            wf_tag = " [wf fail]"
        print(f"  {row['spec_name']:<28} {row['ticker']:<8} "
              f"n={row['n'] or 0:>4} pf={row['pf']} wr={row['wr']} "
              f"cagr={row['cagr_pct']}% dd={row['max_dd_pct']}% "
              f"sharpe={row['sharpe']}{wf_tag}")

    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    """Pull daily bars for a universe of tickers from Massive.com into
    helio/data_massive/. Incremental on subsequent runs (only fetches
    the gap since the cached last-bar date)."""
    try:
        client = massive_data.MassiveClient.from_env()
    except massive_data.MassiveError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 2

    tickers = list(args.tickers)
    if not tickers:
        print("FAIL: --tickers is empty", file=sys.stderr)
        return 2

    print(f"=== massive fetch: {len(tickers)} tickers, years_back={args.years_back} ===")
    print(f"   rate limit: ~5 req/min => est wall time ~{len(tickers) * 13}s")
    print()

    def _progress(idx: int, total: int, r: massive_data.FetchResult) -> None:
        status = "OK" if r.cache_path else "FAIL"
        kind = "incr" if r.incremental else "full"
        print(f"  [{idx:>3}/{total}] {r.ticker:<8} {status:<5} {kind:<5} "
              f"+{r.rows_fetched:>4} rows  total={r.rows_total:>4} "
              f"({r.elapsed_s:.1f}s)")

    try:
        results = massive_data.fetch_universe(
            tickers,
            client=client,
            years_back=args.years_back,
            refresh=args.refresh,
            on_progress=_progress,
        )
    except massive_data.MassiveError as e:
        print(f"FAIL (plan/auth, aborted): {e}", file=sys.stderr)
        return 2

    ok = sum(1 for r in results if r.cache_path)
    bad = len(results) - ok
    print()
    print(f"complete: ok={ok} fail={bad} total_rows={sum(r.rows_total for r in results)}")
    print(f"cache root: {massive_data.DATA_MASSIVE_DIR}")
    return 0 if bad == 0 else 1


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Re-run each (spec, ticker) and apply bootstrap PF CI on the resulting
    trade pnls. Per project_2026_05_20_pead_generalization_bootstrap_sunset.md:
    a real paper candidate must have 95% CI lower bound >= 1.20 PF."""
    from helio.bootstrap_stats import bootstrap_profit_factor

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        print(f"FAIL: data dir not found: {data_dir}", file=sys.stderr)
        return 2

    if args.specs_inline:
        specs = json.loads(args.specs_inline)
        if isinstance(specs, dict):
            specs = [specs]
        specs_label = f"<inline:{len(specs)} specs>"
    else:
        specs_path = Path(args.specs)
        if not specs_path.exists():
            print(f"FAIL: specs file not found: {specs_path}", file=sys.stderr)
            return 2
        specs = _load_specs(specs_path)
        specs_label = str(specs_path)
    universe = list(args.universe)
    if not universe:
        print("FAIL: --universe is empty", file=sys.stderr)
        return 2

    t0 = time.perf_counter()
    rows: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for ticker in universe:
        df = _load_bars(ticker, data_dir, args.suffix)
        if df is None or len(df) < 60:
            skipped.append({"ticker": ticker, "reason": "missing_or_too_short"})
            continue
        for spec in specs:
            try:
                br: BacktestResult = run_spec(
                    spec, df, ticker=ticker, slippage_bps=args.slippage_bps,
                )
            except Exception as e:
                skipped.append({"spec": spec.get("name", "?"), "ticker": ticker,
                                "reason": "engine_error", "error": str(e)})
                continue
            pnls = [float(t["pnl_pct"]) for t in br.trades]
            n = len(pnls)
            if n < args.min_n:
                rows.append({
                    "spec_name": br.spec_name, "ticker": ticker,
                    "n": n, "verdict": "INSUFFICIENT_N",
                    "pf_point": None, "pf_ci_lower": None, "pf_ci_upper": None,
                })
                continue
            br_pf = bootstrap_profit_factor(
                pnls, n_resamples=args.n_resamples, confidence=args.confidence,
                seed=args.seed,
            )
            passes_floor = br_pf.ci_lower >= args.promotion_floor
            verdict = "PASS_PROMOTION_FLOOR" if passes_floor else "BELOW_FLOOR"
            rows.append({
                "spec_name": br.spec_name, "ticker": ticker,
                "n": n,
                "pf_point": round(br_pf.point, 4),
                "pf_ci_lower": round(br_pf.ci_lower, 4),
                "pf_ci_upper": round(br_pf.ci_upper, 4),
                "pf_ci_mean": round(br_pf.mean, 4),
                "n_resamples": br_pf.n_resamples,
                "confidence": br_pf.confidence,
                "promotion_floor": args.promotion_floor,
                "verdict": verdict,
                "wr": br.metrics.get("wr"),
                "cagr_pct": br.metrics.get("cagr_pct"),
                "max_dd_pct": br.metrics.get("max_dd_pct"),
            })

    rows.sort(key=lambda r: (
        0 if r["verdict"] == "PASS_PROMOTION_FLOOR" else 1,
        -(r.get("pf_ci_lower") or -1),
    ))

    elapsed_s = time.perf_counter() - t0
    output = {
        "factory_version": VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "specs_path": specs_label,
        "specs_count": len(specs),
        "universe": universe,
        "data_dir": str(data_dir),
        "n_resamples": args.n_resamples,
        "confidence": args.confidence,
        "promotion_floor": args.promotion_floor,
        "min_n": args.min_n,
        "slippage_bps": args.slippage_bps,
        "elapsed_s": round(elapsed_s, 3),
        "skipped": skipped,
        "results": rows,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")

    print(f"=== bootstrap PF screen ===")
    print(f"specs={len(specs)} universe={len(universe)} cells={len(rows)} "
          f"elapsed={elapsed_s:.2f}s")
    print(f"output: {output_path}")
    print()
    print(f"floor={args.promotion_floor} confidence={args.confidence} "
          f"resamples={args.n_resamples} min_n={args.min_n}")
    print()
    passes = [r for r in rows if r["verdict"] == "PASS_PROMOTION_FLOOR"]
    print(f"PROMOTION-FLOOR PASS: {len(passes)} of {len(rows)} cells\n")
    if passes:
        print(f"  {'spec':<28} {'ticker':<7} {'n':>4} {'pf':>6} {'CI lo':>6} {'CI hi':>6} {'WR':>5} {'CAGR':>6}")
        for r in passes:
            print(f"  {r['spec_name']:<28} {r['ticker']:<7} {r['n']:>4} "
                  f"{r['pf_point']:>6.2f} {r['pf_ci_lower']:>6.2f} {r['pf_ci_upper']:>6.2f} "
                  f"{r['wr']:>5.2f} {r['cagr_pct']:>5.1f}%")

    return 0


def cmd_fetch_yf(args: argparse.Namespace) -> int:
    """Pull daily bars from yfinance (free, unlimited daily history). Output
    layout matches the Massive cache so the factory CLI's --data-dir flag
    can point at either source."""
    tickers = list(args.tickers)
    if not tickers:
        print("FAIL: --tickers is empty", file=sys.stderr)
        return 2

    print(f"=== yfinance fetch: {len(tickers)} tickers, years_back={args.years_back} ===")
    print()

    def _progress(idx: int, total: int, r: yfinance_data.YFFetchResult) -> None:
        status = "OK" if r.cache_path else "FAIL"
        kind = "incr" if r.incremental else "full"
        print(f"  [{idx:>3}/{total}] {r.ticker:<10} {status:<5} {kind:<5} "
              f"+{r.rows_fetched:>5} rows  total={r.rows_total:>5} "
              f"({r.elapsed_s:.1f}s)")

    results = yfinance_data.fetch_universe(
        tickers,
        years_back=args.years_back,
        refresh=args.refresh,
        between_ticker_sleep_s=args.sleep_s,
        on_progress=_progress,
    )

    ok = sum(1 for r in results if r.cache_path)
    bad = len(results) - ok
    print()
    print(f"complete: ok={ok} fail={bad} total_rows={sum(r.rows_total for r in results)}")
    print(f"cache root: {yfinance_data.DATA_YFINANCE_DIR}")
    return 0 if bad == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="backtest_factory")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("screen", help="Screen specs across tickers")
    s.add_argument("--specs", required=True, help="Path to spec JSON")
    s.add_argument("--universe", nargs="+", required=True, help="Ticker list")
    s.add_argument("--data-dir", default="helio/data", help="Bar CSV directory")
    s.add_argument("--suffix", default="_daily", help="CSV filename suffix (e.g. _daily, _1h)")
    s.add_argument("--output", required=True, help="Output JSON path")
    s.add_argument("--walk-forward", type=int, default=0, help="WF fold count (0=skip)")
    s.add_argument("--include-folds", action="store_true", help="Include per-fold detail in WF")
    s.add_argument("--rank-metric", default="sharpe",
                   help="Metric used to sort the ranking (default: sharpe)")
    s.set_defaults(func=cmd_screen)

    f = sub.add_parser("fetch", help="Pull daily bars from Massive.com to local cache")
    f.add_argument("--tickers", nargs="+", required=True, help="Ticker list")
    f.add_argument("--years-back", type=float, default=2.0,
                   help="Initial pull window (free tier caps at ~2y)")
    f.add_argument("--refresh", action="store_true",
                   help="Bypass existing cache and re-fetch full window")
    f.set_defaults(func=cmd_fetch)

    y = sub.add_parser("fetch-yf", help="Pull daily bars from yfinance (free, deep history)")
    y.add_argument("--tickers", nargs="+", required=True, help="Ticker list")
    y.add_argument("--years-back", type=float, default=10.0,
                   help="Initial pull window (yfinance: effectively unlimited daily)")
    y.add_argument("--refresh", action="store_true",
                   help="Bypass existing cache and re-fetch full window")
    y.add_argument("--sleep-s", type=float, default=1.0,
                   help="Polite sleep between tickers (default 1.0s)")
    y.set_defaults(func=cmd_fetch_yf)

    b = sub.add_parser("bootstrap",
                       help="Bootstrap PF confidence intervals per (spec, ticker)")
    b.add_argument("--specs", help="Path to spec JSON (or use --specs-inline)")
    b.add_argument("--specs-inline", help="JSON spec(s) as a string (alternative to --specs)")
    b.add_argument("--universe", nargs="+", required=True, help="Ticker list")
    b.add_argument("--data-dir", default="helio/data_yfinance", help="Bar CSV directory")
    b.add_argument("--suffix", default="_daily", help="CSV filename suffix")
    b.add_argument("--output", required=True, help="Output JSON path")
    b.add_argument("--n-resamples", type=int, default=5000,
                   help="Bootstrap resample count (default 5000)")
    b.add_argument("--confidence", type=float, default=0.95)
    b.add_argument("--promotion-floor", type=float, default=1.20,
                   help="PF CI lower-bound threshold for PASS_PROMOTION_FLOOR")
    b.add_argument("--min-n", type=int, default=30,
                   help="Min trade count required to even compute a CI (below = INSUFFICIENT_N)")
    b.add_argument("--slippage-bps", type=float, default=0.0,
                   help="Per-trade round-trip cost in bps subtracted from pnl_pct")
    b.add_argument("--seed", type=int, default=42)
    b.set_defaults(func=cmd_bootstrap)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
