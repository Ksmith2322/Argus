#!/usr/bin/env python3
# backtest/walk_forward.py
#
# Phase 12 — Walk-Forward Validation
#
# Splits the candle dataset into N non-overlapping windows (or anchored expanding
# windows) and runs backtest/runner.run_backtest() on each independently.
# Collects results into a single JSON summary for research_report.py.
#
# Usage:
#   python -m backtest.walk_forward                        # defaults
#   python -m backtest.walk_forward --windows 8 --candles-csv data/eth_usd_1m.csv
#   python -m backtest.walk_forward --mode expanding       # expanding window
#   python -m backtest.walk_forward --friction-mode constant --friction-report-latest
#
# Each window runs in isolation (fresh BotState) with the same cfg as the
# canonical backtest, subject to FRICTION_MODE / FRICTION_REPORT_PATH overrides.

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(str(v).strip())
    except Exception:
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(str(v).strip())
    except Exception:
        return default


def _read_candle_rows(path: str) -> List[Dict[str, str]]:
    with open(path, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_candle_tmp(rows: List[Dict[str, str]], fieldnames: List[str]) -> str:
    fd, tmp_path = tempfile.mkstemp(suffix=".csv", prefix="wf_window_")
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return tmp_path


def _result_to_dict(res: Any, window_idx: int, window_rows: int) -> Dict[str, Any]:
    """Extract key metrics from BacktestResults into a serialisable dict."""
    d: Dict[str, Any] = {
        "window": window_idx,
        "candles": window_rows,
    }

    # Use summary() — canonical field names from BacktestResults
    try:
        sd = res.summary() if hasattr(res, "summary") else {}
        for k, v in sd.items():
            if k not in d:
                try:
                    d[k] = float(v) if isinstance(v, Decimal) else v
                except Exception:
                    d[k] = str(v)
    except Exception:
        pass

    return d


# ─────────────────────────────────────────────────────────────────────────────
# Window splitting
# ─────────────────────────────────────────────────────────────────────────────

def split_windows(
    rows: List[Dict[str, str]],
    n_windows: int,
    mode: str = "rolling",  # rolling | expanding
) -> List[Tuple[int, List[Dict[str, str]]]]:
    """
    Split candle rows into windows.

    rolling:    non-overlapping equal-size chunks  [ [0..W), [W..2W), ... ]
    expanding:  anchor at 0, each window adds one chunk [ [0..W), [0..2W), ... ]

    Returns list of (window_idx, rows) tuples.
    """
    total = len(rows)
    chunk = total // n_windows
    if chunk < 1:
        raise ValueError(
            f"Too few candles ({total}) for {n_windows} windows. "
            "Reduce --windows or use more candle data."
        )

    windows: List[Tuple[int, List[Dict[str, str]]]] = []
    for i in range(n_windows):
        if mode == "expanding":
            start = 0
            end = min((i + 1) * chunk, total)
        else:  # rolling
            start = i * chunk
            end = start + chunk if i < n_windows - 1 else total
        windows.append((i, rows[start:end]))

    return windows


# ─────────────────────────────────────────────────────────────────────────────
# Run one window
# ─────────────────────────────────────────────────────────────────────────────

def _run_window(
    window_idx: int,
    rows: List[Dict[str, str]],
    fieldnames: List[str],
    cfg_override: Optional[Dict] = None,
) -> Dict[str, Any]:
    from backtest.runner import run_backtest

    tmp_path = _write_candle_tmp(rows, fieldnames)
    try:
        res = run_backtest(
            candles_csv=tmp_path,
            csv_format_hint="auto",
            limit=None,
            write_logs=False,
            cfg_override=cfg_override,
        )
        d = _result_to_dict(res, window_idx, len(rows))
        d["status"] = "ok"
    except Exception as exc:
        d = {"window": window_idx, "candles": len(rows), "status": "error", "error": str(exc)}
    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass

    return d


# ─────────────────────────────────────────────────────────────────────────────
# Walk-forward runner
# ─────────────────────────────────────────────────────────────────────────────

def run_walk_forward(
    candles_csv: str,
    n_windows: int = 8,
    mode: str = "rolling",
    cfg_override: Optional[Dict] = None,
    out_dir: Optional[str] = None,
    run_id: Optional[str] = None,
    friction_mode: str = "off",
    friction_report_path: Optional[str] = None,
    friction_report_latest: bool = False,
) -> Dict[str, Any]:
    """
    Run walk-forward validation and return a summary dict.
    Also writes wf_summary_<run_id>.json to out_dir.
    """
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = out_dir or str(Path(candles_csv).parent.parent / "ops" / "logs")
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    rows = _read_candle_rows(candles_csv)
    if not rows:
        raise ValueError(f"No candle rows in {candles_csv}")
    fieldnames = list(rows[0].keys())

    # Apply friction mode env overrides
    env_backup: Dict[str, Optional[str]] = {}

    def _set_env(k: str, v: str) -> None:
        env_backup[k] = os.environ.get(k)
        os.environ[k] = v

    if friction_mode not in ("", "off", "none"):
        _set_env("FRICTION_MODE", friction_mode)
        if friction_report_path:
            _set_env("FRICTION_REPORT_PATH", friction_report_path)
        elif friction_report_latest:
            _set_env("FRICTION_REPORT_LATEST", "1")

    try:
        windows = split_windows(rows, n_windows, mode=mode)
    except ValueError as exc:
        return {"error": str(exc), "run_id": run_id}

    print(f"Walk-forward: {n_windows} {mode} windows over {len(rows)} candles")
    results: List[Dict[str, Any]] = []
    for widx, wrows in windows:
        print(f"  Window {widx + 1}/{n_windows}: {len(wrows)} candles ...", end=" ", flush=True)
        d = _run_window(widx, wrows, fieldnames, cfg_override=cfg_override)
        results.append(d)
        status = d.get("status", "?")
        pnl = d.get("pnl_usd", d.get("total_pnl", d.get("pnl", "?")))
        trades = d.get("trades_closed", d.get("total_trades", "?"))
        print(f"status={status}  pnl={pnl}  trades={trades}")

    # Restore env
    for k, v in env_backup.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    # Aggregate
    ok_results = [r for r in results if r.get("status") == "ok"]
    agg = _aggregate_windows(ok_results)

    summary = {
        "version": "1.0",
        "generated_at": _utc_now(),
        "run_id": run_id,
        "candles_csv": candles_csv,
        "total_candles": len(rows),
        "n_windows": n_windows,
        "mode": mode,
        "friction_mode": friction_mode,
        "windows_ok": len(ok_results),
        "windows_error": len(results) - len(ok_results),
        "windows": results,
        "aggregate": agg,
    }

    out_path = Path(out_dir) / f"wf_summary_{run_id}.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\n  Summary -> {out_path}")

    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation
# ─────────────────────────────────────────────────────────────────────────────

def _aggregate_windows(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not results:
        return {}

    def _vals(key: str) -> List[float]:
        return [_safe_float(r[key]) for r in results if key in r]

    def _stats(vals: List[float]) -> Dict[str, Optional[float]]:
        if not vals:
            return {"mean": None, "min": None, "max": None, "positive": None}
        return {
            "mean": round(sum(vals) / len(vals), 6),
            "min": round(min(vals), 6),
            "max": round(max(vals), 6),
            "positive": sum(1 for v in vals if v > 0),
        }

    pnl_key = next((k for k in ("pnl_usd", "total_pnl", "net_pnl", "pnl") if any(k in r for r in results)), None)
    trades_key = next((k for k in ("trades_closed", "total_trades", "num_trades", "n_trades") if any(k in r for r in results)), None)

    agg: Dict[str, Any] = {}
    if pnl_key:
        pnl_vals = _vals(pnl_key)
        agg["pnl"] = _stats(pnl_vals)
        agg["pnl"]["windows_positive"] = sum(1 for v in pnl_vals if v > 0)
        agg["pnl"]["windows_total"] = len(pnl_vals)
    if trades_key:
        agg["trades"] = _stats(_vals(trades_key))
    for key in ("win_rate", "expectancy", "sharpe", "max_drawdown"):
        vals = _vals(key)
        if vals:
            agg[key] = _stats(vals)

    return agg


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Phase 12 walk-forward validation — runs backtest on N independent windows"
    )
    parser.add_argument("--candles-csv", default=None,
                        help="Candle CSV path (default: data/eth_usd_1m.csv)")
    parser.add_argument("--windows", type=int, default=8,
                        help="Number of windows (default: 8)")
    parser.add_argument("--mode", choices=["rolling", "expanding"], default="rolling",
                        help="Window mode: rolling (equal non-overlapping) or expanding (default: rolling)")
    parser.add_argument("--out-dir", default="ops/logs",
                        help="Output directory for wf_summary_*.json (default: ops/logs)")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--friction-mode", default="off",
                        choices=["off", "constant", "conditional", "monte_carlo"],
                        help="Friction injection mode (default: off)")
    parser.add_argument("--friction-report-path", default=None,
                        help="Explicit path to friction_report_*.json")
    parser.add_argument("--friction-report-latest", action="store_true",
                        help="Auto-load latest friction_report_*.json from ops/logs")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).parent.parent
    candles_csv = args.candles_csv or str(repo_root / "data" / "eth_usd_1m.csv")
    out_dir = str(Path(args.out_dir).resolve())

    summary = run_walk_forward(
        candles_csv=candles_csv,
        n_windows=args.windows,
        mode=args.mode,
        out_dir=out_dir,
        run_id=args.run_id,
        friction_mode=args.friction_mode,
        friction_report_path=args.friction_report_path,
        friction_report_latest=args.friction_report_latest,
    )

    agg = summary.get("aggregate", {})
    pnl = agg.get("pnl", {})
    print(f"\nAggregate ({summary.get('windows_ok')}/{summary.get('n_windows')} windows ok):")
    print(f"  PnL    mean={pnl.get('mean')}  min={pnl.get('min')}  max={pnl.get('max')}  "
          f"positive={pnl.get('windows_positive')}/{pnl.get('windows_total')}")
    wr = agg.get("win_rate", {})
    if wr:
        print(f"  WinRate mean={wr.get('mean')}  min={wr.get('min')}  max={wr.get('max')}")
    dd = agg.get("max_drawdown", {})
    if dd:
        print(f"  MaxDD   mean={dd.get('mean')}  min={dd.get('min')}  max={dd.get('max')}")


if __name__ == "__main__":
    main()
