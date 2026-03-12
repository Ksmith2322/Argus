#!/usr/bin/env python3
# analytics/friction_report.py
#
# Phase 11 — Empirical Friction Measurement
#
# Reads:
#   fills.csv           — canonical fill truth (ts, side, price, order_id, fee)
#   order_events.csv    — order lifecycle events (ORDER_ACCEPTED / ORDER_FILLED ts per order_id)
#   trade_journal_*.csv — lifecycle records with entry/exit mid_px, slippage_bps, regime, duration
#
# Writes:
#   friction_report_<run>.json  — machine-readable, consumed by friction_injector.py
#   friction_summary_<run>.html — human-readable single-file summary table
#
# Usage:
#   python -m analytics.friction_report
#   python -m analytics.friction_report --log-dir ops/logs --out-dir ops/logs --run-id myrun
#
# NOTE: Distributions are not meaningful below 30 fills.  The report flags
#       data_quality.sufficient_data = false and continues to emit results.

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

MINIMUM_FILLS = 30          # Phase 11 entry criterion
REPORT_VERSION = "1.0"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_decimal(v: Any, default: str = "0") -> Decimal:
    if v is None or str(v).strip() == "":
        return Decimal(default)
    try:
        return Decimal(str(v).strip())
    except InvalidOperation:
        return Decimal(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(_safe_decimal(v, str(default)))
    except Exception:
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(str(v).strip())
    except Exception:
        return default


def _parse_ts_ms(v: Any) -> Optional[int]:
    """Return timestamp in milliseconds from raw CSV field (may be ms or s)."""
    if v is None or str(v).strip() == "":
        return None
    try:
        t = int(str(v).strip())
        if t < 1_000_000_000_000:
            t *= 1000   # convert seconds → ms
        return t
    except Exception:
        return None


def _parse_iso(v: Any) -> Optional[datetime]:
    if not v or str(v).strip() == "":
        return None
    s = str(v).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _session_from_hour(hour_utc: int) -> str:
    """Classify UTC hour into a named session."""
    if 0 <= hour_utc < 7:
        return "ASIA"
    if 7 <= hour_utc < 12:
        return "LONDON"
    if 12 <= hour_utc < 15:
        return "OVERLAP"
    if 15 <= hour_utc < 20:
        return "NY"
    return "OFF_HOURS"


def _percentiles(data: List[float]) -> Dict[str, Optional[float]]:
    if not data:
        return {"p50": None, "p95": None, "p99": None,
                "min": None, "max": None, "mean": None, "count": 0}
    s = sorted(data)
    n = len(s)

    def pct(p: float) -> float:
        idx = (p / 100.0) * (n - 1)
        lo, frac = int(idx), idx - int(idx)
        if lo + 1 >= n:
            return s[lo]
        return s[lo] + frac * (s[lo + 1] - s[lo])

    return {
        "p50": round(pct(50), 4),
        "p95": round(pct(95), 4),
        "p99": round(pct(99), 4),
        "min": round(s[0], 4),
        "max": round(s[-1], 4),
        "mean": round(sum(s) / n, 4),
        "count": n,
    }


def _bucket_percentiles(groups: Dict[str, List[float]]) -> Dict[str, Dict]:
    return {k: _percentiles(v) for k, v in groups.items() if v}


# ─────────────────────────────────────────────────────────────────────────────
# Readers
# ─────────────────────────────────────────────────────────────────────────────

def _read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _read_fills(log_dir: Path) -> List[Dict[str, str]]:
    return _read_csv(log_dir / "fills.csv")


def _read_order_events(log_dir: Path) -> List[Dict[str, str]]:
    return _read_csv(log_dir / "order_events.csv")


def _read_all_trade_journals(log_dir: Path) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for p in sorted(log_dir.glob("trade_journal_*.csv")):
        rows.extend(_read_csv(p))
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Latency computation
# ─────────────────────────────────────────────────────────────────────────────

def _compute_latency_ms(
    order_events: List[Dict[str, str]],
) -> Dict[str, List[float]]:
    """
    For each order_id, compute latency_ms = FILLED_ts − ACCEPTED_ts.
    Returns {"entry": [...], "exit": [...], "all": [...]}.
    """
    # Build per-order_id timeline: {order_id: {event_type: ts_ms}}
    timeline: Dict[str, Dict[str, int]] = defaultdict(dict)
    order_side: Dict[str, str] = {}

    for row in order_events:
        oid = row.get("order_id", "").strip()
        etype = row.get("event_type", "").strip()
        side = row.get("side", "").upper().strip()
        ts = _parse_ts_ms(row.get("ts"))
        if not oid or ts is None:
            continue
        timeline[oid][etype] = ts
        if side:
            order_side[oid] = side

    entry_lat: List[float] = []
    exit_lat: List[float] = []

    for oid, events in timeline.items():
        accepted = events.get("ORDER_ACCEPTED")
        filled = events.get("ORDER_FILLED")
        if accepted is None or filled is None:
            continue
        lat = float(filled - accepted)
        if lat < 0:
            continue  # malformed
        side = order_side.get(oid, "")
        if side == "BUY":
            entry_lat.append(lat)
        elif side == "SELL":
            exit_lat.append(lat)
        else:
            # unknown side — put in both totals but not split
            entry_lat.append(lat)

    return {
        "entry": entry_lat,
        "exit": exit_lat,
        "all": entry_lat + exit_lat,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Slippage computation
# ─────────────────────────────────────────────────────────────────────────────

def _compute_slippage(
    trade_rows: List[Dict[str, str]],
) -> Tuple[
    Dict[str, List[float]],   # slippage_bps buckets by side
    Dict[str, List[float]],   # slippage by regime
    Dict[str, List[float]],   # slippage by session
    List[float],               # all entry slippage
    List[float],               # all exit slippage
]:
    """
    Compute actual slippage in bps from trade_journal:
      entry slippage = (entry_px - entry_mid_px) / entry_mid_px * 10000
      exit slippage  = (exit_mid_px - exit_px)   / exit_mid_px * 10000
    Positive = adverse (paid more to buy / received less to sell).
    """
    entry_slip: List[float] = []
    exit_slip: List[float] = []
    by_regime: Dict[str, List[float]] = defaultdict(list)
    by_session: Dict[str, List[float]] = defaultdict(list)

    for row in trade_rows:
        entry_px = _safe_float(row.get("entry_px"))
        exit_px = _safe_float(row.get("exit_px"))
        entry_mid = _safe_float(row.get("entry_mid_px"))
        exit_mid = _safe_float(row.get("exit_mid_px"))
        regime = row.get("regime_at_entry", "").strip() or "UNKNOWN"
        entry_dt = _parse_iso(row.get("entry_time"))

        session = "UNKNOWN"
        if entry_dt:
            session = _session_from_hour(entry_dt.hour)

        if entry_px > 0 and entry_mid > 0:
            slip_e = (entry_px - entry_mid) / entry_mid * 10000.0
            entry_slip.append(slip_e)
            by_regime[regime].append(slip_e)
            by_session[session].append(slip_e)

        if exit_px > 0 and exit_mid > 0:
            slip_x = (exit_mid - exit_px) / exit_mid * 10000.0
            exit_slip.append(slip_x)

    return {}, by_regime, by_session, entry_slip, exit_slip


# ─────────────────────────────────────────────────────────────────────────────
# Fee drag computation
# ─────────────────────────────────────────────────────────────────────────────

def _compute_fee_drag(
    trade_rows: List[Dict[str, str]],
    fills: List[Dict[str, str]],
) -> Dict[str, Any]:
    total_fees = sum(_safe_float(r.get("total_fee")) for r in trade_rows)
    total_pnl = sum(_safe_float(r.get("pnl")) for r in trade_rows)
    n = len(trade_rows)

    fill_fees = sum(_safe_float(r.get("fee")) for r in fills)

    fee_pct_gross = None
    if total_pnl != 0:
        fee_pct_gross = round(abs(total_fees / total_pnl) * 100, 4)

    return {
        "total_fees_usd_from_journal": round(total_fees, 8),
        "total_fees_usd_from_fills": round(fill_fees, 8),
        "avg_fee_per_trade_usd": round(total_fees / n, 8) if n else None,
        "fee_as_pct_gross_pnl": fee_pct_gross,
        "total_gross_pnl_usd": round(total_pnl, 8),
    }


# ─────────────────────────────────────────────────────────────────────────────
# MAE / MFE
# ─────────────────────────────────────────────────────────────────────────────

def _compute_mae_mfe(trade_rows: List[Dict[str, str]]) -> Dict[str, Any]:
    mae_vals: List[float] = []
    mfe_vals: List[float] = []
    for row in trade_rows:
        mae = row.get("mae_pct", "").strip()
        mfe = row.get("mfe_pct", "").strip()
        if mae:
            mae_vals.append(_safe_float(mae))
        if mfe:
            mfe_vals.append(_safe_float(mfe))

    return {
        "mae_pct": _percentiles(mae_vals),
        "mfe_pct": _percentiles(mfe_vals),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Trade duration
# ─────────────────────────────────────────────────────────────────────────────

def _compute_duration(trade_rows: List[Dict[str, str]]) -> Dict[str, Any]:
    vals: List[float] = []
    for row in trade_rows:
        d = row.get("trade_duration_s", "").strip()
        if d:
            vals.append(_safe_float(d))
    return {"trade_duration_s": _percentiles(vals)}


# ─────────────────────────────────────────────────────────────────────────────
# Main report assembly
# ─────────────────────────────────────────────────────────────────────────────

def build_report(log_dir: Path) -> Dict[str, Any]:
    fills = _read_fills(log_dir)
    order_events = _read_order_events(log_dir)
    trade_rows = _read_all_trade_journals(log_dir)

    n_fills = len([r for r in fills if r.get("side", "").upper() in ("BUY", "SELL")])
    n_trades = len(trade_rows)

    warnings: List[str] = []
    if n_fills < MINIMUM_FILLS:
        warnings.append(
            f"Only {n_fills} fills found; {MINIMUM_FILLS}+ required for meaningful distributions. "
            "Keep paper adapter running and accumulate more data."
        )

    # ── Latency
    lat_groups = _compute_latency_ms(order_events)
    latency = {
        "all": _percentiles(lat_groups["all"]),
        "entry": _percentiles(lat_groups["entry"]),
        "exit": _percentiles(lat_groups["exit"]),
    }

    # ── Slippage
    _, by_regime, by_session, entry_slip, exit_slip = _compute_slippage(trade_rows)
    all_slip = entry_slip + exit_slip
    trades_with_mid_px = len(entry_slip)

    slippage = {
        "all": _percentiles(all_slip),
        "entry": _percentiles(entry_slip),
        "exit": _percentiles(exit_slip),
        "by_regime": _bucket_percentiles(dict(by_regime)),
        "by_session": _bucket_percentiles(dict(by_session)),
    }

    if trades_with_mid_px == 0 and n_trades > 0:
        warnings.append(
            "No mid-price data found in trade_journal (entry_mid_px / exit_mid_px empty). "
            "Slippage distributions are unavailable until mid-price capture is live."
        )

    # ── Fee drag
    fee_drag = _compute_fee_drag(trade_rows, fills)

    # ── MAE / MFE
    mae_mfe = _compute_mae_mfe(trade_rows)

    # ── Duration
    duration = _compute_duration(trade_rows)

    # ── Configured slippage summary (from journal field, not computed)
    configured_slip_vals: List[float] = []
    for row in trade_rows:
        v = row.get("slippage_bps", "").strip()
        if v:
            configured_slip_vals.append(_safe_float(v))

    return {
        "version": REPORT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "log_dir": str(log_dir),
        "latency_ms": latency,
        "slippage_bps": slippage,
        "configured_slippage_bps": _percentiles(configured_slip_vals),
        "fee_drag": fee_drag,
        "mae_mfe": mae_mfe,
        "trade_duration": duration,
        "sample_sizes": {
            "total_fills": n_fills,
            "entry_fills": len([r for r in fills if r.get("side", "").upper() == "BUY"]),
            "exit_fills": len([r for r in fills if r.get("side", "").upper() == "SELL"]),
            "total_trades": n_trades,
            "trades_with_mid_px": trades_with_mid_px,
            "order_events": len(order_events),
            "minimum_required": MINIMUM_FILLS,
        },
        "data_quality": {
            "sufficient_data": n_fills >= MINIMUM_FILLS,
            "warnings": warnings,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# HTML summary
# ─────────────────────────────────────────────────────────────────────────────

def _fmt(v: Any, suffix: str = "", na: str = "n/a") -> str:
    if v is None:
        return na
    return f"{v}{suffix}"


def _pct_row(label: str, d: Dict) -> str:
    return (
        f"<tr><td>{label}</td>"
        f"<td>{_fmt(d.get('p50'))}</td>"
        f"<td>{_fmt(d.get('p95'))}</td>"
        f"<td>{_fmt(d.get('p99'))}</td>"
        f"<td>{_fmt(d.get('mean'))}</td>"
        f"<td>{_fmt(d.get('min'))}</td>"
        f"<td>{_fmt(d.get('max'))}</td>"
        f"<td>{d.get('count', 0)}</td></tr>"
    )


def _table(title: str, header_cols: List[str], rows_html: str) -> str:
    headers = "".join(f"<th>{h}</th>" for h in header_cols)
    return (
        f"<h2>{title}</h2>"
        f"<table><thead><tr>{headers}</tr></thead><tbody>{rows_html}</tbody></table>"
    )


def build_html(report: Dict[str, Any]) -> str:
    generated = report.get("generated_at", "")
    sizes = report.get("sample_sizes", {})
    dq = report.get("data_quality", {})
    warn_html = ""
    for w in dq.get("warnings", []):
        warn_html += f'<div class="warn">&#9888; {w}</div>\n'

    pct_cols = ["Metric", "p50", "p95", "p99", "mean", "min", "max", "n"]

    # Latency
    lat = report.get("latency_ms", {})
    lat_rows = (
        _pct_row("All fills", lat.get("all", {}))
        + _pct_row("Entry (BUY)", lat.get("entry", {}))
        + _pct_row("Exit (SELL)", lat.get("exit", {}))
    )

    # Slippage
    slip = report.get("slippage_bps", {})
    slip_rows = (
        _pct_row("All trades", slip.get("all", {}))
        + _pct_row("Entry", slip.get("entry", {}))
        + _pct_row("Exit", slip.get("exit", {}))
    )
    by_regime = slip.get("by_regime", {})
    for regime, d in sorted(by_regime.items()):
        slip_rows += _pct_row(f"Regime: {regime}", d)
    by_session = slip.get("by_session", {})
    for session, d in sorted(by_session.items()):
        slip_rows += _pct_row(f"Session: {session}", d)

    # Fee drag
    fee = report.get("fee_drag", {})
    fee_rows = (
        f"<tr><td>Total fees (journal)</td><td colspan='7'>{_fmt(fee.get('total_fees_usd_from_journal'))} USD</td></tr>"
        f"<tr><td>Total fees (fills)</td><td colspan='7'>{_fmt(fee.get('total_fees_usd_from_fills'))} USD</td></tr>"
        f"<tr><td>Avg fee / trade</td><td colspan='7'>{_fmt(fee.get('avg_fee_per_trade_usd'))} USD</td></tr>"
        f"<tr><td>Fee as % gross PnL</td><td colspan='7'>{_fmt(fee.get('fee_as_pct_gross_pnl'))}%</td></tr>"
        f"<tr><td>Total gross PnL</td><td colspan='7'>{_fmt(fee.get('total_gross_pnl_usd'))} USD</td></tr>"
    )

    # MAE / MFE
    mm = report.get("mae_mfe", {})
    mae_mfe_rows = (
        _pct_row("MAE %", mm.get("mae_pct", {}))
        + _pct_row("MFE %", mm.get("mfe_pct", {}))
    )

    # Duration
    dur = report.get("trade_duration", {})
    dur_rows = _pct_row("Duration (s)", dur.get("trade_duration_s", {}))

    sufficient = dq.get("sufficient_data", False)
    status_cls = "ok" if sufficient else "warn-badge"
    status_txt = "SUFFICIENT DATA" if sufficient else f"INSUFFICIENT DATA ({sizes.get('total_fills', 0)}/{sizes.get('minimum_required', 30)} fills)"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Argus Friction Report</title>
<style>
  body {{ font-family: monospace; background: #111; color: #ccc; margin: 2rem; }}
  h1 {{ color: #eee; }}
  h2 {{ color: #aaa; margin-top: 2rem; border-bottom: 1px solid #333; padding-bottom: 4px; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 1rem; }}
  th {{ background: #222; color: #999; padding: 6px 12px; text-align: left; }}
  td {{ padding: 5px 12px; border-bottom: 1px solid #222; }}
  tr:hover td {{ background: #1a1a1a; }}
  .warn {{ background: #2a1a00; border-left: 3px solid #f90; padding: 8px 14px; margin: 6px 0; color: #f90; }}
  .ok {{ color: #0f0; font-weight: bold; }}
  .warn-badge {{ color: #f90; font-weight: bold; }}
  .meta {{ color: #666; font-size: 0.85em; margin-bottom: 1rem; }}
</style>
</head>
<body>
<h1>Argus — Friction Report (Phase 11)</h1>
<div class="meta">Generated: {generated}</div>
<div class="meta">Log dir: {report.get('log_dir', '')}</div>
<p>Data quality: <span class="{status_cls}">{status_txt}</span></p>
{warn_html}

<h2>Sample Sizes</h2>
<table><tbody>
  <tr><td>Total fills</td><td>{sizes.get('total_fills', 0)}</td></tr>
  <tr><td>Entry fills</td><td>{sizes.get('entry_fills', 0)}</td></tr>
  <tr><td>Exit fills</td><td>{sizes.get('exit_fills', 0)}</td></tr>
  <tr><td>Total trades</td><td>{sizes.get('total_trades', 0)}</td></tr>
  <tr><td>Trades with mid-price data</td><td>{sizes.get('trades_with_mid_px', 0)}</td></tr>
  <tr><td>Order events</td><td>{sizes.get('order_events', 0)}</td></tr>
  <tr><td>Minimum required</td><td>{sizes.get('minimum_required', 30)}</td></tr>
</tbody></table>

{_table("Latency (ms) — ORDER_ACCEPTED → ORDER_FILLED", pct_cols, lat_rows)}
{_table("Actual Slippage (bps) — fill_px vs mid_px", pct_cols, slip_rows)}
{_table("Fee Drag", pct_cols, fee_rows)}
{_table("MAE / MFE", pct_cols, mae_mfe_rows)}
{_table("Trade Duration", pct_cols, dur_rows)}

</body>
</html>
"""
    return html


# ─────────────────────────────────────────────────────────────────────────────
# Output writers
# ─────────────────────────────────────────────────────────────────────────────

def write_report(report: Dict[str, Any], out_dir: Path, run_id: str) -> Tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"friction_report_{run_id}.json"
    html_path = out_dir / f"friction_summary_{run_id}.html"

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    with html_path.open("w", encoding="utf-8") as f:
        f.write(build_html(report))

    return json_path, html_path


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Phase 11 friction report — reads fills + trade_journal, writes JSON + HTML"
    )
    parser.add_argument(
        "--log-dir",
        default="ops/logs",
        help="Directory containing fills.csv, order_events.csv, trade_journal_*.csv (default: ops/logs)",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Output directory for report files (default: same as --log-dir)",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Run ID suffix for output filenames (default: current UTC timestamp)",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        dest="open_browser",
        help="Open friction_summary HTML in browser after generation",
    )
    args = parser.parse_args(argv)

    log_dir = Path(args.log_dir).resolve()
    out_dir = Path(args.out_dir).resolve() if args.out_dir else log_dir
    run_id = args.run_id or _default_run_id()

    if not log_dir.exists():
        print(f"ERROR: log_dir does not exist: {log_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Building friction report from {log_dir} ...")
    report = build_report(log_dir)

    json_path, html_path = write_report(report, out_dir, run_id)

    dq = report["data_quality"]
    for w in dq["warnings"]:
        print(f"  WARNING: {w}")

    sizes = report["sample_sizes"]
    print(f"  Fills analyzed:  {sizes['total_fills']}")
    print(f"  Trades analyzed: {sizes['total_trades']}")
    print(f"  Sufficient data: {dq['sufficient_data']}")

    slip = report["slippage_bps"]
    lat = report["latency_ms"]
    print(f"  Slippage p50/p95/p99 (all): "
          f"{slip['all'].get('p50')} / {slip['all'].get('p95')} / {slip['all'].get('p99')} bps")
    print(f"  Latency  p50/p95/p99 (all): "
          f"{lat['all'].get('p50')} / {lat['all'].get('p95')} / {lat['all'].get('p99')} ms")

    print(f"\n  JSON  -> {json_path}")
    print(f"  HTML  -> {html_path}")

    if args.open_browser:
        import webbrowser
        webbrowser.open(html_path.as_uri())


if __name__ == "__main__":
    main()
