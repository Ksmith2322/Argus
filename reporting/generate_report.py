#!/usr/bin/env python3
# reporting/generate_report.py
#
# Phase 15 — Performance Visibility Dashboard
#
# Reads (all from --log-dir, most-recent file of each type):
#   trade_journal_*.csv     — trade lifecycle (canonical source)
#   equity_*.csv            — equity curve per run (latest run)
#   bt_summary_*.json       — backtest run summaries (all, latest highlighted)
#   friction_report_*.json  — Phase 11 friction model (optional)
#   attribution_*.json      — Phase 14 attribution (optional)
#   risk_model_*.json       — Phase 13 risk model (optional)
#
# Writes:
#   report_<date>.html — single self-contained HTML file, no server required.
#                        All chart data is embedded as JSON in the HTML.
#                        Charts rendered via inline Canvas 2D API (no CDN).
#
# Stack: pure Python + inline JS (zero external dependencies)
#
# Usage:
#   python -m reporting.generate_report
#   python -m reporting.generate_report --log-dir ops/logs --out-dir ops/logs
#   python -m reporting.generate_report --no-browser
#
# TRUTH-SURFACE DOCTRINE: reads only from canonical source artifacts.
# This module is pure analytics/display — it must never mutate lower-layer files.

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import webbrowser
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


REPORT_VERSION = "1.0"

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_float(v: Any, default: float = 0.0) -> float:
    if v is None or str(v).strip() in ("", "None", "null", "n/a"):
        return default
    try:
        return float(str(v).strip())
    except Exception:
        return default


def _fmt_usd(v: Optional[float], decimals: int = 4) -> str:
    if v is None:
        return "n/a"
    return f"${v:,.{decimals}f}"


def _fmt_pct(v: Optional[float], decimals: int = 2) -> str:
    if v is None:
        return "n/a"
    return f"{v:.{decimals}f}%"


def _fmt_num(v: Optional[float], decimals: int = 4) -> str:
    if v is None:
        return "n/a"
    return f"{v:.{decimals}f}"


def _utc_now_str() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_date_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


# ─────────────────────────────────────────────────────────────────────────────
# Artifact readers
# ─────────────────────────────────────────────────────────────────────────────

def _read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return {}


def _latest(log_dir: Path, pattern: str) -> Optional[Path]:
    """Return the most recently modified file matching pattern, or None."""
    matches = sorted(log_dir.glob(pattern), key=lambda p: p.stat().st_mtime)
    return matches[-1] if matches else None


def _all_sorted(log_dir: Path, pattern: str) -> List[Path]:
    """Return all files matching pattern sorted by name (alphabetical = chronological)."""
    return sorted(log_dir.glob(pattern))


def _load_trade_journals(log_dir: Path) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for p in _all_sorted(log_dir, "trade_journal_*.csv"):
        rows.extend(_read_csv(p))
    return rows


def _load_equity_csv(log_dir: Path) -> List[Dict[str, str]]:
    """Load the most recent equity_*.csv (latest backtest run)."""
    p = _latest(log_dir, "equity_bt_*.csv")
    return _read_csv(p) if p else []


def _load_all_bt_summaries(log_dir: Path) -> List[Dict[str, Any]]:
    summaries = []
    seen: set = set()
    for search_dir in [log_dir, log_dir / "bt"]:
        for p in _all_sorted(search_dir, "bt_summary_*.json"):
            if "latest" in p.name:
                continue
            obj = _read_json(p)
            run_id = obj.get("run_id", str(p))
            if run_id not in seen:
                seen.add(run_id)
                summaries.append(obj)
    return summaries


def _load_latest_bt_summary(log_dir: Path) -> Dict[str, Any]:
    p = log_dir / "bt_summary_latest.json"
    if p.exists():
        return _read_json(p)
    # fallback: most recent bt_summary file by mtime
    latest = _latest(log_dir, "bt_summary_bt_*.json")
    return _read_json(latest) if latest else {}


def _load_latest_json(log_dir: Path, pattern: str) -> Dict[str, Any]:
    p = _latest(log_dir, pattern)
    return _read_json(p) if p else {}


# ─────────────────────────────────────────────────────────────────────────────
# Data computation helpers
# ─────────────────────────────────────────────────────────────────────────────

def _compute_equity_series(equity_rows: List[Dict[str, str]]) -> Tuple[List[int], List[float], List[float]]:
    """
    Returns (epochs, equity_values, drawdown_values).
    Drawdown is the running underwater depth (0 or negative).
    """
    epochs:    List[int]   = []
    equities:  List[float] = []
    drawdowns: List[float] = []
    peak = -math.inf
    for row in equity_rows:
        try:
            epoch = int(row.get("epoch", 0))
            eq    = _safe_float(row.get("equity_usd"))
        except Exception:
            continue
        epochs.append(epoch)
        equities.append(eq)
        if eq > peak:
            peak = eq
        drawdowns.append(eq - peak)  # 0 or negative
    return epochs, equities, drawdowns


def _compute_daily_weekly(trade_rows: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Group trade PnL by calendar day (UTC) and week (ISO week).
    Returns daily_pnl: {date_str: {pnl, count}} and
            weekly_pnl: {week_str: {pnl, count}}.
    """
    daily:  Dict[str, Dict[str, Any]] = defaultdict(lambda: {"pnl": 0.0, "count": 0})
    weekly: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"pnl": 0.0, "count": 0})

    for row in trade_rows:
        raw_ts = row.get("exit_time", "") or row.get("entry_time", "")
        pnl    = _safe_float(row.get("pnl"))
        try:
            ts = raw_ts.strip()
            if ts.endswith("Z"):
                ts = ts[:-1] + "+00:00"
            dt = datetime.fromisoformat(ts).astimezone(timezone.utc)
            day_key  = dt.strftime("%Y-%m-%d")
            week_key = dt.strftime("%G-W%V")  # ISO week
            daily[day_key]["pnl"]   += pnl
            daily[day_key]["count"] += 1
            weekly[week_key]["pnl"]   += pnl
            weekly[week_key]["count"] += 1
        except Exception:
            pass

    return {
        "daily":  {k: {"pnl": round(v["pnl"], 8), "count": v["count"]}
                   for k, v in sorted(daily.items())},
        "weekly": {k: {"pnl": round(v["pnl"], 8), "count": v["count"]}
                   for k, v in sorted(weekly.items())},
    }


def _mae_mfe_points(trade_rows: List[Dict[str, str]]) -> List[Dict[str, float]]:
    """
    Return [{mae, mfe, pnl}] for MAE/MFE scatter plot.
    Filters rows with valid numeric values only.
    """
    points = []
    for row in trade_rows:
        try:
            mae = _safe_float(row.get("mae_pct"), default=None)   # type: ignore
            mfe = _safe_float(row.get("mfe_pct"), default=None)   # type: ignore
            pnl = _safe_float(row.get("pnl"),     default=None)   # type: ignore
            mae_raw = row.get("mae_pct", "").strip()
            mfe_raw = row.get("mfe_pct", "").strip()
            if mae_raw and mfe_raw:
                points.append({"mae": round(float(mae_raw), 6),
                                "mfe": round(float(mfe_raw), 6),
                                "pnl": round(_safe_float(row.get("pnl")), 6)})
        except Exception:
            pass
    return points


def _attribution_bar_data(attribution: Dict[str, Any], dimension: str) -> Dict[str, Any]:
    """Extract sorted {label: total_pnl} for a bar chart from attribution JSON."""
    section = attribution.get(dimension, {})
    buckets = section.get("buckets", {})
    return {
        "labels": list(buckets.keys()),
        "pnl":    [b.get("total_pnl") or 0.0 for b in buckets.values()],
        "count":  [b.get("count") or 0        for b in buckets.values()],
        "win_rate": [round((b.get("win_rate") or 0.0) * 100, 1) for b in buckets.values()],
    }


def _reconciliation_status(summaries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Extract reconciliation status from bt_summary list.
    Returns list of {run_id, ok, trades_closed, pnl_usd, attempt_invariants_ok}.
    """
    results = []
    for s in summaries[-20:]:   # last 20 runs
        results.append({
            "run_id":                s.get("run_id", "?"),
            "trades_closed":         s.get("trades_closed", 0),
            "pnl_usd":               _safe_float(s.get("pnl_usd", "0")),
            "win_rate_pct":          _safe_float(s.get("win_rate_pct", "0")),
            "total_return_pct":      _safe_float(s.get("total_return_pct", "0")),
            "max_drawdown_pct":      _safe_float(s.get("max_drawdown_pct", "0")),
            "attempt_invariants_ok": bool(s.get("attempt_invariants_ok", False)),
            "start_equity":          _safe_float(s.get("start_equity", "0")),
            "end_equity":            _safe_float(s.get("end_equity", "0")),
        })
    return results


# ─────────────────────────────────────────────────────────────────────────────
# HTML building blocks
# ─────────────────────────────────────────────────────────────────────────────

_CSS = """
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Courier New', Courier, monospace;
  background: #0d0d0d;
  color: #c8c8c8;
  font-size: 13px;
  line-height: 1.5;
}
.page { max-width: 1400px; margin: 0 auto; padding: 20px 24px 60px; }
h1 { color: #7ec8e3; font-size: 1.6em; margin-bottom: 4px; }
h2 { color: #a8d8a8; font-size: 1.1em; border-bottom: 1px solid #2a2a2a;
     padding-bottom: 4px; margin: 28px 0 10px; }
h3 { color: #c8a8d8; font-size: 0.95em; margin: 16px 0 6px; }
.sub  { color: #666; font-size: 0.78em; margin-bottom: 16px; }
.warn { color: #e8c07a; background: #1e1800; padding: 7px 12px;
        border-left: 3px solid #c09040; margin: 8px 0; font-size: 0.84em; }
.ok   { color: #8dc888; }
.bad  { color: #e86060; }
.na   { color: #555; font-style: italic; }

/* Summary cards */
.cards { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; }
.card {
  background: #141414;
  border: 1px solid #2a2a2a;
  border-radius: 4px;
  padding: 10px 18px;
  min-width: 140px;
}
.card-label { font-size: 0.75em; color: #666; text-transform: uppercase; margin-bottom: 2px; }
.card-value { font-size: 1.25em; font-weight: bold; color: #e8e8e8; }
.card-value.ok  { color: #8dc888; }
.card-value.bad { color: #e86060; }

/* Charts */
.chart-wrap { background: #111; border: 1px solid #222; border-radius: 4px;
              padding: 12px; margin: 8px 0; }
canvas { display: block; }

/* Tables */
.tbl-wrap { overflow-x: auto; margin: 8px 0; }
table { border-collapse: collapse; width: 100%; min-width: 500px; }
th {
  background: #1a2a1a; color: #a8d8a8;
  padding: 6px 10px; text-align: left;
  border: 1px solid #2d3a2d; font-size: 0.82em;
  cursor: pointer; user-select: none;
  white-space: nowrap;
}
th:hover { background: #243a24; }
td { padding: 4px 10px; border: 1px solid #1e1e1e; font-size: 0.82em; white-space: nowrap; }
tr:nth-child(even) { background: #111; }
tr:hover { background: #1a1a1a; }
.sort-asc::after  { content: ' ▲'; color: #7ec8e3; }
.sort-desc::after { content: ' ▼'; color: #7ec8e3; }

/* Two-column layout for some sections */
.two-col { display: flex; gap: 20px; flex-wrap: wrap; }
.two-col > div { flex: 1; min-width: 300px; }

/* Friction panel */
.fric-row { display: flex; gap: 12px; flex-wrap: wrap; margin: 6px 0; }
.fric-item { background: #141414; border: 1px solid #222; border-radius: 3px;
             padding: 6px 14px; font-size: 0.84em; }
.fric-key { color: #666; }
.fric-val { color: #e8e8e8; font-weight: bold; }

hr { border: none; border-top: 1px solid #1e1e1e; margin: 20px 0; }
.note { color: #555; font-size: 0.8em; margin-top: 4px; }
.tag-pass { background: #1a3a1a; color: #8dc888; padding: 2px 7px;
            border-radius: 3px; font-size: 0.8em; }
.tag-fail { background: #3a1a1a; color: #e86060; padding: 2px 7px;
            border-radius: 3px; font-size: 0.8em; }
.section-header { display: flex; align-items: center; gap: 12px; }
</style>
"""

# Inline JS chart renderer — Canvas 2D API, no external libs required.
_CHART_JS = r"""
<script>
"use strict";
// ── ArgusChart ─────────────────────────────────────────────────────────────
// Minimal Canvas 2D chart renderer. Handles: line, bar, scatter.
// All colours, padding, and fonts are hardcoded to the dark theme.

const AC = {};

AC.COLORS = {
  grid:       '#1e1e1e',
  axis:       '#333',
  text:       '#666',
  series: ['#7ec8e3','#a8d8a8','#e8c07a','#c8a8d8','#e86060','#f0b060'],
  pos:        '#8dc888',
  neg:        '#e86060',
  zero:       '#444',
};

function _setFont(ctx, size) {
  ctx.font = `${size}px "Courier New", Courier, monospace`;
  ctx.fillStyle = AC.COLORS.text;
}

function _drawGrid(ctx, l, t, w, h, nX, nY) {
  ctx.strokeStyle = AC.COLORS.grid;
  ctx.lineWidth = 0.5;
  for (let i = 0; i <= nY; i++) {
    const y = t + (h / nY) * i;
    ctx.beginPath(); ctx.moveTo(l, y); ctx.lineTo(l + w, y); ctx.stroke();
  }
  for (let i = 0; i <= nX; i++) {
    const x = l + (w / nX) * i;
    ctx.beginPath(); ctx.moveTo(x, t); ctx.lineTo(x, t + h); ctx.stroke();
  }
}

function _mapX(v, min, max, l, w) {
  return max === min ? l + w / 2 : l + ((v - min) / (max - min)) * w;
}
function _mapY(v, min, max, t, h) {
  return max === min ? t + h / 2 : t + h - ((v - min) / (max - min)) * h;
}

function _yLabels(ctx, min, max, l, t, h, nY, fmt) {
  _setFont(ctx, 10);
  ctx.textAlign = 'right';
  for (let i = 0; i <= nY; i++) {
    const v = min + ((max - min) / nY) * (nY - i);
    const y = t + (h / nY) * i;
    ctx.fillText(fmt(v), l - 6, y + 4);
  }
}

function _xLabels(ctx, labels, l, t, w, h, maxLabels) {
  if (!labels || labels.length === 0) return;
  _setFont(ctx, 10);
  ctx.textAlign = 'center';
  const step = Math.max(1, Math.floor(labels.length / maxLabels));
  for (let i = 0; i < labels.length; i += step) {
    const x = l + (w / (labels.length - 1 || 1)) * i;
    ctx.fillText(labels[i], x, t + h + 14);
  }
}

// ── Line chart ──────────────────────────────────────────────────────────────
// opts: { series: [{label, data}], xLabels, title, yFmt, zeroline }
AC.line = function(canvas, opts) {
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const pad = { l: 72, r: 20, t: 28, b: 32 };
  const l = pad.l, t = pad.t, w = W - pad.l - pad.r, h = H - pad.t - pad.b;
  ctx.clearRect(0, 0, W, H);

  const allData = opts.series.flatMap(s => s.data).filter(v => v != null && isFinite(v));
  if (allData.length === 0) {
    _setFont(ctx, 12); ctx.textAlign = 'center';
    ctx.fillStyle = '#555';
    ctx.fillText('No data', W/2, H/2);
    return;
  }
  let dMin = Math.min(...allData), dMax = Math.max(...allData);
  if (dMin === dMax) { dMin -= 1; dMax += 1; }
  const pad5 = (dMax - dMin) * 0.05;
  dMin -= pad5; dMax += pad5;

  // Title
  if (opts.title) {
    _setFont(ctx, 11); ctx.textAlign = 'left'; ctx.fillStyle = '#888';
    ctx.fillText(opts.title, l, 18);
  }

  _drawGrid(ctx, l, t, w, h, 8, 5);

  const fmt = opts.yFmt || (v => v.toFixed(2));
  _yLabels(ctx, dMin, dMax, l, t, h, 5, fmt);

  const nPts = opts.series[0].data.length;
  _xLabels(ctx, opts.xLabels, l, t + h, w, h, 8);

  // Zero line
  if (opts.zeroline !== false) {
    const zy = _mapY(0, dMin, dMax, t, h);
    if (zy >= t && zy <= t + h) {
      ctx.strokeStyle = AC.COLORS.zero;
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(l, zy); ctx.lineTo(l + w, zy); ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  // Series
  opts.series.forEach((s, si) => {
    const col = s.color || AC.COLORS.series[si % AC.COLORS.series.length];
    ctx.strokeStyle = col;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    let started = false;
    s.data.forEach((v, i) => {
      if (v == null || !isFinite(v)) { started = false; return; }
      const x = _mapX(i, 0, nPts - 1, l, w);
      const y = _mapY(v, dMin, dMax, t, h);
      if (!started) { ctx.moveTo(x, y); started = true; }
      else ctx.lineTo(x, y);
    });
    ctx.stroke();

    // Fill under area for first series (equity)
    if (si === 0 && opts.fillArea) {
      ctx.globalAlpha = 0.08;
      ctx.fillStyle = col;
      // Close path to baseline
      const zy = _mapY(0, dMin, dMax, t, h);
      const zyClamped = Math.min(Math.max(zy, t), t + h);
      // Rebuild path with fill
      ctx.beginPath();
      let firstX = l, firstSet = false;
      s.data.forEach((v, i) => {
        if (v == null || !isFinite(v)) return;
        const x = _mapX(i, 0, nPts - 1, l, w);
        const y = _mapY(v, dMin, dMax, t, h);
        if (!firstSet) { ctx.moveTo(x, zyClamped); ctx.lineTo(x, y); firstX = x; firstSet = true; }
        else ctx.lineTo(x, y);
      });
      ctx.lineTo(_mapX(nPts-1, 0, nPts - 1, l, w), zyClamped);
      ctx.closePath();
      ctx.fill();
      ctx.globalAlpha = 1.0;
    }
  });

  // Legend
  if (opts.series.length > 1) {
    let lx = l + 8;
    opts.series.forEach((s, si) => {
      const col = s.color || AC.COLORS.series[si % AC.COLORS.series.length];
      ctx.fillStyle = col;
      ctx.fillRect(lx, t + 6, 18, 3);
      _setFont(ctx, 10); ctx.textAlign = 'left'; ctx.fillStyle = '#888';
      ctx.fillText(s.label || '', lx + 22, t + 10);
      lx += 80;
    });
  }
};

// ── Bar chart ──────────────────────────────────────────────────────────────
// opts: { labels, values, title, yFmt, colorFn }
AC.bar = function(canvas, opts) {
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const pad = { l: 72, r: 20, t: 28, b: 48 };
  const l = pad.l, t = pad.t, w = W - pad.l - pad.r, h = H - pad.t - pad.b;
  ctx.clearRect(0, 0, W, H);

  const vals = opts.values;
  if (!vals || vals.length === 0) {
    _setFont(ctx, 12); ctx.textAlign = 'center';
    ctx.fillStyle = '#555'; ctx.fillText('No data', W/2, H/2); return;
  }
  let dMin = Math.min(0, ...vals), dMax = Math.max(0, ...vals);
  if (dMin === dMax) { dMax = dMin + 1; }
  const pad5 = (dMax - dMin) * 0.1;
  dMin -= pad5; dMax += pad5;

  if (opts.title) {
    _setFont(ctx, 11); ctx.textAlign = 'left'; ctx.fillStyle = '#888';
    ctx.fillText(opts.title, l, 18);
  }

  _drawGrid(ctx, l, t, w, h, vals.length, 5);
  const fmt = opts.yFmt || (v => v.toFixed(2));
  _yLabels(ctx, dMin, dMax, l, t, h, 5, fmt);

  const zy = _mapY(0, dMin, dMax, t, h);
  ctx.strokeStyle = AC.COLORS.zero; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(l, zy); ctx.lineTo(l + w, zy); ctx.stroke();

  const bw = (w / vals.length) * 0.7;
  const gap = w / vals.length;
  vals.forEach((v, i) => {
    const x = l + gap * i + (gap - bw) / 2;
    const y = _mapY(v, dMin, dMax, t, h);
    const barH = Math.abs(y - zy);
    const barY = v >= 0 ? y : zy;
    const col = opts.colorFn ? opts.colorFn(v, i)
               : (v >= 0 ? AC.COLORS.pos : AC.COLORS.neg);
    ctx.fillStyle = col;
    ctx.fillRect(x, barY, bw, barH);
    // Label
    _setFont(ctx, 10); ctx.textAlign = 'center'; ctx.fillStyle = '#888';
    const lbl = opts.labels[i] || '';
    const maxLblW = gap - 4;
    ctx.save();
    ctx.translate(x + bw/2, t + h + 14);
    if (lbl.length * 7 > maxLblW) {
      ctx.rotate(-Math.PI / 4);
      ctx.textAlign = 'right';
    }
    ctx.fillText(lbl, 0, 0);
    ctx.restore();
  });
};

// ── Scatter chart ──────────────────────────────────────────────────────────
// opts: { points: [{x,y,color?}], title, xLabel, yLabel, xFmt, yFmt }
AC.scatter = function(canvas, opts) {
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const pad = { l: 72, r: 20, t: 28, b: 36 };
  const l = pad.l, t = pad.t, w = W - pad.l - pad.r, h = H - pad.t - pad.b;
  ctx.clearRect(0, 0, W, H);

  const pts = opts.points || [];
  if (pts.length === 0) {
    _setFont(ctx, 12); ctx.textAlign = 'center';
    ctx.fillStyle = '#555'; ctx.fillText('No data', W/2, H/2); return;
  }

  const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
  let xMin = Math.min(...xs), xMax = Math.max(...xs);
  let yMin = Math.min(0, ...ys), yMax = Math.max(0, ...ys);
  if (xMin === xMax) { xMin -= 1; xMax += 1; }
  if (yMin === yMax) { yMin -= 1; yMax += 1; }
  const xPad = (xMax - xMin) * 0.1, yPad = (yMax - yMin) * 0.1;
  xMin -= xPad; xMax += xPad; yMin -= yPad; yMax += yPad;

  if (opts.title) {
    _setFont(ctx, 11); ctx.textAlign = 'left'; ctx.fillStyle = '#888';
    ctx.fillText(opts.title, l, 18);
  }
  _drawGrid(ctx, l, t, w, h, 6, 5);
  const xFmt = opts.xFmt || (v => v.toFixed(2));
  const yFmt = opts.yFmt || (v => v.toFixed(2));
  _yLabels(ctx, yMin, yMax, l, t, h, 5, yFmt);

  // X axis labels
  _setFont(ctx, 10); ctx.textAlign = 'center';
  for (let i = 0; i <= 6; i++) {
    const v = xMin + (xMax - xMin) / 6 * i;
    const x = _mapX(v, xMin, xMax, l, w);
    ctx.fillStyle = AC.COLORS.text;
    ctx.fillText(xFmt(v), x, t + h + 14);
  }

  // Zero lines
  const zy = _mapY(0, yMin, yMax, t, h);
  const zx = _mapX(0, xMin, xMax, l, w);
  ctx.strokeStyle = AC.COLORS.zero; ctx.lineWidth = 1;
  ctx.setLineDash([4,4]);
  if (zy >= t && zy <= t + h) {
    ctx.beginPath(); ctx.moveTo(l, zy); ctx.lineTo(l+w, zy); ctx.stroke();
  }
  if (zx >= l && zx <= l + w) {
    ctx.beginPath(); ctx.moveTo(zx, t); ctx.lineTo(zx, t+h); ctx.stroke();
  }
  ctx.setLineDash([]);

  // Axis labels
  if (opts.xLabel) {
    _setFont(ctx, 10); ctx.textAlign = 'center'; ctx.fillStyle = '#555';
    ctx.fillText(opts.xLabel, l + w/2, t + h + 28);
  }
  if (opts.yLabel) {
    ctx.save(); ctx.translate(14, t + h/2);
    ctx.rotate(-Math.PI/2); _setFont(ctx, 10); ctx.textAlign = 'center';
    ctx.fillStyle = '#555'; ctx.fillText(opts.yLabel, 0, 0);
    ctx.restore();
  }

  // Points
  pts.forEach(p => {
    const x = _mapX(p.x, xMin, xMax, l, w);
    const y = _mapY(p.y, yMin, yMax, t, h);
    ctx.beginPath();
    ctx.arc(x, y, 4, 0, Math.PI * 2);
    ctx.fillStyle = p.color || (p.y >= 0 ? AC.COLORS.pos : AC.COLORS.neg);
    ctx.globalAlpha = 0.75;
    ctx.fill();
    ctx.globalAlpha = 1.0;
  });
};

// ── Table sorter ───────────────────────────────────────────────────────────
function initSortableTable(tableId) {
  const tbl = document.getElementById(tableId);
  if (!tbl) return;
  const ths = tbl.querySelectorAll('th');
  let sortCol = -1, sortDir = 1;
  ths.forEach((th, ci) => {
    th.addEventListener('click', () => {
      const tbody = tbl.querySelector('tbody');
      const rows  = Array.from(tbody.querySelectorAll('tr'));
      if (sortCol === ci) sortDir = -sortDir;
      else { sortCol = ci; sortDir = 1; }
      ths.forEach(h => h.className = '');
      th.className = sortDir === 1 ? 'sort-asc' : 'sort-desc';
      rows.sort((a, b) => {
        const av = a.cells[ci]?.textContent?.trim() || '';
        const bv = b.cells[ci]?.textContent?.trim() || '';
        const an = parseFloat(av), bn = parseFloat(bv);
        if (!isNaN(an) && !isNaN(bn)) return (an - bn) * sortDir;
        return av.localeCompare(bv) * sortDir;
      });
      rows.forEach(r => tbody.appendChild(r));
    });
  });
}
</script>
"""


# ─────────────────────────────────────────────────────────────────────────────
# HTML section builders
# ─────────────────────────────────────────────────────────────────────────────

def _card(label: str, value: str, cls: str = "") -> str:
    cls_str = f' class="card-value {cls}"' if cls else ' class="card-value"'
    return (
        f'<div class="card">'
        f'<div class="card-label">{label}</div>'
        f'<div{cls_str}>{value}</div>'
        f'</div>'
    )


def _summary_cards(latest: Dict[str, Any]) -> str:
    pnl      = _safe_float(latest.get("pnl_usd",          "0"))
    ret      = _safe_float(latest.get("total_return_pct",  "0"))
    trades   = latest.get("trades_closed", 0)
    win_rate = _safe_float(latest.get("win_rate_pct",      "0"))
    dd       = _safe_float(latest.get("max_drawdown_pct",  "0"))
    start_eq = _safe_float(latest.get("start_equity",      "0"))
    end_eq   = _safe_float(latest.get("end_equity",        "0"))
    run_id   = latest.get("run_id", "n/a")

    pnl_cls  = "ok" if pnl >= 0 else "bad"
    ret_cls  = "ok" if ret >= 0 else "bad"

    return (
        f'<div class="cards">'
        + _card("Latest Run", run_id[:30] if run_id else "n/a")
        + _card("Net PnL", f"${pnl:+.4f}", pnl_cls)
        + _card("Return", f"{ret:+.4f}%", ret_cls)
        + _card("Trades Closed", str(trades))
        + _card("Win Rate", f"{win_rate:.1f}%")
        + _card("Max Drawdown", f"{dd:.4f}%")
        + _card("Start Equity", f"${start_eq:,.2f}")
        + _card("End Equity", f"${end_eq:,.2f}")
        + '</div>'
    )


def _equity_chart_section(equity_rows: List[Dict[str, str]]) -> str:
    if not equity_rows:
        return '<p class="na">No equity_*.csv data found.</p>'

    epochs, equities, drawdowns = _compute_equity_series(equity_rows)

    # Downsample to max 800 points for chart performance
    step = max(1, len(equities) // 800)
    eq_ds  = equities[::step]
    dd_ds  = drawdowns[::step]
    ep_ds  = epochs[::step]

    # Format epoch labels as HH:MM
    def _epoch_label(e: int) -> str:
        try:
            return datetime.fromtimestamp(e, tz=timezone.utc).strftime("%m/%d %H:%M")
        except Exception:
            return str(e)

    x_labels = [_epoch_label(e) for e in ep_ds]

    eq_json  = json.dumps(eq_ds)
    dd_json  = json.dumps(dd_ds)
    xl_json  = json.dumps(x_labels)

    return f"""
<div class="chart-wrap">
  <canvas id="cvEq" width="1100" height="200"></canvas>
</div>
<div class="chart-wrap">
  <canvas id="cvDd" width="1100" height="140"></canvas>
</div>
<script>
(function(){{
  const eq = {eq_json};
  const dd = {dd_json};
  const xl = {xl_json};
  const eqStart = eq.length > 0 ? eq[0] : 0;
  const eqRel   = eq.map(v => v - eqStart);
  AC.line(document.getElementById('cvEq'), {{
    series: [{{label: 'Equity (USD)', data: eq, color: '#7ec8e3'}}],
    xLabels: xl,
    title: 'Equity Curve (USD)',
    yFmt: v => '$' + v.toFixed(2),
    fillArea: true,
  }});
  AC.line(document.getElementById('cvDd'), {{
    series: [{{label: 'Drawdown', data: dd, color: '#e86060'}}],
    xLabels: xl,
    title: 'Drawdown (USD)',
    yFmt: v => '$' + v.toFixed(2),
    fillArea: false,
  }});
}})();
</script>
"""


def _trade_table_section(trade_rows: List[Dict[str, str]]) -> str:
    if not trade_rows:
        return '<p class="na">No trade_journal_*.csv rows found.</p>'

    cols = [
        ("trade_id",        "Trade ID"),
        ("entry_time",      "Entry Time"),
        ("exit_time",       "Exit Time"),
        ("entry_px",        "Entry Px"),
        ("exit_px",         "Exit Px"),
        ("qty",             "Qty"),
        ("pnl",             "PnL"),
        ("regime_at_entry", "Regime"),
        ("entry_reason",    "Entry Reason"),
        ("trade_duration_s","Duration (s)"),
        ("mae_pct",         "MAE%"),
        ("mfe_pct",         "MFE%"),
        ("slippage_bps",    "Slip bps"),
        ("total_fee",       "Total Fee"),
    ]

    thead = "".join(f"<th>{label}</th>" for _, label in cols)
    tbody = ""
    for row in reversed(trade_rows):   # newest first
        cells = ""
        for key, _ in cols:
            v = row.get(key, "")
            if key == "pnl":
                try:
                    fv = float(v)
                    cls = "ok" if fv >= 0 else "bad"
                    cells += f'<td><span class="{cls}">{fv:+.6f}</span></td>'
                except Exception:
                    cells += f"<td>{v}</td>"
            else:
                cells += f"<td>{v}</td>"
        tbody += f"<tr>{cells}</tr>\n"

    return f"""
<div class="tbl-wrap">
<table id="tblTrades">
  <thead><tr>{thead}</tr></thead>
  <tbody>{tbody}</tbody>
</table>
</div>
<script>initSortableTable('tblTrades');</script>
"""


def _attribution_section(attribution: Dict[str, Any]) -> str:
    if not attribution:
        return '<p class="na">No attribution_*.json found. Run: python -m analytics.attribution</p>'

    dq = attribution.get("data_quality", {})
    warns = dq.get("warnings", [])
    warn_html = "".join(f'<div class="warn">⚠ {w}</div>' for w in warns)

    regime_data  = _attribution_bar_data(attribution, "regime_attribution")
    session_data = _attribution_bar_data(attribution, "session_attribution")
    reason_data  = _attribution_bar_data(attribution, "entry_reason_attribution")

    rl_json = json.dumps(regime_data["labels"])
    rv_json = json.dumps(regime_data["pnl"])
    rc_json = json.dumps(regime_data["count"])

    sl_json = json.dumps(session_data["labels"])
    sv_json = json.dumps(session_data["pnl"])
    sc_json = json.dumps(session_data["count"])

    rl2_json = json.dumps(reason_data["labels"])
    rv2_json = json.dumps(reason_data["pnl"])

    # Fee drag summary
    fd = attribution.get("fee_drag_analysis", {})
    fee_total = fd.get("total_fees", 0.0)
    fee_drag  = fd.get("fee_as_pct_gross_wins")
    net_pnl   = fd.get("net_pnl", 0.0)

    # MAE/MFE note
    mf_note = attribution.get("mae_mfe_analysis", {}).get("exit_timing_note", "")

    return f"""
{warn_html}
<p class="note">Trades analysed: {dq.get('n_trades', 0)} |
  Sufficient data: {'<span class="ok">YES</span>' if dq.get('sufficient_data') else '<span class="bad">NO</span>'}
  (need {dq.get('minimum_required', 20)}+)
</p>

<div class="two-col">
<div>
<h3>Regime Attribution (PnL)</h3>
<div class="chart-wrap">
  <canvas id="cvReg" width="520" height="220"></canvas>
</div>
</div>
<div>
<h3>Session Attribution (PnL)</h3>
<div class="chart-wrap">
  <canvas id="cvSes" width="520" height="220"></canvas>
</div>
</div>
</div>

<h3>Entry Reason Attribution (PnL)</h3>
<div class="chart-wrap">
  <canvas id="cvRsn" width="1100" height="180"></canvas>
</div>

<div class="two-col">
<div>
<p class="note">Fee drag: total fees <b>${fee_total:.8f}</b> |
  Fee as % of gross wins: <b>{'n/a' if fee_drag is None else f'{fee_drag:.2f}%'}</b> |
  Net PnL: <b>{net_pnl:+.8f}</b></p>
<p class="note">Exit timing: {mf_note or 'n/a'}</p>
</div>
</div>

<script>
(function(){{
  AC.bar(document.getElementById('cvReg'), {{
    labels: {rl_json}, values: {rv_json}, title: 'Regime — Total PnL'
  }});
  AC.bar(document.getElementById('cvSes'), {{
    labels: {sl_json}, values: {sv_json}, title: 'Session — Total PnL'
  }});
  AC.bar(document.getElementById('cvRsn'), {{
    labels: {rl2_json}, values: {rv2_json}, title: 'Entry Reason — Total PnL'
  }});
}})();
</script>
"""


def _risk_section(risk_model: Dict[str, Any]) -> str:
    if not risk_model:
        return '<p class="na">No risk_model_*.json found. Run: python -m analytics.risk_model</p>'

    dq   = risk_model.get("data_quality", {})
    bs   = risk_model.get("basic_stats", {})
    kelly = risk_model.get("kelly_fraction")
    mc   = risk_model.get("monte_carlo", {})
    hdd  = risk_model.get("historical_drawdown", {})
    mcl  = risk_model.get("max_consecutive_losses", 0)

    warns = dq.get("warnings", [])
    warn_html = "".join(f'<div class="warn">⚠ {w}</div>' for w in warns)

    ror  = mc.get("risk_of_ruin")
    ror_str = f"{ror:.4%}" if ror is not None else "n/a"
    ror_cls = "bad" if (ror or 0) > 0.05 else "ok"

    kelly_str = f"{kelly:.4f}" if kelly is not None else "n/a"
    kelly_cls = "ok" if kelly is not None and kelly > 0 else "bad"

    # MC equity path percentiles for chart
    mc_paths = mc.get("equity_path_percentiles", [])
    mc_steps = [p["step"] for p in mc_paths]
    mc_p5    = [p.get("p5")  for p in mc_paths]
    mc_p50   = [p.get("p50") for p in mc_paths]
    mc_p95   = [p.get("p95") for p in mc_paths]

    have_mc = len(mc_paths) > 1

    mc_section = ""
    if have_mc:
        mc_section = f"""
<h3>Monte Carlo Equity Paths (10k paths, {mc.get('n_steps',0)} steps)</h3>
<div class="chart-wrap">
  <canvas id="cvMC" width="1100" height="200"></canvas>
</div>
<script>
(function(){{
  const p5  = {json.dumps(mc_p5)};
  const p50 = {json.dumps(mc_p50)};
  const p95 = {json.dumps(mc_p95)};
  AC.line(document.getElementById('cvMC'), {{
    series: [
      {{label:'p5',  data:p5,  color:'#e86060'}},
      {{label:'p50', data:p50, color:'#7ec8e3'}},
      {{label:'p95', data:p95, color:'#8dc888'}},
    ],
    title: 'Monte Carlo Equity Paths — p5 / p50 / p95',
    yFmt: v => '$' + v.toFixed(2),
  }});
}})();
</script>
"""

    # Risk sensitivity table
    sensitivity = risk_model.get("risk_sensitivity", [])
    sens_rows = ""
    for row in sensitivity:
        rf   = row.get("risk_fraction", 0)
        usd  = row.get("risk_usd_at_start", 0)
        pok  = row.get("pct_of_kelly")
        at_k = row.get("at_or_above_kelly", False)
        cls  = "bad" if at_k else ""
        pok_str = f"{pok:.4f}x" if pok is not None else "n/a"
        sens_rows += (
            f'<tr class="{cls}">'
            f'<td>{rf:.1%}</td>'
            f'<td>${usd:.2f}</td>'
            f'<td>{pok_str}</td>'
            f'<td>{"<span class=\'bad\'>YES</span>" if at_k else "no"}</td>'
            f'</tr>'
        )

    return f"""
{warn_html}
<p class="note">Trades: {dq.get('n_trades',0)} | Sufficient: {'<span class="ok">YES</span>' if dq.get('sufficient_data') else '<span class="bad">NO</span>'} (need {dq.get('minimum_required',50)}+)</p>

<div class="cards">
  {_card("Win Rate",    f"{(bs.get('win_rate') or 0)*100:.1f}%")}
  {_card("Expectancy",  _fmt_usd(bs.get('expectancy'), 6))}
  {_card("Profit Factor", _fmt_num(bs.get('profit_factor')))}
  {_card("Sharpe",      _fmt_num(bs.get('sharpe_per_trade')))}
  {_card("Kelly Frac",  kelly_str, kelly_cls)}
  {_card("Risk of Ruin", ror_str, ror_cls)}
  {_card("Max Consec L", str(mcl))}
  {_card("Hist MaxDD",  _fmt_usd(hdd.get('max_drawdown_abs')))}
</div>

{mc_section}

<h3>Risk-per-Trade Sensitivity</h3>
<div class="tbl-wrap">
<table id="tblRisk">
  <thead><tr>
    <th>Risk Frac</th><th>$ at Risk (start)</th><th>% of Kelly</th><th>≥ Full Kelly</th>
  </tr></thead>
  <tbody>{sens_rows}</tbody>
</table>
</div>
"""


def _mae_mfe_section(trade_rows: List[Dict[str, str]]) -> str:
    points = _mae_mfe_points(trade_rows)
    if not points:
        return '<p class="na">No MAE/MFE data in trade journals.</p>'

    scatter_pts = [
        {"x": p["mae"], "y": p["mfe"],
         "color": "#8dc888" if p["pnl"] >= 0 else "#e86060"}
        for p in points
    ]

    pts_json = json.dumps(scatter_pts)

    return f"""
<p class="note">Each point = one trade. x=MAE%, y=MFE%. Green=win, red=loss.</p>
<div class="chart-wrap">
  <canvas id="cvMaeMfe" width="700" height="350"></canvas>
</div>
<script>
(function(){{
  AC.scatter(document.getElementById('cvMaeMfe'), {{
    points: {pts_json},
    title: 'MAE vs MFE (% of entry price)',
    xLabel: 'MAE % (negative = adverse)',
    yLabel: 'MFE %',
    xFmt: v => v.toFixed(3) + '%',
    yFmt: v => v.toFixed(3) + '%',
  }});
}})();
</script>
"""


def _friction_section(friction: Dict[str, Any]) -> str:
    if not friction:
        return '<p class="na">No friction_report_*.json found. Run: python -m analytics.friction_report</p>'

    dq     = friction.get("data_quality", {})
    warns  = dq.get("warnings", [])
    warn_html = "".join(f'<div class="warn">⚠ {w}</div>' for w in warns)

    lat = friction.get("latency_ms", {})
    slip = friction.get("slippage_bps", {})
    cfg_slip = friction.get("configured_slippage_bps", {})

    def _fric_item(key: str, val: Any) -> str:
        return (
            f'<div class="fric-item">'
            f'<span class="fric-key">{key}:</span> '
            f'<span class="fric-val">{_fmt_num(val, 2) if val is not None else "n/a"}</span>'
            f'</div>'
        )

    lat_html = "".join([
        _fric_item("Latency p50 ms", lat.get("p50")),
        _fric_item("Latency p95 ms", lat.get("p95")),
        _fric_item("Latency p99 ms", lat.get("p99")),
    ])
    slip_html = "".join([
        _fric_item("Slip p50 bps",     slip.get("p50")),
        _fric_item("Slip p95 bps",     slip.get("p95")),
        _fric_item("Cfg slip p50 bps", cfg_slip.get("p50")),
        _fric_item("Cfg slip p95 bps", cfg_slip.get("p95")),
    ])

    sample_sizes = friction.get("sample_sizes", {})
    n_fills = sample_sizes.get("fills", 0)
    n_trades = sample_sizes.get("trades", 0)

    return f"""
{warn_html}
<p class="note">Fills: {n_fills} | Trades: {n_trades} | Sufficient: {'<span class="ok">YES</span>' if dq.get('sufficient_data') else '<span class="bad">NO</span>'} (need 30+)</p>
<h3>Order-to-Fill Latency</h3>
<div class="fric-row">{lat_html}</div>
<h3>Slippage (fill vs mid-price)</h3>
<div class="fric-row">{slip_html}</div>
"""


def _reconciliation_section(
    summaries: List[Dict[str, Any]],
    latest: Dict[str, Any],
) -> str:
    if not summaries:
        return '<p class="na">No bt_summary_*.json found.</p>'

    inv_ok   = latest.get("attempt_invariants_ok", None)
    inv_msg  = latest.get("attempt_invariants_msg", "")
    inv_html = (
        '<span class="tag-pass">PASS</span>'
        if inv_ok else
        '<span class="tag-fail">FAIL</span>'
    )

    rec_rows = _reconciliation_status(summaries)
    tbody = ""
    for r in reversed(rec_rows):
        ok   = r["attempt_invariants_ok"]
        tag  = '<span class="tag-pass">PASS</span>' if ok else '<span class="tag-fail">FAIL</span>'
        pnl  = r["pnl_usd"]
        pcls = "ok" if pnl >= 0 else "bad"
        tbody += (
            f"<tr>"
            f"<td>{r['run_id']}</td>"
            f"<td>{r['trades_closed']}</td>"
            f"<td><span class='{pcls}'>{pnl:+.6f}</span></td>"
            f"<td>{r['win_rate_pct']:.1f}%</td>"
            f"<td>{r['max_drawdown_pct']:.4f}%</td>"
            f"<td>{tag}</td>"
            f"</tr>\n"
        )

    return f"""
<p>Latest run invariants: {inv_html}
  {'— ' + inv_msg if inv_msg else ''}</p>
<div class="tbl-wrap">
<table id="tblRecon">
  <thead><tr>
    <th>Run ID</th><th>Trades</th><th>PnL USD</th>
    <th>Win Rate</th><th>Max DD%</th><th>Invariants</th>
  </tr></thead>
  <tbody>{tbody}</tbody>
</table>
</div>
<script>initSortableTable('tblRecon');</script>
"""


def _daily_weekly_section(trade_rows: List[Dict[str, str]]) -> str:
    if not trade_rows:
        return '<p class="na">No trade data for daily/weekly snapshots.</p>'

    perf = _compute_daily_weekly(trade_rows)
    daily  = perf["daily"]
    weekly = perf["weekly"]

    if not daily:
        return '<p class="na">No timestamped trades for daily/weekly breakdown.</p>'

    # Daily bar chart
    d_labels = list(daily.keys())
    d_values = [daily[k]["pnl"] for k in d_labels]

    # Weekly bar chart
    w_labels = list(weekly.keys())
    w_values = [weekly[k]["pnl"] for k in w_labels]

    dl_json = json.dumps(d_labels)
    dv_json = json.dumps(d_values)
    wl_json = json.dumps(w_labels)
    wv_json = json.dumps(w_values)

    # Daily table
    daily_rows = "".join(
        f'<tr><td>{k}</td>'
        f'<td><span class="{"ok" if daily[k]["pnl"]>=0 else "bad"}">{daily[k]["pnl"]:+.6f}</span></td>'
        f'<td>{daily[k]["count"]}</td></tr>\n'
        for k in sorted(daily.keys(), reverse=True)
    )

    return f"""
<div class="two-col">
<div>
<h3>Daily PnL</h3>
<div class="chart-wrap">
  <canvas id="cvDaily" width="520" height="200"></canvas>
</div>
</div>
<div>
<h3>Weekly PnL</h3>
<div class="chart-wrap">
  <canvas id="cvWeekly" width="520" height="200"></canvas>
</div>
</div>
</div>

<div class="tbl-wrap" style="max-height:220px;overflow-y:auto;">
<table id="tblDaily">
  <thead><tr><th>Date</th><th>PnL (USD)</th><th>Trades</th></tr></thead>
  <tbody>{daily_rows}</tbody>
</table>
</div>
<script>initSortableTable('tblDaily');</script>

<script>
(function(){{
  AC.bar(document.getElementById('cvDaily'), {{
    labels: {dl_json}, values: {dv_json}, title: 'Daily PnL (USD)'
  }});
  AC.bar(document.getElementById('cvWeekly'), {{
    labels: {wl_json}, values: {wv_json}, title: 'Weekly PnL (USD)'
  }});
}})();
</script>
"""


def _friction_vs_baseline_section(
    friction: Dict[str, Any],
    risk_model: Dict[str, Any],
) -> str:
    """
    Compare baseline expectancy (from risk_model) vs
    friction-adjusted expectancy.
    Simple comparison table — no complex math, just display.
    """
    bs = risk_model.get("basic_stats", {}) if risk_model else {}
    exp_base = bs.get("expectancy")

    slip = friction.get("slippage_bps", {}) if friction else {}
    slip_p50 = slip.get("p50")

    if exp_base is None and slip_p50 is None:
        return '<p class="na">Requires both friction_report and risk_model data.</p>'

    rows = ""
    if exp_base is not None:
        pcls = "ok" if exp_base >= 0 else "bad"
        rows += (
            f'<tr><td>Baseline Expectancy (from risk model)</td>'
            f'<td><span class="{pcls}">${exp_base:+.6f}</span></td>'
            f'<td class="na">per trade (live journal)</td></tr>'
        )
    if slip_p50 is not None:
        rows += (
            f'<tr><td>Empirical Slippage p50</td>'
            f'<td>{slip_p50:.2f} bps</td>'
            f'<td class="na">order-to-fill friction</td></tr>'
        )
    lat = friction.get("latency_ms", {}) if friction else {}
    lat_p50 = lat.get("p50")
    if lat_p50 is not None:
        rows += (
            f'<tr><td>Latency p50</td>'
            f'<td>{lat_p50:.1f} ms</td>'
            f'<td class="na">submit → fill roundtrip</td></tr>'
        )

    return f"""
<div class="tbl-wrap">
<table>
  <thead><tr><th>Metric</th><th>Value</th><th>Note</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
</div>
<p class="note">Full friction-adjusted expectancy modeling requires Phase 12 research_report. This panel shows the raw inputs side-by-side.</p>
"""


# ─────────────────────────────────────────────────────────────────────────────
# Report assembly
# ─────────────────────────────────────────────────────────────────────────────

def build_report_html(
    log_dir:    Path,
    generated:  str,
) -> str:
    """
    Load all artifacts from log_dir, build and return the complete HTML string.
    """
    # Load artifacts
    trade_rows  = _load_trade_journals(log_dir)
    equity_rows = _load_equity_csv(log_dir)
    latest_bt   = _load_latest_bt_summary(log_dir)
    all_bt      = _load_all_bt_summaries(log_dir)
    attribution = _load_latest_json(log_dir, "attribution_*.json")
    risk_model  = _load_latest_json(log_dir, "risk_model_*.json")
    friction    = _load_latest_json(log_dir, "friction_report_*.json")

    n_trades  = len(trade_rows)
    n_eq_rows = len(equity_rows)
    n_bt_runs = len(all_bt)

    # Warn if no data at all
    data_warnings = []
    if n_trades == 0:
        data_warnings.append("No trade_journal_*.csv found — trade panels will be empty.")
    if n_eq_rows == 0:
        data_warnings.append("No equity_*.csv found — equity chart will be empty.")
    if not latest_bt:
        data_warnings.append("No bt_summary_latest.json — summary cards will be empty.")
    if not attribution:
        data_warnings.append("No attribution_*.json — run python -m analytics.attribution to populate.")
    if not risk_model:
        data_warnings.append("No risk_model_*.json — run python -m analytics.risk_model to populate.")
    if not friction:
        data_warnings.append("No friction_report_*.json — run python -m analytics.friction_report to populate.")

    warn_banner = "".join(
        f'<div class="warn">⚠ {w}</div>' for w in data_warnings
    )

    # Build each section
    summary_html    = _summary_cards(latest_bt)
    equity_html     = _equity_chart_section(equity_rows)
    trade_html      = _trade_table_section(trade_rows)
    attr_html       = _attribution_section(attribution)
    risk_html       = _risk_section(risk_model)
    mae_mfe_html    = _mae_mfe_section(trade_rows)
    friction_html   = _friction_section(friction)
    recon_html      = _reconciliation_section(all_bt, latest_bt)
    daily_html      = _daily_weekly_section(trade_rows)
    vs_html         = _friction_vs_baseline_section(friction, risk_model)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Argus Performance Report — {generated[:10]}</title>
  {_CSS}
</head>
<body>
{_CHART_JS}
<div class="page">

<h1>Argus Performance Dashboard</h1>
<p class="sub">Generated: {generated} | Log dir: {log_dir}
  | Trade records: {n_trades} | BT runs: {n_bt_runs} | Equity rows: {n_eq_rows}
  | Report version: {REPORT_VERSION}
</p>
{warn_banner}

<h2>Summary — Latest Run</h2>
{summary_html}

<h2>Equity Curve &amp; Drawdown</h2>
{equity_html}

<h2>Trade History</h2>
{trade_html}

<h2>Daily / Weekly Performance</h2>
{daily_html}

<h2>Regime &amp; Session Attribution</h2>
{attr_html}

<h2>MAE / MFE Scatter</h2>
{mae_mfe_html}

<h2>Risk Model Summary</h2>
{risk_html}

<h2>Friction — Empirical Measurements</h2>
{friction_html}

<h2>Friction vs Baseline Expectancy</h2>
{vs_html}

<h2>Reconciliation Status (last {min(20, n_bt_runs)} runs)</h2>
{recon_html}

<hr>
<p class="note">
  Argus Phase 15 — Performance Visibility Dashboard |
  Report regenerable from artifacts alone (canonical truth) |
  No internet connection required
</p>

</div>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────────
# Output writer
# ─────────────────────────────────────────────────────────────────────────────

def write_report(html: str, out_dir: Path, date_str: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"report_{date_str}.html"
    with path.open("w", encoding="utf-8") as f:
        f.write(html)
    return path


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 15 — generate self-contained HTML performance report "
            "from ops/logs artifacts (no internet required)."
        )
    )
    parser.add_argument(
        "--log-dir",
        default="ops/logs",
        help="Directory containing artifact files (default: ops/logs)",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Output directory (default: same as --log-dir)",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Date string for filename (default: current UTC date YYYYMMDD)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the report in a browser after generation",
    )
    args = parser.parse_args(argv)

    log_dir  = Path(args.log_dir).resolve()
    out_dir  = Path(args.out_dir).resolve() if args.out_dir else log_dir
    date_str = args.date or _default_date_str()

    if not log_dir.exists():
        print(f"ERROR: log_dir does not exist: {log_dir}", file=sys.stderr)
        sys.exit(1)

    generated = _utc_now_str()
    print(f"Building report from {log_dir} ...")

    html = build_report_html(log_dir, generated)
    path = write_report(html, out_dir, date_str)

    print(f"  HTML -> {path}  ({len(html)//1024} KB)")

    if not args.no_browser:
        url = path.as_uri()
        print(f"  Opening browser: {url}")
        try:
            webbrowser.open(url)
        except Exception as e:
            print(f"  (browser open failed: {e})", file=sys.stderr)

    print("  Done.")


if __name__ == "__main__":
    main()
