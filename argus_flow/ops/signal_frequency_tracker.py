"""Live vs replay signal-frequency tracker.

Answers the question: "is this strategy firing at the rate its backtest
predicted?" For each tracked strategy, computes:

  - replay_signals_per_day : expectation from config/rulebook
  - live_triggers_per_day  : non-NO_TRIGGER rows / observed window
  - live_valid_trades_per_day : valid trades / observed window
  - ratio_trigger / ratio_valid : live / replay

A ratio far below 1.0 is the symptom we saw on USDJPY (12x replay vs live)
and GBPUSD (18.9/day expected vs 0-1/day observed). Now it runs nightly
across every strategy instead of being discovered by hand.

Output: argus_flow/logs/signal_frequency_report.json

Usage:
    python -m argus_flow.ops.signal_frequency_tracker
    python -m argus_flow.ops.signal_frequency_tracker --window 7
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT_PATH = REPO / "argus_flow" / "logs" / "signal_frequency_report.json"
HISTORY_PATH = REPO / "argus_flow" / "logs" / "signal_frequency_history.jsonl"

# Strategy specs. `replay_expected_per_day` is pulled from config where
# available (Argus) or hardcoded from the runner spec / rulebook (Forge).
# Forge backtest cadences from 2026-04-16 research batch: gld_pm_long 265/yr,
# nq_overnight 325/yr, jpy_pm_short 500/yr combined, wick_gbpusd 13/yr.
# Divide by 252 trading days for per-day.
STRATEGIES = [
    {"label": "argus_usdjpy",        "signals": "argus_flow/logs/usdjpy/signals.csv",
     "trades": "argus_flow/logs/usdjpy/trades.csv",
     "replay_source": "argus_flow/configs/usdjpy_mtf_paper_v1.json",
     "valid_filter": True},
    {"label": "argus_gbpusd",        "signals": "argus_flow/logs/gbpusd/signals.csv",
     "trades": "argus_flow/logs/gbpusd/trades.csv",
     "replay_source": "argus_flow/configs/gbpusd_range_paper_v1.json",
     "valid_filter": True},
    {"label": "argus_cadjpy",        "signals": "argus_flow/logs/cadjpy/signals.csv",
     "trades": "argus_flow/logs/cadjpy/trades.csv",
     "replay_source": "argus_flow/configs/cadjpy_mtf_paper_v1.json",
     "valid_filter": True},
    {"label": "forge_gld_pm_long",   "signals": "forge/logs/gld_pm_long/signals.csv",
     "trades": "forge/logs/gld_pm_long/trades.csv",
     "replay_per_day": 265 / 252},
    {"label": "forge_wick_gbpusd",   "signals": "forge/logs/wick_gbpusd/signals.csv",
     "trades": "forge/logs/wick_gbpusd/trades.csv",
     "replay_per_day": 13 / 252},
    {"label": "forge_nq_overnight",  "signals": "forge/logs/nq_overnight/signals.csv",
     "trades": "forge/logs/nq_overnight/trades.csv",
     "replay_per_day": 325 / 252},
    {"label": "forge_jpy_pm_short",  "signals": "forge/logs/jpy_pm_short/signals.csv",
     "trades": "forge/logs/jpy_pm_short/trades.csv",
     "replay_per_day": 500 / 252},
]

NO_TRIGGER_MARKER = "NO_TRIGGER"


def _parse_ts(s: str):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        try:
            return datetime.strptime(s.split(".")[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def _replay_per_day(spec: dict) -> float | None:
    if "replay_per_day" in spec:
        return float(spec["replay_per_day"])
    src = spec.get("replay_source")
    if not src:
        return None
    p = REPO / src
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    rx = d.get("replay_expectations")
    if isinstance(rx, dict) and "signals_per_day" in rx:
        return float(rx["signals_per_day"])
    return None


def _window_bounds(window_days: int | None):
    if window_days and window_days > 0:
        return datetime.now(timezone.utc) - timedelta(days=window_days)
    return None


def _count_signals(path: Path, cutoff: datetime | None) -> tuple[int, int, datetime | None, datetime | None]:
    """Return (total_rows, non_no_trigger_rows, first_ts, last_ts) within window."""
    if not path.exists():
        return 0, 0, None, None
    total = 0
    triggers = 0
    first_ts = None
    last_ts = None
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            ts = _parse_ts(r.get("ts") or r.get("timestamp") or "")
            if ts is None:
                continue
            if cutoff and ts < cutoff:
                continue
            total += 1
            action = str(r.get("action") or r.get("signal") or "").upper()
            if action and NO_TRIGGER_MARKER not in action and action != "SIGNAL":
                triggers += 1
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
    return total, triggers, first_ts, last_ts


def _count_trades(path: Path, cutoff: datetime | None, valid_filter: bool) -> int:
    if not path.exists():
        return 0
    n = 0
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            ts = _parse_ts(r.get("ts") or r.get("entry_date") or r.get("entry_ts") or "")
            if ts is None:
                continue
            if cutoff and ts < cutoff:
                continue
            if valid_filter and str(r.get("experiment_valid", "")).lower() != "true":
                continue
            n += 1
    return n


def _analyze(spec: dict, cutoff: datetime | None) -> dict:
    out = {"label": spec["label"], "replay_signals_per_day": _replay_per_day(spec)}
    sig_path = REPO / spec["signals"]
    trade_path = REPO / spec["trades"]

    total_rows, triggers, first_ts, last_ts = _count_signals(sig_path, cutoff)
    trades = _count_trades(trade_path, cutoff, spec.get("valid_filter", False))

    out["signal_rows_in_window"] = total_rows
    out["live_triggers_in_window"] = triggers
    out["trades_in_window"] = trades
    out["first_signal_ts"] = first_ts.isoformat() if first_ts else None
    out["last_signal_ts"] = last_ts.isoformat() if last_ts else None

    if first_ts and last_ts and last_ts > first_ts:
        observed_days = max((last_ts - first_ts).total_seconds() / 86400, 0.25)  # floor 6h to avoid divide-noise
        out["observed_days"] = round(observed_days, 2)
        out["live_triggers_per_day"] = round(triggers / observed_days, 3)
        out["live_trades_per_day"] = round(trades / observed_days, 3)
        if out["replay_signals_per_day"]:
            rexp = out["replay_signals_per_day"]
            out["ratio_trigger_vs_replay"] = round(out["live_triggers_per_day"] / rexp, 3) if rexp > 0 else None
            out["ratio_trade_vs_replay"] = round(out["live_trades_per_day"] / rexp, 3) if rexp > 0 else None
    else:
        out["observed_days"] = 0
        out["live_triggers_per_day"] = 0
        out["live_trades_per_day"] = 0

    # Drift severity: use trade-rate ratio (not trigger ratio). A trigger
    # can be a filtered/blocked signal — only valid trades represent the
    # edge actually realized. We still surface trigger rate for diagnosis
    # of "signals firing but not passing gates" vs "signals not firing."
    ratio = out.get("ratio_trade_vs_replay")
    observed_days = out.get("observed_days", 0)
    if observed_days < 3:
        out["severity"] = "insufficient_sample"  # <3 days of data; ratio unreliable
    elif ratio is None:
        out["severity"] = "unknown"
    elif ratio < 0.2:
        out["severity"] = "severe"
    elif ratio < 0.5:
        out["severity"] = "warning"
    elif ratio > 3.0:
        out["severity"] = "high_variance"
    else:
        out["severity"] = "ok"

    # Diagnostic flag: are signals firing but gates eating them?
    rt = out.get("ratio_trigger_vs_replay")
    if rt is not None and ratio is not None and rt >= 1.0 and ratio < 0.5:
        out["diagnosis"] = "triggers_firing_but_gates_eating_them"
    elif rt is not None and rt < 0.2 and ratio is not None and ratio < 0.2:
        out["diagnosis"] = "strategy_silent"

    return out


def build_report(window_days: int | None) -> dict:
    cutoff = _window_bounds(window_days)
    strategies = [_analyze(spec, cutoff) for spec in STRATEGIES]
    severe = [s["label"] for s in strategies if s["severity"] == "severe"]
    warning = [s["label"] for s in strategies if s["severity"] == "warning"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": window_days,
        "window_label": "all_time" if not window_days else f"last_{window_days}d",
        "strategies": strategies,
        "summary": {
            "severe_drift": severe,
            "warning_drift": warning,
            "total_analyzed": len(strategies),
        },
    }


def _append_history(report: dict) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        from helio.fleet_sizing import get_sizing_anchor_usd
        anchor_now = get_sizing_anchor_usd()
    except Exception:
        anchor_now = None
    row = {
        "ts": report["generated_at"],
        "anchor_at_time_usd": anchor_now,
        "window_days": report["window_days"],
        "per_strategy": {
            s["label"]: {
                "replay": s.get("replay_signals_per_day"),
                "live_triggers_per_day": s.get("live_triggers_per_day"),
                "live_trades_per_day": s.get("live_trades_per_day"),
                "ratio": s.get("ratio_trigger_vs_replay"),
                "severity": s.get("severity"),
            }
            for s in report["strategies"]
        },
    }
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _print_table(report: dict) -> None:
    print(f"=== SIGNAL FREQUENCY vs REPLAY  ({report['window_label']}) ===")
    print(f"{'Strategy':24s} {'Replay/d':>9s} {'Trig/d':>8s} {'Trade/d':>9s} {'Trig.rat':>9s} {'Trade.rat':>10s} {'Severity':>20s}")
    print("-" * 92)
    for s in report["strategies"]:
        r = s.get("replay_signals_per_day")
        lt = s.get("live_triggers_per_day") or 0.0
        ltr = s.get("live_trades_per_day") or 0.0
        trig_ratio = s.get("ratio_trigger_vs_replay")
        trade_ratio = s.get("ratio_trade_vs_replay")
        rs = f"{r:.2f}" if r is not None else "  -  "
        tr = f"{trig_ratio:.2f}x" if trig_ratio is not None else "  -  "
        tdr = f"{trade_ratio:.2f}x" if trade_ratio is not None else "  -  "
        print(f"{s['label']:24s} {rs:>9s} {lt:>8.2f} {ltr:>9.2f} {tr:>9s} {tdr:>10s} {s['severity']:>20s}")
    print()
    print("  Severity uses Trade.rat (valid trades / replay). Trig.rat is diagnostic:")
    print("  if Trig.rat is ~1.0 but Trade.rat is <0.5, gates are eating signals.")
    print()
    sm = report["summary"]
    if sm["severe_drift"]:
        print(f"  SEVERE drift (trades <20% of replay):  {', '.join(sm['severe_drift'])}")
    if sm["warning_drift"]:
        print(f"  WARNING drift (trades <50% of replay): {', '.join(sm['warning_drift'])}")
    if not sm["severe_drift"] and not sm["warning_drift"]:
        print("  No drift flags on strategies with sufficient sample.")
    # Highlight diagnosis
    for s in report["strategies"]:
        if s.get("diagnosis"):
            print(f"  DIAGNOSIS {s['label']}: {s['diagnosis']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=None,
                    help="Restrict to trades/signals within last N days (default: all time)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-history", action="store_true")
    args = ap.parse_args()

    report = build_report(window_days=args.window)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2))
    if not args.no_history:
        _append_history(report)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_table(report)
        print(f"\nWrote: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
