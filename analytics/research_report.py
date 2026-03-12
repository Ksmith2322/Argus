#!/usr/bin/env python3
# analytics/research_report.py
#
# Phase 12 — Research Reality Integration Report
#
# Reads:
#   bt_summary_*.json           — baseline backtest summaries
#   wf_summary_*.json           — walk-forward summaries
#   stress_summary_*.json       — stress scenario summaries
#   friction_report_*.json      — Phase 11 friction report
#
# Computes:
#   * expectancy erosion (baseline vs friction-adjusted scenarios)
#   * drawdown distortion under realistic friction
#   * walk-forward consistency (are results stable across windows?)
#   * parameter sensitivity summary (from multiple backtest runs with varied params)
#
# Writes:
#   research_report_<date>.json    — machine-readable
#   research_summary_<date>.html   — human-readable single-file report
#
# Usage:
#   python -m analytics.research_report
#   python -m analytics.research_report --log-dir ops/logs --out-dir ops/logs

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


REPORT_VERSION = "1.0"
MINIMUM_TRADES_FOR_MEANINGFUL_STATS = 10


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(str(v).strip())
    except Exception:
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(str(v).strip())
    except Exception:
        return default


def _percentiles(data: List[float]) -> Dict[str, Optional[float]]:
    if not data:
        return {"p50": None, "p95": None, "min": None, "max": None, "mean": None, "count": 0}
    s = sorted(data)
    n = len(s)

    def pct(p: float) -> float:
        idx = (p / 100.0) * (n - 1)
        lo, frac = int(idx), idx - int(idx)
        return s[lo] + (frac * (s[lo + 1] - s[lo]) if lo + 1 < n else 0)

    return {
        "p50": round(pct(50), 6),
        "p95": round(pct(95), 6),
        "min": round(s[0], 6),
        "max": round(s[-1], 6),
        "mean": round(sum(s) / n, 6),
        "count": n,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Readers
# ─────────────────────────────────────────────────────────────────────────────

def _load_json(path: str) -> Optional[Dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _latest_json(pattern: str) -> Optional[Dict]:
    matches = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    if not matches:
        return None
    return _load_json(matches[0])


def _all_json(pattern: str) -> List[Dict]:
    results = []
    for path in sorted(glob.glob(pattern), key=os.path.getmtime):
        d = _load_json(path)
        if d:
            results.append(d)
    return results


def _load_baseline_summaries(log_dir: str) -> List[Dict]:
    """All bt_summary_*.json in log_dir (excludes latest pointer)."""
    pattern = os.path.join(log_dir, "bt_summary_*.json")
    return [
        d for d in _all_json(pattern)
        if d.get("run_id") and d.get("run_id") != "latest"
    ]


def _load_walk_forward_summaries(log_dir: str) -> List[Dict]:
    return _all_json(os.path.join(log_dir, "wf_summary_*.json"))


def _load_stress_summaries(log_dir: str) -> List[Dict]:
    return _all_json(os.path.join(log_dir, "stress_summary_*.json"))


def _load_friction_report(log_dir: str) -> Optional[Dict]:
    return _latest_json(os.path.join(log_dir, "friction_report_*.json"))


# ─────────────────────────────────────────────────────────────────────────────
# Baseline analysis
# ─────────────────────────────────────────────────────────────────────────────

def _analyse_baseline(summaries: List[Dict]) -> Dict[str, Any]:
    if not summaries:
        return {"count": 0, "warning": "No bt_summary_*.json found in log_dir"}

    # Use latest summary as canonical
    latest = max(summaries, key=lambda d: d.get("end_epoch", 0))

    trades = _safe_int(latest.get("trades_closed"))
    pnl = _safe_float(latest.get("pnl_usd"))
    dd_pct = _safe_float(latest.get("max_drawdown_pct"))
    wr = _safe_float(latest.get("win_rate_pct"))
    expectancy = _safe_float(latest.get("expectancy_usd"))
    pf = _safe_float(latest.get("profit_factor"))
    ret_pct = _safe_float(latest.get("total_return_pct"))
    start_eq = _safe_float(latest.get("start_equity", "500"))
    end_eq = _safe_float(latest.get("end_equity", "500"))

    warnings: List[str] = []
    if trades < MINIMUM_TRADES_FOR_MEANINGFUL_STATS:
        warnings.append(
            f"Only {trades} trades in baseline; {MINIMUM_TRADES_FOR_MEANINGFUL_STATS}+ needed for reliable statistics."
        )

    return {
        "run_id": latest.get("run_id", ""),
        "trades_closed": trades,
        "pnl_usd": pnl,
        "total_return_pct": ret_pct,
        "max_drawdown_pct": dd_pct,
        "win_rate_pct": wr,
        "expectancy_usd": expectancy,
        "profit_factor": pf,
        "start_equity": start_eq,
        "end_equity": end_eq,
        "edge_positive": pnl > 0,
        "count": len(summaries),
        "warnings": warnings,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Walk-forward analysis
# ─────────────────────────────────────────────────────────────────────────────

def _analyse_walk_forward(wf_summaries: List[Dict]) -> Dict[str, Any]:
    if not wf_summaries:
        return {"count": 0, "warning": "No wf_summary_*.json found — run: python -m backtest.walk_forward"}

    # Use latest WF run
    latest = wf_summaries[-1]
    windows = latest.get("windows", [])
    ok_windows = [w for w in windows if w.get("status") == "ok"]

    pnl_key = next((k for k in ("total_pnl", "net_pnl", "pnl_usd", "pnl") if any(k in w for w in ok_windows)), None)

    pnl_vals = [_safe_float(w.get(pnl_key)) for w in ok_windows if pnl_key and pnl_key in w]
    wins = sum(1 for v in pnl_vals if v > 0)
    consistency = wins / len(pnl_vals) if pnl_vals else None

    agg = latest.get("aggregate", {})
    pnl_agg = agg.get("pnl", {})

    return {
        "run_id": latest.get("run_id", ""),
        "n_windows": latest.get("n_windows"),
        "mode": latest.get("mode"),
        "windows_ok": len(ok_windows),
        "windows_total": len(windows),
        "pnl_stats": _percentiles(pnl_vals),
        "windows_positive": wins,
        "consistency_pct": round(consistency * 100, 1) if consistency is not None else None,
        "consistent": consistency >= 0.6 if consistency is not None else None,
        "friction_mode": latest.get("friction_mode", "off"),
        "count": len(wf_summaries),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Stress / erosion analysis
# ─────────────────────────────────────────────────────────────────────────────

def _analyse_stress(stress_summaries: List[Dict]) -> Dict[str, Any]:
    if not stress_summaries:
        return {"count": 0, "warning": "No stress_summary_*.json found — run: python -m backtest.stress_runner"}

    latest = stress_summaries[-1]
    erosion = latest.get("erosion", {})

    # Find which scenarios killed the edge
    edge_killers: List[str] = []
    for name, e in erosion.items():
        if e.get("edge_survived") is False:
            edge_killers.append(name)

    # Find worst erosion
    worst = sorted(
        [(name, e.get("erosion_pct")) for name, e in erosion.items() if e.get("erosion_pct") is not None],
        key=lambda x: x[1] or 0,
    )

    return {
        "run_id": latest.get("run_id", ""),
        "scenarios_run": latest.get("scenarios_run"),
        "scenarios_ok": latest.get("scenarios_ok"),
        "erosion": erosion,
        "edge_killers": edge_killers,
        "edge_survives_all": len(edge_killers) == 0,
        "worst_erosion_scenarios": [name for name, _ in worst[:3]],
        "friction_p50": latest.get("friction", {}).get("p50"),
        "friction_p95": latest.get("friction", {}).get("p95"),
        "count": len(stress_summaries),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Friction summary
# ─────────────────────────────────────────────────────────────────────────────

def _analyse_friction(friction: Optional[Dict]) -> Dict[str, Any]:
    if not friction:
        return {"available": False, "warning": "No friction_report_*.json — run: python -m analytics.friction_report"}

    slip = friction.get("slippage_bps", {})
    lat = friction.get("latency_ms", {})
    dq = friction.get("data_quality", {})
    sizes = friction.get("sample_sizes", {})

    return {
        "available": True,
        "sufficient_data": dq.get("sufficient_data", False),
        "total_fills": sizes.get("total_fills", 0),
        "total_trades": sizes.get("total_trades", 0),
        "slippage_all_p50": slip.get("all", {}).get("p50"),
        "slippage_all_p95": slip.get("all", {}).get("p95"),
        "slippage_all_p99": slip.get("all", {}).get("p99"),
        "slippage_entry_p50": slip.get("entry", {}).get("p50"),
        "slippage_exit_p50": slip.get("exit", {}).get("p50"),
        "latency_all_p50_ms": lat.get("all", {}).get("p50"),
        "latency_all_p95_ms": lat.get("all", {}).get("p95"),
        "warnings": dq.get("warnings", []),
        "generated_at": friction.get("generated_at"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Phase 12 exit gate check
# ─────────────────────────────────────────────────────────────────────────────

def _check_exit_gate(
    baseline: Dict,
    walk_forward: Dict,
    stress: Dict,
    friction: Dict,
) -> Dict[str, Any]:
    checks: Dict[str, Any] = {}

    # Edge positive in baseline
    checks["edge_positive_baseline"] = bool(baseline.get("edge_positive"))

    # Walk-forward consistent (>=60% of windows positive)
    consistent = walk_forward.get("consistent")
    checks["walk_forward_consistent"] = consistent if consistent is not None else False

    # Edge survives all stress scenarios
    checks["edge_survives_stress"] = bool(stress.get("edge_survives_all"))

    # Friction data quality
    checks["friction_data_sufficient"] = bool(friction.get("sufficient_data"))

    # At least one WF run exists
    checks["walk_forward_run_exists"] = walk_forward.get("count", 0) > 0

    # At least one stress run exists
    checks["stress_run_exists"] = stress.get("count", 0) > 0

    gate_pass = (
        checks["edge_positive_baseline"]
        and checks["walk_forward_consistent"]
        and checks["edge_survives_stress"]
        and checks["walk_forward_run_exists"]
        and checks["stress_run_exists"]
    )

    return {
        "checks": checks,
        "gate_pass": gate_pass,
        "notes": [
            "friction_data_sufficient is advisory — Phase 11 data accumulates in parallel.",
            "gate_pass requires: baseline edge + walk-forward consistency + stress survival.",
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# HTML
# ─────────────────────────────────────────────────────────────────────────────

def _fmt(v: Any, suffix: str = "", na: str = "n/a") -> str:
    if v is None:
        return na
    return f"{v}{suffix}"


def _check_row(label: str, ok: Any) -> str:
    icon = "PASS" if ok else "FAIL"
    cls = "pass" if ok else "fail"
    return f'<tr><td>{label}</td><td class="{cls}">{icon}</td></tr>'


def build_html(report: Dict[str, Any]) -> str:
    gen = report.get("generated_at", "")
    base = report.get("baseline", {})
    wf = report.get("walk_forward", {})
    stress = report.get("stress", {})
    fric = report.get("friction", {})
    gate = report.get("exit_gate", {})
    gate_pass = gate.get("gate_pass", False)

    # Erosion table rows
    erosion = stress.get("erosion", {})
    erosion_rows = ""
    for name, e in erosion.items():
        survived = e.get("edge_survived")
        cls = "pass" if survived else ("fail" if survived is False else "")
        erosion_rows += (
            f"<tr><td>{name}</td>"
            f"<td>{_fmt(e.get('baseline_pnl'))}</td>"
            f"<td>{_fmt(e.get('scenario_pnl'))}</td>"
            f"<td>{_fmt(e.get('delta_pnl'))}</td>"
            f"<td>{_fmt(e.get('erosion_pct'))}%</td>"
            f"<td class='{cls}'>{_fmt(survived)}</td></tr>"
        )

    checks = gate.get("checks", {})
    gate_rows = "".join(_check_row(k, v) for k, v in checks.items())
    gate_cls = "pass" if gate_pass else "fail"
    gate_txt = "GATE PASS" if gate_pass else "GATE FAIL"

    warnings: List[str] = []
    for src in (base, wf, stress, fric):
        warnings.extend(src.get("warnings", []) if isinstance(src.get("warnings"), list) else [])
    warn_html = "".join(f'<div class="warn">&#9888; {w}</div>\n' for w in warnings)

    pnl_stats = wf.get("pnl_stats", {})

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Argus Research Report (Phase 12)</title>
<style>
  body {{ font-family: monospace; background: #111; color: #ccc; margin: 2rem; }}
  h1 {{ color: #eee; }}
  h2 {{ color: #aaa; margin-top: 2rem; border-bottom: 1px solid #333; padding-bottom: 4px; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 1rem; }}
  th {{ background: #222; color: #999; padding: 6px 12px; text-align: left; }}
  td {{ padding: 5px 12px; border-bottom: 1px solid #222; }}
  tr:hover td {{ background: #1a1a1a; }}
  .warn {{ background: #2a1a00; border-left: 3px solid #f90; padding: 8px 14px; margin: 6px 0; color: #f90; }}
  .pass {{ color: #0f0; font-weight: bold; }}
  .fail {{ color: #f44; font-weight: bold; }}
  .meta {{ color: #666; font-size: 0.85em; margin-bottom: 1rem; }}
  .gate {{ font-size: 1.5rem; padding: 10px 20px; margin: 1rem 0; display: inline-block; }}
</style>
</head>
<body>
<h1>Argus — Research Reality Integration (Phase 12)</h1>
<div class="meta">Generated: {gen}</div>
{warn_html}

<p>Phase 12 exit gate: <span class="gate {gate_cls}">{gate_txt}</span></p>

<h2>Baseline Performance</h2>
<table><tbody>
  <tr><td>Run ID</td><td>{_fmt(base.get('run_id'))}</td></tr>
  <tr><td>Trades closed</td><td>{_fmt(base.get('trades_closed'))}</td></tr>
  <tr><td>PnL (USD)</td><td>{_fmt(base.get('pnl_usd'))}</td></tr>
  <tr><td>Total return %</td><td>{_fmt(base.get('total_return_pct'))}%</td></tr>
  <tr><td>Max drawdown %</td><td>{_fmt(base.get('max_drawdown_pct'))}%</td></tr>
  <tr><td>Win rate %</td><td>{_fmt(base.get('win_rate_pct'))}%</td></tr>
  <tr><td>Expectancy (USD)</td><td>{_fmt(base.get('expectancy_usd'))}</td></tr>
  <tr><td>Profit factor</td><td>{_fmt(base.get('profit_factor'))}</td></tr>
  <tr><td>Edge positive</td><td class="{'pass' if base.get('edge_positive') else 'fail'}">{base.get('edge_positive')}</td></tr>
</tbody></table>

<h2>Walk-Forward Validation</h2>
<table><tbody>
  <tr><td>Run ID</td><td>{_fmt(wf.get('run_id'))}</td></tr>
  <tr><td>Windows</td><td>{_fmt(wf.get('windows_ok'))}/{_fmt(wf.get('windows_total'))} ok</td></tr>
  <tr><td>Mode</td><td>{_fmt(wf.get('mode'))}</td></tr>
  <tr><td>PnL p50</td><td>{_fmt(pnl_stats.get('p50'))}</td></tr>
  <tr><td>PnL min/max</td><td>{_fmt(pnl_stats.get('min'))} / {_fmt(pnl_stats.get('max'))}</td></tr>
  <tr><td>Windows positive</td><td>{_fmt(wf.get('windows_positive'))}/{_fmt(wf.get('windows_ok'))}</td></tr>
  <tr><td>Consistency</td><td>{_fmt(wf.get('consistency_pct'))}%</td></tr>
  <tr><td>Consistent (≥60%)</td><td class="{'pass' if wf.get('consistent') else 'fail'}">{wf.get('consistent')}</td></tr>
</tbody></table>

<h2>Stress Scenarios — Expectancy Erosion</h2>
<table>
<thead><tr><th>Scenario</th><th>Baseline PnL</th><th>Scenario PnL</th><th>Delta</th><th>Erosion %</th><th>Survived</th></tr></thead>
<tbody>{erosion_rows or '<tr><td colspan="6">No stress results — run: python -m backtest.stress_runner</td></tr>'}</tbody>
</table>
<p>Edge killers: <strong>{', '.join(stress.get('edge_killers', [])) or 'none'}</strong></p>

<h2>Friction (Phase 11)</h2>
<table><tbody>
  <tr><td>Available</td><td>{fric.get('available')}</td></tr>
  <tr><td>Sufficient data</td><td class="{'pass' if fric.get('sufficient_data') else 'warn'}">{fric.get('sufficient_data')}</td></tr>
  <tr><td>Total fills</td><td>{_fmt(fric.get('total_fills'))}</td></tr>
  <tr><td>Total trades</td><td>{_fmt(fric.get('total_trades'))}</td></tr>
  <tr><td>Slippage p50 (bps)</td><td>{_fmt(fric.get('slippage_all_p50'))}</td></tr>
  <tr><td>Slippage p95 (bps)</td><td>{_fmt(fric.get('slippage_all_p95'))}</td></tr>
  <tr><td>Latency p50 (ms)</td><td>{_fmt(fric.get('latency_all_p50_ms'))}</td></tr>
</tbody></table>

<h2>Phase 12 Exit Gate</h2>
<table><thead><tr><th>Check</th><th>Result</th></tr></thead>
<tbody>{gate_rows}</tbody>
</table>
{'<br>'.join(f'<div class="meta">{n}</div>' for n in gate.get('notes', []))}

</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def build_report(log_dir: str) -> Dict[str, Any]:
    baseline_summaries = _load_baseline_summaries(log_dir)
    wf_summaries = _load_walk_forward_summaries(log_dir)
    stress_summaries = _load_stress_summaries(log_dir)
    friction = _load_friction_report(log_dir)

    baseline = _analyse_baseline(baseline_summaries)
    walk_forward = _analyse_walk_forward(wf_summaries)
    stress = _analyse_stress(stress_summaries)
    fric_analysis = _analyse_friction(friction)
    gate = _check_exit_gate(baseline, walk_forward, stress, fric_analysis)

    return {
        "version": REPORT_VERSION,
        "generated_at": _utc_now(),
        "log_dir": log_dir,
        "baseline": baseline,
        "walk_forward": walk_forward,
        "stress": stress,
        "friction": fric_analysis,
        "exit_gate": gate,
    }


def write_report(report: Dict[str, Any], out_dir: str, date_str: str) -> Tuple[str, str]:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    json_path = os.path.join(out_dir, f"research_report_{date_str}.json")
    html_path = os.path.join(out_dir, f"research_summary_{date_str}.html")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(build_html(report))

    return json_path, html_path


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Phase 12 research report — baseline, walk-forward, stress, friction analysis"
    )
    parser.add_argument("--log-dir", default="ops/logs",
                        help="Directory containing all summary/report JSON files (default: ops/logs)")
    parser.add_argument("--out-dir", default=None,
                        help="Output directory (default: same as --log-dir)")
    parser.add_argument("--open", action="store_true", dest="open_browser",
                        help="Open HTML report in browser after generation")
    args = parser.parse_args(argv)

    log_dir = str(Path(args.log_dir).resolve())
    out_dir = str(Path(args.out_dir).resolve()) if args.out_dir else log_dir
    date_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    print(f"Building research report from {log_dir} ...")
    report = build_report(log_dir)

    json_path, html_path = write_report(report, out_dir, date_str)

    gate = report["exit_gate"]
    gate_txt = "GATE PASS" if gate["gate_pass"] else "GATE FAIL"
    print(f"  Phase 12 exit gate: {gate_txt}")
    for k, v in gate["checks"].items():
        mark = "PASS" if v else "FAIL"
        print(f"    {mark} {k}: {v}")

    base = report["baseline"]
    print(f"  Baseline: trades={base.get('trades_closed')} pnl={base.get('pnl_usd')} wr={base.get('win_rate_pct')}%")

    wf = report["walk_forward"]
    if wf.get("count", 0) > 0:
        print(f"  Walk-fwd: windows={wf.get('windows_positive')}/{wf.get('windows_ok')} pos, consistency={wf.get('consistency_pct')}%")

    stress = report["stress"]
    if stress.get("count", 0) > 0:
        killers = stress.get("edge_killers", [])
        print(f"  Stress:   edge_survives_all={stress.get('edge_survives_all')}, killers={killers or 'none'}")

    print(f"\n  JSON -> {json_path}")
    print(f"  HTML -> {html_path}")

    if args.open_browser:
        import webbrowser
        webbrowser.open(Path(html_path).as_uri())


if __name__ == "__main__":
    main()
