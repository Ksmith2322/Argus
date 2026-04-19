"""USDJPY live-vs-replay drift forensics.

Answers: "why is USDJPY firing at 14% of replay's expected rate?"

Builds:
  1. Blocker histogram — distribution of non-NO_TRIGGER actions in signals.csv
  2. Hour-of-day trigger counts
  3. Feature distribution comparison (triggered bars vs filtered bars)
  4. Summary verdict

Output: argus_flow/logs/usdjpy_drift_forensics_report.json
        + console table

Run:
    python -m argus_flow.ops.usdjpy_drift_forensics
    python -m argus_flow.ops.usdjpy_drift_forensics --pair gbpusd
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]


def _load_signals(pair: str, window_days: int | None = 7) -> list[dict]:
    path = _REPO / "argus_flow" / "logs" / pair / "signals.csv"
    if not path.exists():
        return []
    rows = []
    cutoff = None
    if window_days and window_days > 0:
        import datetime as _dt
        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=window_days)
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            ts_raw = r.get("ts") or ""
            if cutoff and ts_raw:
                try:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < cutoff:
                        continue
                except ValueError:
                    pass
            rows.append(r)
    return rows


def _parse_float(v, default=0.0):
    try:
        return float(v) if v not in (None, "", "nan") else default
    except (TypeError, ValueError):
        return default


def analyze_pair(pair: str, window_days: int = 7) -> dict:
    rows = _load_signals(pair, window_days)
    if not rows:
        return {"pair": pair, "error": "no signals.csv rows in window"}

    action_counts = Counter(r.get("action", "UNKNOWN") for r in rows)
    total = len(rows)
    no_trigger = action_counts.get("NO_TRIGGER", 0)
    triggers = total - no_trigger
    entries = action_counts.get("ENTRY", 0)

    # Blocker histogram: any action that's NOT NO_TRIGGER and NOT ENTRY
    blockers = {a: c for a, c in action_counts.items() if a not in ("NO_TRIGGER", "ENTRY")}
    blocker_total = sum(blockers.values())

    # By-hour histogram
    hour_counts = defaultdict(lambda: {"total": 0, "triggers": 0, "entries": 0})
    for r in rows:
        try:
            hr = int(r.get("hour") or 0)
        except (TypeError, ValueError):
            continue
        hour_counts[hr]["total"] += 1
        action = r.get("action", "")
        if action != "NO_TRIGGER":
            hour_counts[hr]["triggers"] += 1
        if action == "ENTRY":
            hour_counts[hr]["entries"] += 1

    # Feature distribution: range_pct + range_accel on triggered vs filtered
    triggered_rpct = []
    triggered_raccel = []
    filtered_rpct = []
    filtered_raccel = []
    for r in rows:
        rpct = _parse_float(r.get("range_pct"))
        raccel = _parse_float(r.get("range_accel"))
        if rpct == 0 and raccel == 0:
            continue
        if r.get("action", "") == "NO_TRIGGER":
            filtered_rpct.append(rpct)
            filtered_raccel.append(raccel)
        else:
            triggered_rpct.append(rpct)
            triggered_raccel.append(raccel)

    def _stats(xs):
        if not xs:
            return {"n": 0}
        return {
            "n": len(xs),
            "mean": round(statistics.mean(xs), 6),
            "median": round(statistics.median(xs), 6),
            "stdev": round(statistics.stdev(xs), 6) if len(xs) > 1 else 0,
        }

    # Load replay expectation if present
    cfg_path = _REPO / "argus_flow" / "configs" / f"{pair}_mtf_paper_v1.json"
    if not cfg_path.exists():
        cfg_path = _REPO / "argus_flow" / "configs" / f"{pair}_range_paper_v1.json"
    replay_expected_per_day = None
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            replay_expected_per_day = cfg.get("replay_expectations", {}).get("signals_per_day")
        except Exception:
            pass

    # Observed per day (total triggers / window_days)
    observed_per_day = triggers / window_days if window_days > 0 else 0
    ratio = (observed_per_day / replay_expected_per_day) if replay_expected_per_day else None

    # Verdict logic: where is the drop?
    verdict = []
    if ratio is not None and ratio < 0.5:
        verdict.append(f"Live trigger rate {observed_per_day:.2f}/day is {ratio:.1%} of replay {replay_expected_per_day}/day — severe drift.")
    if blocker_total > entries * 2:
        most_common = Counter(blockers).most_common(3)
        verdict.append(f"Gates eating {blocker_total} signals vs only {entries} entries. Top blockers: {most_common}")
    # Feature comparison insight
    if triggered_rpct and filtered_rpct:
        t_mean = statistics.mean(triggered_rpct)
        f_mean = statistics.mean(filtered_rpct)
        if t_mean > f_mean * 1.3:
            verdict.append(f"Triggered bars have higher range_pct (mean {t_mean:.5f}) than filtered ({f_mean:.5f}) — filter working as designed, edge may be in the filter not being hit more often.")
    if not verdict:
        verdict.append("No severe drift signal in this window.")

    return {
        "pair": pair,
        "window_days": window_days,
        "total_bars_evaluated": total,
        "no_trigger_count": no_trigger,
        "trigger_count": triggers,
        "entry_count": entries,
        "trigger_rate_pct": round(triggers / total * 100, 2) if total else 0,
        "entry_rate_pct": round(entries / total * 100, 2) if total else 0,
        "blockers": dict(sorted(blockers.items(), key=lambda x: -x[1])),
        "replay_expected_signals_per_day": replay_expected_per_day,
        "observed_triggers_per_day": round(observed_per_day, 3),
        "ratio_observed_vs_replay": round(ratio, 3) if ratio is not None else None,
        "hour_distribution": {str(h): dict(v) for h, v in sorted(hour_counts.items())},
        "feature_stats": {
            "triggered": {"range_pct": _stats(triggered_rpct), "range_accel": _stats(triggered_raccel)},
            "filtered": {"range_pct": _stats(filtered_rpct), "range_accel": _stats(filtered_raccel)},
        },
        "verdict": verdict,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", default="usdjpy", help="Pair to analyze (default: usdjpy)")
    ap.add_argument("--window", type=int, default=7, help="Days to look back (default: 7)")
    ap.add_argument("--all-pairs", action="store_true", help="Run on usdjpy, gbpusd, cadjpy")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    pairs = ["usdjpy", "gbpusd", "cadjpy"] if args.all_pairs else [args.pair]
    reports = {p: analyze_pair(p, args.window) for p in pairs}

    out_path = _REPO / "argus_flow" / "logs" / "drift_forensics_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": args.window,
        "pairs": reports,
    }, indent=2))

    if args.json:
        print(json.dumps(reports, indent=2))
    else:
        for pair, r in reports.items():
            print(f"=== {pair.upper()} drift forensics (last {args.window}d) ===")
            if "error" in r:
                print(f"  {r['error']}")
                continue
            print(f"  bars evaluated: {r['total_bars_evaluated']}")
            print(f"  triggers: {r['trigger_count']} ({r['trigger_rate_pct']}%)  entries: {r['entry_count']} ({r['entry_rate_pct']}%)")
            if r["replay_expected_signals_per_day"]:
                rr = r["ratio_observed_vs_replay"]
                print(f"  observed {r['observed_triggers_per_day']:.2f}/day  vs replay {r['replay_expected_signals_per_day']}/day  ratio={rr:.2%}" if rr is not None else "")
            if r["blockers"]:
                print("  blocker histogram (top 5):")
                for name, count in list(r["blockers"].items())[:5]:
                    print(f"    {name}: {count}")
            print("  verdict:")
            for v in r["verdict"]:
                print(f"    - {v}")
            print()
        print(f"Wrote: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
