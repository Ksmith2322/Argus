"""helio/fleet_perf_summary.py — Unified per-strategy + fleet-level performance.

Aggregates every strategy's trades.csv into one JSON, expressing PnL in both
dollars and % of the fleet-sizing anchor. This is the "how are we actually
doing" single source of truth — meant to replace eyeballing per-strategy
logs when you want the fleet picture.

Usage:
    python -m helio.fleet_perf_summary           # write summary + print
    python -m helio.fleet_perf_summary --window 7   # last 7 days only
    python -m helio.fleet_perf_summary --json     # machine-readable

Output: argus_flow/logs/fleet_perf_summary.json
Intended to be wired into ops/run_cohort_report.ps1 for daily regeneration.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from helio.fleet_sizing import get_initial_capital_usd, pnl_pct_of_fleet  # noqa: E402

OUT_PATH = REPO / "argus_flow" / "logs" / "fleet_perf_summary.json"
HISTORY_PATH = REPO / "argus_flow" / "logs" / "fleet_perf_history.jsonl"

# Strategy spec: (label, trade_csv_path, pnl_column, valid_filter)
# pnl_column chooses which numeric field to sum; some strategies log pnl_usd,
# others pnl_pct, others pnl_pips. We normalize to USD below using per-row
# fallbacks where possible.
STRATEGIES: list[dict] = [
    {"label": "argus_usdjpy", "path": "argus_flow/logs/usdjpy/trades.csv", "pnl_col": "pnl_pips", "usd_col": "pnl_usd", "valid_filter": True},
    {"label": "argus_gbpusd", "path": "argus_flow/logs/gbpusd/trades.csv", "pnl_col": "pnl_pips", "usd_col": "pnl_usd", "valid_filter": True},
    {"label": "argus_cadjpy", "path": "argus_flow/logs/cadjpy/trades.csv", "pnl_col": "pnl_pips", "usd_col": "pnl_usd", "valid_filter": True},
    {"label": "forge_gld_pm_long", "path": "forge/logs/gld_pm_long/trades.csv", "pnl_col": "pnl_usd", "usd_col": "pnl_usd"},
    {"label": "forge_wick_gbpusd", "path": "forge/logs/wick_gbpusd/trades.csv", "pnl_col": "pnl_usd", "usd_col": "pnl_usd"},
    {"label": "forge_nq_overnight", "path": "forge/logs/nq_overnight/trades.csv", "pnl_col": "pnl_usd", "usd_col": "pnl_usd"},
    {"label": "forge_jpy_pm_short", "path": "forge/logs/jpy_pm_short/trades.csv", "pnl_col": "pnl_usd", "usd_col": "pnl_usd"},
    {"label": "forge_gdx_gld", "path": "forge/logs/gdx_gld/trades.csv", "pnl_col": "pnl_pct", "usd_col": "pnl_usd"},
]


def _parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    # Try ISO 8601 first (handles microseconds + timezones)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    # Fallback formats
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _row_ts_candidates(row: dict) -> str:
    """Some strategies use different ts column names. Return first populated."""
    for col in ("ts", "entry_ts", "entry_date", "timestamp"):
        v = row.get(col)
        if v:
            return v
    return ""


def _row_usd_pnl(row: dict, spec: dict) -> float | None:
    """Return exact USD PnL for a row, or None if not directly available.

    Strategies logging pnl_pct only (e.g. gdx_gld — pct of its own equity,
    not of the fleet anchor) return None; we surface pnl_pct separately in
    `alt_metric_note` rather than guessing a USD conversion.

    Strategies logging pnl_pips only (e.g. Argus pairs with unset pnl_usd
    column) also return None; summary rolls up pips separately.
    """
    usd = row.get(spec.get("usd_col") or "")
    if usd not in (None, ""):
        try:
            return float(usd)
        except ValueError:
            return None
    return None


def _strategy_summary(spec: dict, anchor: float, window_cutoff: datetime | None) -> dict:
    p = REPO / spec["path"]
    s = {
        "label": spec["label"],
        "present": p.exists(),
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate_pct": None,
        "pnl_usd": None,
        "pnl_pct_of_fleet": None,
        "alt_metric": None,
        "alt_metric_note": None,
        "first_trade_ts": None,
        "last_trade_ts": None,
    }
    if not p.exists():
        return s

    try:
        with open(p, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        s["error"] = f"read_failed: {e}"
        return s

    if spec.get("valid_filter"):
        rows = [r for r in rows if str(r.get("experiment_valid", "")).lower() == "true"]

    if window_cutoff is not None:
        rows = [r for r in rows if (ts := _parse_ts(_row_ts_candidates(r))) and ts >= window_cutoff]

    s["trades"] = len(rows)
    if not rows:
        return s

    # Primary USD path — tolerate partial coverage (some historical rows lack pnl_usd)
    usd_pnls = [_row_usd_pnl(r, spec) for r in rows]
    usd_rows = [(r, p) for r, p in zip(rows, usd_pnls) if p is not None]
    usd_count = len(usd_rows)

    if usd_count > 0:
        usd_pnls_clean = [p for _, p in usd_rows]
        s["wins"] = sum(1 for p in usd_pnls_clean if p > 0)
        s["losses"] = sum(1 for p in usd_pnls_clean if p < 0)
        s["win_rate_pct"] = round(s["wins"] / usd_count * 100.0, 1)
        s["pnl_usd"] = round(sum(usd_pnls_clean), 2)
        s["pnl_pct_of_fleet"] = round(pnl_pct_of_fleet(s["pnl_usd"]), 4)
        s["usd_coverage"] = f"{usd_count}/{len(rows)}"
        if usd_count < len(rows):
            s["partial_usd"] = True

    # Also compute alt_metric from fallback column (informational only when
    # USD coverage is partial or zero)
    pnl_col = spec.get("pnl_col")
    if pnl_col and pnl_col != spec.get("usd_col"):
        vals: list[float] = []
        for r in rows:
            v = r.get(pnl_col)
            if v not in (None, ""):
                try:
                    vals.append(float(v))
                except ValueError:
                    pass
        if vals:
            if s["pnl_usd"] is None:
                s["wins"] = sum(1 for v in vals if v > 0)
                s["losses"] = sum(1 for v in vals if v < 0)
                s["win_rate_pct"] = round(s["wins"] / len(vals) * 100.0, 1)
            s["alt_metric"] = round(sum(vals), 4)
            s["alt_metric_note"] = f"sum of `{pnl_col}`"

    tss = [_parse_ts(_row_ts_candidates(r)) for r in rows]
    tss = [t for t in tss if t]
    if tss:
        s["first_trade_ts"] = min(tss).isoformat()
        s["last_trade_ts"] = max(tss).isoformat()

    return s


def _view(anchor: float, window_days: int | None) -> dict:
    cutoff = None
    if window_days is not None and window_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    strategies = [_strategy_summary(spec, anchor, cutoff) for spec in STRATEGIES]
    usd_strats = [s for s in strategies if s.get("pnl_usd") is not None]
    unconverted = [s["label"] for s in strategies if s.get("pnl_usd") is None and s.get("alt_metric") is not None]
    total_usd = sum(s["pnl_usd"] for s in usd_strats)
    return {
        "window_days": window_days,
        "window_label": "all_time" if window_days is None else f"last_{window_days}d",
        "strategies": strategies,
        "fleet_total": {
            "pnl_usd": round(total_usd, 2),
            "pnl_pct_of_fleet": round(pnl_pct_of_fleet(total_usd), 4),
            "active_strategies": sum(1 for s in strategies if s.get("trades", 0) > 0),
            "strategies_in_roll_up": [s["label"] for s in usd_strats],
            "unconverted_strategies": unconverted,
        },
    }


def build_summary(window_days: int | None = 7) -> dict:
    """Produce both a live window view (default 7d) AND the all-time view.

    Why: earlier single-view output let a ~20 years of gdx_gld historical
    back-fill look like "current fleet PnL" in nightly reports. Shipping both
    side-by-side makes the distinction impossible to miss.
    """
    anchor = get_initial_capital_usd()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "anchor_capital_usd": anchor,
        "live_window": _view(anchor, window_days),
        "all_time": _view(anchor, None),
    }


def _print_view(view: dict, header: str) -> None:
    print(header)
    print(f"{'Strategy':24s} {'Trades':>6s} {'WR':>6s} {'PnL USD':>10s} {'PnL %':>8s} {'Last trade':>22s}")
    print("-" * 82)
    for s in view["strategies"]:
        wr = f"{s['win_rate_pct']:.0f}%" if s["win_rate_pct"] is not None else "  -  "
        lt = (s["last_trade_ts"] or "-")[:19]
        if s.get("pnl_usd") is not None:
            pnl_u = f"{s['pnl_usd']:>10.2f}"
            pnl_p = f"{s['pnl_pct_of_fleet']:>7.3f}%"
            cov = f"({s['usd_coverage']})" if s.get("partial_usd") else ""
        else:
            alt_note = f"alt={s.get('alt_metric')}" if s.get("alt_metric") is not None else "no USD"
            pnl_u = f"{alt_note:>10s}"
            pnl_p = "     -  "
            cov = ""
        print(f"{s['label']:24s} {s['trades']:>6d} {wr:>6s} {pnl_u} {pnl_p} {lt:>22s} {cov}")
    print("-" * 82)
    ft = view["fleet_total"]
    print(f"{'FLEET TOTAL (USD-roll-up)':26s} {'':>4s} {'':>6s} {ft['pnl_usd']:>10.2f} {ft['pnl_pct_of_fleet']:>7.3f}%")
    if ft.get("unconverted_strategies"):
        print(f"  (excluded — no USD PnL: {', '.join(ft['unconverted_strategies'])})")


def _print_table(summary: dict) -> None:
    anchor = summary["anchor_capital_usd"]
    print(f"Fleet anchor: ${anchor:,.2f}    Generated: {summary['generated_at']}")
    print()
    live = summary["live_window"]
    _print_view(live, f"=== LIVE WINDOW ({live['window_label']}) ===")
    print()
    _print_view(summary["all_time"], "=== ALL TIME (includes historical back-fill) ===")
    print()
    print("Reminder: 'all time' includes multi-year gdx_gld historical trades "
          "(first trade 2006). Use 'live window' to gauge current fleet performance.")


def _append_history(summary: dict) -> None:
    """Append a compact row to fleet_perf_history.jsonl. Each row records
    the anchor active at write time so future charts can normalize across
    the 2026-04-17 $10K→broker-equity transition honestly.
    """
    row = {
        "ts": summary["generated_at"],
        "anchor_at_time_usd": summary["anchor_capital_usd"],
        "anchor_capital_usd": summary["anchor_capital_usd"],  # back-compat alias
        "all_time": {
            "pnl_usd": summary["all_time"]["fleet_total"]["pnl_usd"],
            "pnl_pct_of_fleet": summary["all_time"]["fleet_total"]["pnl_pct_of_fleet"],
        },
        "live_window_days": summary["live_window"]["window_days"],
        "live_window": {
            "pnl_usd": summary["live_window"]["fleet_total"]["pnl_usd"],
            "pnl_pct_of_fleet": summary["live_window"]["fleet_total"]["pnl_pct_of_fleet"],
        },
        "per_strategy": {
            s["label"]: {
                "trades_all_time": s.get("trades"),
                "pnl_usd_all_time": s.get("pnl_usd"),
                "wr_pct": s.get("win_rate_pct"),
            }
            for s in summary["all_time"]["strategies"]
        },
    }
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _append_promotion_history() -> None:
    """Snapshot promotion-gate state so we can chart valid_trades growth
    toward the 60-trade funding gate over time."""
    path = REPO / "argus_flow" / "logs" / "promotion_gate_report.json"
    if not path.exists():
        return
    try:
        gate = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    hist_path = REPO / "argus_flow" / "logs" / "promotion_gate_history.jsonl"
    runners = gate.get("runners", []) if isinstance(gate.get("runners"), list) else []
    try:
        anchor_now = get_initial_capital_usd()
    except Exception:
        anchor_now = None
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "anchor_at_time_usd": anchor_now,
        "overall_status": gate.get("overall_status", gate.get("status")),
        "promote": gate.get("promote"),
        "not_ready": gate.get("not_ready"),
        "runners": [
            {
                "name": r.get("name"),
                "verdict": r.get("verdict"),
                "valid_trades": r.get("valid_trades"),
                "total_trades": r.get("total_trades"),
                "profit_factor": r.get("profit_factor"),
                "win_rate": r.get("win_rate"),
                "expectancy": r.get("expectancy"),
            } for r in runners
        ],
    }
    hist_path.parent.mkdir(parents=True, exist_ok=True)
    with open(hist_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=7,
                    help="Days for live-window view (default 7). All-time view is always emitted alongside.")
    ap.add_argument("--json", action="store_true", help="Print JSON instead of table")
    ap.add_argument("--no-history", action="store_true",
                    help="Don't append to fleet_perf_history.jsonl (useful for ad-hoc runs)")
    args = ap.parse_args()

    summary = build_summary(window_days=args.window)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(summary, indent=2))
    if not args.no_history:
        _append_history(summary)
        _append_promotion_history()

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        _print_table(summary)
        print(f"\nWrote: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
