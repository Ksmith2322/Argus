#!/usr/bin/env python3
# analytics/attribution.py
#
# Phase 14 — Performance Attribution and Diagnostics
#
# Reads:
#   trade_journal_*.csv (canonical lifecycle truth, from log_dir, non-recursive)
#   bt_summary_*.json   (backtest summary context, from log_dir/bt sub-dir)
#
# Writes:
#   attribution_<date>.json — machine-readable attribution tables
#   attribution_<date>.html — human-readable summary report (self-contained)
#
# Attribution dimensions (from roadmap Phase 14):
#   * regime  — trend / range / volatile / unknown (from regime_at_entry)
#   * session — London / NY / overlap / Asian / off-hours (derived from entry_time UTC)
#   * volatility bucket — ATR percentile  [NOT IN CURRENT SCHEMA — stub only]
#   * liquidity state                      [NOT IN CURRENT SCHEMA — stub only]
#   * entry reason attribution (from entry_reason)
#   * hold time distribution and PnL correlation
#   * MAE/MFE analysis (too early / too late exit detection)
#   * fee drag as % of gross PnL per bucket
#
# TRUTH-SURFACE DOCTRINE: reads only from canonical source artifacts.
# Never mutates lower-layer files. Pure analytics — no runtime control state.
#
# NOTE: With < 20 trades the distributions are illustrative only.
#       The report flags data_quality.sufficient_data = false and continues.
#
# Usage:
#   python -m analytics.attribution
#   python -m analytics.attribution --log-dir ops/logs --out-dir ops/logs

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

MINIMUM_TRADES    = 20       # below this, distributions are illustrative only
REPORT_VERSION    = "1.0"

# Session boundary definitions (UTC hour, inclusive-start, exclusive-end)
SESSION_BOUNDARIES = [
    ("asia",     0,  7),   # 00:00–07:00 UTC
    ("london",   7, 12),   # 07:00–12:00 UTC
    ("overlap", 12, 16),   # 12:00–16:00 UTC (London/NY)
    ("ny",      16, 21),   # 16:00–21:00 UTC
    ("off_hrs", 21, 24),   # 21:00–24:00 UTC
]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_float(v: Any, default: float = 0.0) -> float:
    if v is None or str(v).strip() in ("", "None", "null"):
        return default
    try:
        return float(str(v).strip())
    except Exception:
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    if v is None or str(v).strip() in ("", "None", "null"):
        return default
    try:
        return int(float(str(v).strip()))
    except Exception:
        return default


def _percentiles(data: List[float]) -> Dict[str, Any]:
    """Return p25/p50/p75/p95/min/max/mean/count dict."""
    if not data:
        return {"p25": None, "p50": None, "p75": None, "p95": None,
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
        "p25":  round(pct(25), 8),
        "p50":  round(pct(50), 8),
        "p75":  round(pct(75), 8),
        "p95":  round(pct(95), 8),
        "min":  round(s[0],    8),
        "max":  round(s[-1],   8),
        "mean": round(sum(s) / n, 8),
        "count": n,
    }


def _session_label(entry_time_str: str) -> str:
    """
    Derive session label (asia/london/overlap/ny/off_hrs) from
    an ISO-8601 datetime string.  Falls back to 'unknown' on parse error.
    """
    try:
        # Handle both Z and +00:00 suffixes
        ts = entry_time_str.strip()
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        # Normalise to UTC
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
        hour = dt.hour
        for label, start, end in SESSION_BOUNDARIES:
            if start <= hour < end:
                return label
        return "off_hrs"
    except Exception:
        return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Data readers
# ─────────────────────────────────────────────────────────────────────────────

def _read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _read_all_trade_journals(log_dir: Path) -> List[Dict[str, str]]:
    """Read all trade_journal_*.csv from log_dir (non-recursive, sorted by name)."""
    rows: List[Dict[str, str]] = []
    for p in sorted(log_dir.glob("trade_journal_*.csv")):
        rows.extend(_read_csv(p))
    return rows


def _read_all_bt_summaries(log_dir: Path) -> List[Dict[str, Any]]:
    """
    Read all bt_summary_*.json from log_dir and from log_dir/bt/.
    Skip bt_summary_latest.json (it duplicates the newest run).
    """
    summaries: List[Dict[str, Any]] = []
    search_dirs = [log_dir, log_dir / "bt"]
    seen_run_ids: set = set()
    for d in search_dirs:
        for p in sorted(d.glob("bt_summary_*.json")):
            if "latest" in p.name:
                continue
            try:
                with p.open("r", encoding="utf-8") as f:
                    obj = json.load(f)
                run_id = obj.get("run_id", str(p))
                if run_id not in seen_run_ids:
                    seen_run_ids.add(run_id)
                    summaries.append(obj)
            except Exception:
                pass
    return summaries


# ─────────────────────────────────────────────────────────────────────────────
# Bucket accumulator
# ─────────────────────────────────────────────────────────────────────────────

class _Bucket:
    """Accumulates per-trade stats for one attribution bucket."""

    __slots__ = (
        "pnl_seq", "win_count", "loss_count",
        "gross_wins", "gross_losses",
        "fee_seq", "mae_seq", "mfe_seq", "duration_seq",
    )

    def __init__(self) -> None:
        self.pnl_seq:      List[float] = []
        self.win_count:    int         = 0
        self.loss_count:   int         = 0
        self.gross_wins:   float       = 0.0
        self.gross_losses: float       = 0.0
        self.fee_seq:      List[float] = []
        self.mae_seq:      List[float] = []
        self.mfe_seq:      List[float] = []
        self.duration_seq: List[float] = []

    def add(self, pnl: float, fee: float, mae: float,
            mfe: float, duration: float) -> None:
        self.pnl_seq.append(pnl)
        self.fee_seq.append(fee)
        self.mae_seq.append(mae)
        self.mfe_seq.append(mfe)
        self.duration_seq.append(duration)
        if pnl > 0:
            self.win_count  += 1
            self.gross_wins += pnl
        else:
            self.loss_count   += 1
            self.gross_losses += abs(pnl)

    def to_dict(self) -> Dict[str, Any]:
        n = len(self.pnl_seq)
        if n == 0:
            return {"count": 0, "win_rate": None, "expectancy": None,
                    "total_pnl": None, "fee_drag_pct": None,
                    "avg_mae_pct": None, "avg_mfe_pct": None,
                    "avg_duration_s": None}

        total_pnl  = sum(self.pnl_seq)
        expectancy = total_pnl / n
        win_rate   = self.win_count / n

        # Fee drag: sum(fees) / gross_wins * 100  (how much of gross wins eaten)
        total_fees = sum(self.fee_seq)
        fee_drag_pct = (
            round(total_fees / self.gross_wins * 100.0, 4)
            if self.gross_wins > 0 else None
        )

        avg_mae = (sum(self.mae_seq) / n) if self.mae_seq else None
        avg_mfe = (sum(self.mfe_seq) / n) if self.mfe_seq else None
        avg_dur = (sum(self.duration_seq) / n) if self.duration_seq else None

        return {
            "count":          n,
            "win_count":      self.win_count,
            "loss_count":     self.loss_count,
            "win_rate":       round(win_rate,   6),
            "total_pnl":      round(total_pnl,  8),
            "expectancy":     round(expectancy, 8),
            "avg_mae_pct":    round(avg_mae, 6) if avg_mae is not None else None,
            "avg_mfe_pct":    round(avg_mfe, 6) if avg_mfe is not None else None,
            "avg_duration_s": round(avg_dur, 2) if avg_dur is not None else None,
            "total_fees":     round(total_fees, 8),
            "fee_drag_pct":   fee_drag_pct,
            "gross_wins":     round(self.gross_wins,   8),
            "gross_losses":   round(self.gross_losses, 8),
            "pnl_distribution": _percentiles(self.pnl_seq),
        }


def _bucketize(
    rows: List[Dict[str, str]],
    key_fn,
) -> Dict[str, Any]:
    """
    Group rows by key_fn(row) and return bucket stats per key.
    key_fn must return a hashable string label.
    """
    buckets: Dict[str, _Bucket] = {}
    for row in rows:
        label = key_fn(row)
        if label not in buckets:
            buckets[label] = _Bucket()
        pnl      = _safe_float(row.get("pnl"))
        fee      = _safe_float(row.get("total_fee"))
        mae      = _safe_float(row.get("mae_pct"))
        mfe      = _safe_float(row.get("mfe_pct"))
        duration = _safe_float(row.get("trade_duration_s"))
        buckets[label].add(pnl, fee, mae, mfe, duration)

    return {label: bkt.to_dict() for label, bkt in sorted(buckets.items())}


# ─────────────────────────────────────────────────────────────────────────────
# Hold-time quantile analysis
# ─────────────────────────────────────────────────────────────────────────────

def _hold_time_analysis(rows: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Bin trades by hold duration (quartile of the distribution) and
    compute PnL stats per quartile.  Also returns Pearson correlation
    between duration and pnl (to detect if longer holds pay more).
    """
    if not rows:
        return {"quartile_buckets": {}, "duration_pnl_correlation": None,
                "duration_distribution": _percentiles([])}

    durations = [_safe_float(r.get("trade_duration_s")) for r in rows]
    pnls      = [_safe_float(r.get("pnl"))              for r in rows]
    n = len(durations)

    dur_dist = _percentiles(durations)

    # Pearson correlation
    corr: Optional[float] = None
    if n >= 3:
        mu_d = sum(durations) / n
        mu_p = sum(pnls) / n
        cov  = sum((d - mu_d) * (p - mu_p) for d, p in zip(durations, pnls)) / n
        sd_d = math.sqrt(sum((d - mu_d) ** 2 for d in durations) / n)
        sd_p = math.sqrt(sum((p - mu_p) ** 2 for p in pnls) / n)
        if sd_d > 0 and sd_p > 0:
            corr = round(cov / (sd_d * sd_p), 6)

    # Quartile boundaries from the distribution
    q25 = dur_dist["p25"] or 0.0
    q50 = dur_dist["p50"] or 0.0
    q75 = dur_dist["p75"] or 0.0

    def _quartile_label(d: float) -> str:
        if d <= q25: return "Q1_short"
        if d <= q50: return "Q2"
        if d <= q75: return "Q3"
        return "Q4_long"

    qbuckets = _bucketize(rows, lambda r: _quartile_label(_safe_float(r.get("trade_duration_s"))))

    return {
        "duration_distribution":    dur_dist,
        "duration_pnl_correlation": corr,
        "quartile_buckets":         qbuckets,
        "quartile_boundaries": {
            "q25_s": q25, "q50_s": q50, "q75_s": q75,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# MAE/MFE analysis
# ─────────────────────────────────────────────────────────────────────────────

def _mae_mfe_analysis(rows: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Analyse MAE/MFE to detect whether exits are too early or too late.

    Key ratios:
      * mfe_capture_pct: pnl / mfe — how much of the maximum favourable
        excursion was actually captured (higher = better exit timing)
      * mae_to_mfe_ratio: mae / mfe — how much pain per unit of potential
        (lower = cleaner entries relative to their potential)
    """
    if not rows:
        return {
            "mae_distribution": _percentiles([]),
            "mfe_distribution": _percentiles([]),
            "mfe_capture_distribution": _percentiles([]),
            "mae_mfe_ratio_distribution": _percentiles([]),
            "exit_timing_note": "no data",
        }

    maes:          List[float] = []
    mfes:          List[float] = []
    mfe_captures:  List[float] = []
    mae_mfe_ratios: List[float] = []

    for row in rows:
        mae = _safe_float(row.get("mae_pct"))   # negative or zero
        mfe = _safe_float(row.get("mfe_pct"))   # positive or zero
        pnl = _safe_float(row.get("pnl"))
        entry_px = _safe_float(row.get("entry_px"), default=1.0)

        maes.append(mae)
        mfes.append(mfe)

        # mfe_capture: pnl / (mfe * entry_px / 100) in same currency terms
        # Simplified: compare pnl% vs mfe_pct
        # pnl_pct = (exit_px - entry_px) / entry_px * 100
        exit_px  = _safe_float(row.get("exit_px"), default=0.0)
        qty      = _safe_float(row.get("qty"),      default=0.0)
        if entry_px > 0 and qty > 0:
            pnl_pct = (exit_px - entry_px) / entry_px * 100.0
        else:
            pnl_pct = 0.0

        if mfe > 0:
            mfe_captures.append(round(pnl_pct / mfe, 6))

        if mfe > 0:
            mae_abs = abs(mae)
            mae_mfe_ratios.append(round(mae_abs / mfe, 6))

    # Exit timing interpretation
    median_capture = _percentiles(mfe_captures).get("p50")
    note = "insufficient data"
    if median_capture is not None:
        if median_capture >= 0.80:
            note = "exits capture >=80% of MFE — good timing"
        elif median_capture >= 0.50:
            note = "exits capture 50-80% of MFE — moderate leaving-on-table risk"
        else:
            note = "exits capture <50% of MFE — significant leaving-on-table risk"

    return {
        "mae_distribution":           _percentiles(maes),
        "mfe_distribution":           _percentiles(mfes),
        "mfe_capture_distribution":   _percentiles(mfe_captures),
        "mae_mfe_ratio_distribution": _percentiles(mae_mfe_ratios),
        "exit_timing_note":           note,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Fee drag analysis
# ─────────────────────────────────────────────────────────────────────────────

def _fee_drag_analysis(rows: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Overall fee drag statistics across all trades.
    """
    if not rows:
        return {
            "total_fees": 0.0,
            "gross_wins": 0.0,
            "gross_losses": 0.0,
            "net_pnl": 0.0,
            "fee_as_pct_gross_wins": None,
            "fee_per_trade_distribution": _percentiles([]),
        }

    total_fees   = 0.0
    gross_wins   = 0.0
    gross_losses = 0.0
    net_pnl      = 0.0
    fee_per_trade: List[float] = []

    for row in rows:
        pnl = _safe_float(row.get("pnl"))
        fee = _safe_float(row.get("total_fee"))
        net_pnl      += pnl
        total_fees   += fee
        fee_per_trade.append(fee)
        if pnl > 0:
            gross_wins   += pnl
        else:
            gross_losses += abs(pnl)

    fee_as_pct = (
        round(total_fees / gross_wins * 100.0, 4)
        if gross_wins > 0 else None
    )

    return {
        "total_fees":              round(total_fees,   8),
        "gross_wins":              round(gross_wins,   8),
        "gross_losses":            round(gross_losses, 8),
        "net_pnl":                 round(net_pnl,      8),
        "fee_as_pct_gross_wins":   fee_as_pct,
        "fee_per_trade_distribution": _percentiles(fee_per_trade),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Backtest summary context
# ─────────────────────────────────────────────────────────────────────────────

def _bt_summary_context(summaries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Aggregate key fields from bt_summary_*.json for context.
    Returns: run count, total trades closed, combined PnL, avg win_rate.
    """
    if not summaries:
        return {"run_count": 0, "total_trades_closed": 0,
                "combined_pnl_usd": 0.0, "avg_win_rate_pct": None}

    total_closed = sum(_safe_int(s.get("trades_closed"))   for s in summaries)
    combined_pnl = sum(_safe_float(s.get("pnl_usd", "0")) for s in summaries)
    win_rates    = [_safe_float(s.get("win_rate_pct", "0")) for s in summaries]
    avg_wr       = sum(win_rates) / len(win_rates) if win_rates else None

    return {
        "run_count":           len(summaries),
        "total_trades_closed": total_closed,
        "combined_pnl_usd":    round(combined_pnl, 8),
        "avg_win_rate_pct":    round(avg_wr, 4) if avg_wr is not None else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Dominant-bucket guard
# ─────────────────────────────────────────────────────────────────────────────

def _dominance_check(
    buckets: Dict[str, Any],
    dimension: str,
) -> Dict[str, Any]:
    """
    Detect if any single bucket generates > 80% of total PnL.
    Returns: {dominant_bucket, dominant_pct, fragility_flag}
    """
    total = sum(
        b.get("total_pnl") or 0.0
        for b in buckets.values()
        if b.get("total_pnl") is not None
    )
    if total == 0 or len(buckets) == 0:
        return {"dominant_bucket": None, "dominant_pct": None,
                "fragility_flag": False, "note": "no PnL to analyse"}

    dominant   = max(buckets.items(), key=lambda kv: (kv[1].get("total_pnl") or 0.0))
    dom_name   = dominant[0]
    dom_pnl    = dominant[1].get("total_pnl") or 0.0
    dom_pct    = round(dom_pnl / total * 100.0, 2) if total > 0 else None
    fragile    = dom_pct is not None and dom_pct > 80.0

    note = (
        f"WARNING: {dimension} bucket '{dom_name}' generates {dom_pct}% of "
        "total PnL — single-bucket fragility detected."
        if fragile else
        f"No single {dimension} bucket dominates (>{80}% threshold)."
    )

    return {
        "dominant_bucket": dom_name,
        "dominant_pct":    dom_pct,
        "fragility_flag":  fragile,
        "note":            note,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main model assembly
# ─────────────────────────────────────────────────────────────────────────────

def build_attribution(
    log_dir: Path,
) -> Dict[str, Any]:
    """
    Build the full attribution model from trade_journal_*.csv and
    bt_summary_*.json in log_dir.
    """
    trade_rows = _read_all_trade_journals(log_dir)
    summaries  = _read_all_bt_summaries(log_dir)
    n_trades   = len(trade_rows)

    warnings: List[str] = []
    if n_trades == 0:
        warnings.append(
            "No trade_journal_*.csv rows found in log_dir. "
            "Attribution tables will be empty."
        )
    elif n_trades < MINIMUM_TRADES:
        warnings.append(
            f"Only {n_trades} trades found; {MINIMUM_TRADES}+ recommended "
            "for meaningful attribution. Current output is illustrative only."
        )

    # ── Regime attribution
    regime_buckets = _bucketize(
        trade_rows,
        lambda r: (r.get("regime_at_entry") or "UNKNOWN").upper().strip() or "UNKNOWN",
    )

    # ── Session attribution
    session_buckets = _bucketize(
        trade_rows,
        lambda r: _session_label(r.get("entry_time", "")),
    )

    # ── Entry-reason attribution
    reason_buckets = _bucketize(
        trade_rows,
        lambda r: (r.get("entry_reason") or "UNKNOWN").strip() or "UNKNOWN",
    )

    # ── Volatility bucket (not available in current schema — stub)
    vol_stub = {
        "note": (
            "Volatility bucket attribution requires ATR-percentile-at-entry "
            "in the trade_journal schema. Not available in current version."
        ),
        "buckets": {},
    }

    # ── Liquidity state (not available in current schema — stub)
    liq_stub = {
        "note": (
            "Liquidity state attribution requires liquidity_state_at_entry "
            "in the trade_journal schema. Not available in current version."
        ),
        "buckets": {},
    }

    # ── Hold time analysis
    hold_time = _hold_time_analysis(trade_rows)

    # ── MAE/MFE analysis
    mae_mfe = _mae_mfe_analysis(trade_rows)

    # ── Fee drag
    fee_drag = _fee_drag_analysis(trade_rows)

    # ── Bt summary context
    bt_ctx = _bt_summary_context(summaries)

    # ── Dominance guards
    regime_dominance  = _dominance_check(regime_buckets,  "regime")
    session_dominance = _dominance_check(session_buckets, "session")
    reason_dominance  = _dominance_check(reason_buckets,  "entry_reason")

    return {
        "version":        REPORT_VERSION,
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "log_dir":        str(log_dir),
        "data_quality": {
            "sufficient_data":  n_trades >= MINIMUM_TRADES,
            "n_trades":         n_trades,
            "minimum_required": MINIMUM_TRADES,
            "warnings":         warnings,
        },
        "bt_summary_context":   bt_ctx,
        "regime_attribution": {
            "buckets":   regime_buckets,
            "dominance": regime_dominance,
        },
        "session_attribution": {
            "buckets":         session_buckets,
            "dominance":       session_dominance,
            "boundary_note":   (
                "asia=00-07 UTC, london=07-12 UTC, "
                "overlap=12-16 UTC, ny=16-21 UTC, off_hrs=21-24 UTC"
            ),
        },
        "entry_reason_attribution": {
            "buckets":   reason_buckets,
            "dominance": reason_dominance,
        },
        "volatility_bucket_attribution": vol_stub,
        "liquidity_state_attribution":   liq_stub,
        "hold_time_analysis":   hold_time,
        "mae_mfe_analysis":     mae_mfe,
        "fee_drag_analysis":    fee_drag,
    }


# ─────────────────────────────────────────────────────────────────────────────
# HTML report generator
# ─────────────────────────────────────────────────────────────────────────────

_HTML_STYLE = """
<style>
  body { font-family: monospace; background: #0f0f0f; color: #d0d0d0; margin: 24px; }
  h1 { color: #7ec8e3; margin-bottom: 4px; }
  h2 { color: #a8d8a8; border-bottom: 1px solid #333; padding-bottom: 4px; margin-top: 32px; }
  h3 { color: #c8a8d8; margin-bottom: 6px; margin-top: 20px; }
  table { border-collapse: collapse; margin-bottom: 16px; width: auto; }
  th { background: #1e2a1e; color: #a8d8a8; padding: 6px 12px; text-align: left;
       border: 1px solid #333; font-size: 0.85em; }
  td { padding: 5px 12px; border: 1px solid #2a2a2a; font-size: 0.85em; }
  tr:nth-child(even) { background: #161616; }
  .warn { color: #e8c07a; background: #1e1800; padding: 8px 12px;
          border-left: 3px solid #c09040; margin-bottom: 12px; }
  .ok   { color: #8dc888; }
  .bad  { color: #e86060; }
  .na   { color: #666; font-style: italic; }
  .note { color: #888; font-size: 0.82em; margin-top: 4px; }
  .sub  { color: #999; font-size: 0.8em; }
  pre   { background: #111; padding: 12px; overflow-x: auto; font-size: 0.82em; }
</style>
"""


def _fmt(v: Any, decimals: int = 4) -> str:
    if v is None:
        return '<span class="na">n/a</span>'
    if isinstance(v, float):
        return f"{v:.{decimals}f}"
    return str(v)


def _pct_cell(v: Any) -> str:
    if v is None:
        return '<span class="na">n/a</span>'
    cls = "ok" if float(v) >= 0 else "bad"
    return f'<span class="{cls}">{float(v):.2f}%</span>'


def _pnl_cell(v: Any) -> str:
    if v is None:
        return '<span class="na">n/a</span>'
    fv = float(v)
    cls = "ok" if fv >= 0 else "bad"
    return f'<span class="{cls}">{fv:+.6f}</span>'


def _bucket_table(
    buckets: Dict[str, Any],
    dimension_label: str,
) -> str:
    if not buckets:
        return '<p class="na">No trades in this dimension.</p>'

    rows_html = ""
    for label, b in sorted(buckets.items()):
        count    = b.get("count", 0)
        win_rate = b.get("win_rate")
        exp      = b.get("expectancy")
        tot_pnl  = b.get("total_pnl")
        fee_drag = b.get("fee_drag_pct")
        avg_mae  = b.get("avg_mae_pct")
        avg_mfe  = b.get("avg_mfe_pct")
        avg_dur  = b.get("avg_duration_s")

        wr_str  = _pct_cell(float(win_rate) * 100 if win_rate is not None else None)
        exp_str = _pnl_cell(exp)
        pnl_str = _pnl_cell(tot_pnl)
        fd_str  = _fmt(fee_drag, 2) + "%" if fee_drag is not None else '<span class="na">n/a</span>'
        mae_str = _fmt(avg_mae, 4) if avg_mae is not None else '<span class="na">n/a</span>'
        mfe_str = _fmt(avg_mfe, 4) if avg_mfe is not None else '<span class="na">n/a</span>'
        dur_str = f"{avg_dur:.0f}s" if avg_dur is not None else '<span class="na">n/a</span>'

        rows_html += (
            f"<tr>"
            f"<td><b>{label}</b></td>"
            f"<td>{count}</td>"
            f"<td>{wr_str}</td>"
            f"<td>{exp_str}</td>"
            f"<td>{pnl_str}</td>"
            f"<td>{fd_str}</td>"
            f"<td>{mae_str}</td>"
            f"<td>{mfe_str}</td>"
            f"<td>{dur_str}</td>"
            f"</tr>\n"
        )

    return (
        "<table>"
        "<tr>"
        f"<th>{dimension_label}</th>"
        "<th>Trades</th>"
        "<th>Win%</th>"
        "<th>Expectancy</th>"
        "<th>Total PnL</th>"
        "<th>Fee Drag</th>"
        "<th>Avg MAE%</th>"
        "<th>Avg MFE%</th>"
        "<th>Avg Hold</th>"
        "</tr>\n"
        + rows_html
        + "</table>"
    )


def _dominance_banner(dom: Dict[str, Any]) -> str:
    if dom.get("fragility_flag"):
        return f'<div class="warn">⚠ {dom.get("note", "")}</div>'
    note = dom.get("note", "")
    return f'<p class="note ok">✓ {note}</p>'


def _pct_dist_row(label: str, dist: Dict[str, Any]) -> str:
    p25 = dist.get("p25")
    p50 = dist.get("p50")
    p75 = dist.get("p75")
    p95 = dist.get("p95")
    mn  = dist.get("min")
    mx  = dist.get("max")
    cnt = dist.get("count", 0)
    return (
        f"<tr><td>{label}</td>"
        f"<td>{_fmt(mn,4)}</td>"
        f"<td>{_fmt(p25,4)}</td>"
        f"<td>{_fmt(p50,4)}</td>"
        f"<td>{_fmt(p75,4)}</td>"
        f"<td>{_fmt(p95,4)}</td>"
        f"<td>{_fmt(mx,4)}</td>"
        f"<td>{cnt}</td>"
        f"</tr>\n"
    )


def _dist_table(rows_html: str) -> str:
    header = (
        "<table>"
        "<tr><th>Metric</th><th>Min</th><th>P25</th><th>P50</th>"
        "<th>P75</th><th>P95</th><th>Max</th><th>N</th></tr>\n"
    )
    return header + rows_html + "</table>"


def generate_html(model: Dict[str, Any]) -> str:
    dq   = model["data_quality"]
    bt   = model["bt_summary_context"]
    reg  = model["regime_attribution"]
    ses  = model["session_attribution"]
    rsr  = model["entry_reason_attribution"]
    ht   = model["hold_time_analysis"]
    mf   = model["mae_mfe_analysis"]
    fd   = model["fee_drag_analysis"]
    gen  = model["generated_at"]

    # Warnings block
    warn_html = ""
    for w in dq.get("warnings", []):
        warn_html += f'<div class="warn">⚠ {w}</div>'

    # Data quality banner
    dq_cls   = "ok"   if dq["sufficient_data"] else "bad"
    dq_label = "SUFFICIENT" if dq["sufficient_data"] else "INSUFFICIENT"

    # Bt summary
    bt_html = (
        f"<p>Backtest runs loaded: <b>{bt['run_count']}</b> | "
        f"Total closed trades: <b>{bt['total_trades_closed']}</b> | "
        f"Combined PnL: <b>{_fmt(bt.get('combined_pnl_usd'), 4)}</b> | "
        f"Avg win rate: <b>{_fmt(bt.get('avg_win_rate_pct'), 2)}%</b></p>"
    )

    # MAE/MFE dist table
    mf_rows = (
        _pct_dist_row("MAE %",       mf["mae_distribution"])
        + _pct_dist_row("MFE %",     mf["mfe_distribution"])
        + _pct_dist_row("MFE capture ratio", mf["mfe_capture_distribution"])
        + _pct_dist_row("MAE/MFE ratio",     mf["mae_mfe_ratio_distribution"])
    )

    # Hold time table
    ht_rows = _pct_dist_row("Duration (s)", ht["duration_distribution"])
    corr = ht.get("duration_pnl_correlation")
    corr_str = f"{corr:.4f}" if corr is not None else "n/a"

    # Fee drag summary
    fee_total  = fd.get("total_fees", 0.0)
    fee_net    = fd.get("net_pnl", 0.0)
    fee_drag_overall = fd.get("fee_as_pct_gross_wins")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Argus Attribution Report — {gen[:10]}</title>
  {_HTML_STYLE}
</head>
<body>
<h1>Argus Performance Attribution</h1>
<p class="sub">Generated: {gen} | Source: {model.get('log_dir', '')}</p>
{warn_html}

<p>Data quality: <span class="{dq_cls}"><b>{dq_label}</b></span>
   ({dq['n_trades']} trades, {dq['minimum_required']}+ recommended)</p>

<h2>Backtest Summary Context</h2>
{bt_html}

<h2>Regime Attribution</h2>
{_dominance_banner(reg["dominance"])}
{_bucket_table(reg["buckets"], "Regime")}

<h2>Session Attribution</h2>
<p class="note">{ses.get('boundary_note', '')}</p>
{_dominance_banner(ses["dominance"])}
{_bucket_table(ses["buckets"], "Session")}

<h2>Entry Reason Attribution</h2>
{_dominance_banner(rsr["dominance"])}
{_bucket_table(rsr["buckets"], "Entry Reason")}

<h2>Hold Time Analysis</h2>
<p>Duration–PnL Pearson correlation: <b>{corr_str}</b>
   <span class="note">(positive = longer holds more profitable)</span></p>
{_dist_table(ht_rows)}

<h3>Hold-Time Quartile Buckets</h3>
<p class="note">Quartile boundaries: Q1 ≤ {_fmt(ht.get('quartile_boundaries', {}).get('q25_s'), 1)}s |
  Q2 ≤ {_fmt(ht.get('quartile_boundaries', {}).get('q50_s'), 1)}s |
  Q3 ≤ {_fmt(ht.get('quartile_boundaries', {}).get('q75_s'), 1)}s</p>
{_bucket_table(ht.get("quartile_buckets", {}), "Hold Quartile")}

<h2>MAE / MFE Analysis</h2>
<p>{mf.get('exit_timing_note', 'n/a')}</p>
{_dist_table(mf_rows)}

<h2>Fee Drag Analysis</h2>
<p>Total fees: <b>{_fmt(fee_total, 8)}</b> |
   Net PnL: {_pnl_cell(fee_net)} |
   Fee drag vs gross wins: <b>{_fmt(fee_drag_overall, 2) if fee_drag_overall is not None else 'n/a'}%</b></p>
{_dist_table(_pct_dist_row("Fee per trade", fd.get("fee_per_trade_distribution", {})))}

<h2>Stubs (Schema Extension Pending)</h2>
<p class="note">{model['volatility_bucket_attribution']['note']}</p>
<p class="note">{model['liquidity_state_attribution']['note']}</p>

<hr>
<p class="sub">Attribution report version {model.get('version')} |
Phase 14 — Argus roadmap</p>
</body>
</html>
"""
    return html


# ─────────────────────────────────────────────────────────────────────────────
# Output writers
# ─────────────────────────────────────────────────────────────────────────────

def write_attribution(
    model:    Dict[str, Any],
    out_dir:  Path,
    date_str: str,
) -> Tuple[Path, Path]:
    """Write attribution_<date_str>.json and attribution_<date_str>.html."""
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / f"attribution_{date_str}.json"
    html_path = out_dir / f"attribution_{date_str}.html"

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(model, f, indent=2, default=str)

    with html_path.open("w", encoding="utf-8") as f:
        f.write(generate_html(model))

    return json_path, html_path


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _default_date_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 14 attribution — reads trade_journal_*.csv + bt_summary_*.json, "
            "writes attribution_<date>.json + attribution_<date>.html"
        )
    )
    parser.add_argument(
        "--log-dir",
        default="ops/logs",
        help="Directory containing trade_journal_*.csv files (default: ops/logs)",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Output directory for attribution files (default: same as --log-dir)",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Date string for output filenames (default: current UTC date YYYYMMDD)",
    )
    args = parser.parse_args(argv)

    log_dir  = Path(args.log_dir).resolve()
    out_dir  = Path(args.out_dir).resolve() if args.out_dir else log_dir
    date_str = args.date or _default_date_str()

    if not log_dir.exists():
        print(f"ERROR: log_dir does not exist: {log_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Building attribution from {log_dir} ...")
    model = build_attribution(log_dir)

    json_path, html_path = write_attribution(model, out_dir, date_str)

    dq  = model["data_quality"]
    reg = model["regime_attribution"]
    ses = model["session_attribution"]
    rsr = model["entry_reason_attribution"]
    fd  = model["fee_drag_analysis"]

    for w in dq["warnings"]:
        print(f"  WARNING: {w}")

    print(f"  Trades analysed:     {dq['n_trades']}")
    print(f"  Sufficient data:     {dq['sufficient_data']}")

    print(f"\n  Regime buckets:      {list(reg['buckets'].keys())}")
    dom = reg["dominance"]
    if dom.get("fragility_flag"):
        print(f"  !! REGIME FRAGILITY: {dom.get('note')}")
    else:
        print(f"  Regime dominance:    {dom.get('note')}")

    print(f"\n  Session buckets:     {list(ses['buckets'].keys())}")
    dom = ses["dominance"]
    if dom.get("fragility_flag"):
        print(f"  !! SESSION FRAGILITY: {dom.get('note')}")

    print(f"\n  Entry reason buckets: {list(rsr['buckets'].keys())}")

    print(f"\n  Total fees (USD):    {fd.get('total_fees')}")
    print(f"  Fee drag vs wins:    {fd.get('fee_as_pct_gross_wins')}%")
    print(f"  MAE/MFE note:        {model['mae_mfe_analysis']['exit_timing_note']}")

    print(f"\n  JSON -> {json_path}")
    print(f"  HTML -> {html_path}")


if __name__ == "__main__":
    main()
