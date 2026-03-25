"""Signal Quality Analyzer — analyze signal patterns and feature distributions.

Reports: signals/hour distribution, trigger rate by session, feature stats,
entry vs no-trigger comparison, to verify strategy is behaving as expected.

Usage:
    python -m argus_flow.ops.signal_analyzer
    python -m argus_flow.ops.signal_analyzer --symbol GBPUSD
    python -m argus_flow.ops.signal_analyzer --days 7
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

RUNNERS = [
    {"name": "EUR/USD", "symbol": "EURUSD", "log_dir": "argus_flow/logs/eurusd"},
    {"name": "GBP/USD", "symbol": "GBPUSD", "log_dir": "argus_flow/logs/gbpusd"},
    {"name": "EUR/JPY", "symbol": "EURJPY", "log_dir": "argus_flow/logs/eurjpy"},
]


def _load_signals(log_dir: Path, days: int = 0) -> list[dict]:
    sig_file = log_dir / "signals.csv"
    if not sig_file.exists():
        return []
    with open(sig_file) as f:
        rows = list(csv.DictReader(f))
    if days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        filtered = []
        for r in rows:
            try:
                ts = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
                if ts >= cutoff:
                    filtered.append(r)
            except Exception:
                filtered.append(r)
        return filtered
    return rows


def _safe_float(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def analyze_runner(runner: dict, days: int) -> dict:
    log_dir = REPO / runner["log_dir"]
    signals = _load_signals(log_dir, days)

    result = {
        "name": runner["name"],
        "symbol": runner["symbol"],
        "total_signals": len(signals),
        "entries": 0,
        "no_triggers": 0,
        "trigger_rate": 0,
        "hourly_distribution": {},
        "session_breakdown": {},
        "feature_stats": {},
        "direction_split": {},
        "days_covered": 0,
        "signals_per_day": 0,
        "entries_per_day": 0,
    }

    if not signals:
        return result

    entries = [s for s in signals if s.get("action") == "ENTRY"]
    no_triggers = [s for s in signals if s.get("action") == "NO_TRIGGER"]
    result["entries"] = len(entries)
    result["no_triggers"] = len(no_triggers)
    result["trigger_rate"] = round(len(entries) / len(signals) * 100, 2) if signals else 0

    # Days covered
    try:
        first_ts = datetime.fromisoformat(signals[0]["ts"].replace("Z", "+00:00"))
        last_ts = datetime.fromisoformat(signals[-1]["ts"].replace("Z", "+00:00"))
        result["days_covered"] = round(max((last_ts - first_ts).total_seconds() / 86400, 0.01), 2)
        result["signals_per_day"] = round(len(signals) / result["days_covered"], 1)
        result["entries_per_day"] = round(len(entries) / result["days_covered"], 1)
    except Exception:
        pass

    # Hourly distribution
    hour_counts = Counter()
    hour_entries = Counter()
    for s in signals:
        try:
            h = int(s.get("hour", 0))
            hour_counts[h] += 1
            if s.get("action") == "ENTRY":
                hour_entries[h] += 1
        except (ValueError, TypeError):
            pass

    result["hourly_distribution"] = {
        "signals": dict(sorted(hour_counts.items())),
        "entries": dict(sorted(hour_entries.items())),
    }

    # Session breakdown
    sessions = {"ASIA": (0, 7), "LONDON": (8, 12), "NY": (13, 16), "US_PM": (17, 20), "OFF": (21, 23)}
    for sess_name, (start, end) in sessions.items():
        sess_sigs = [s for s in signals if start <= int(s.get("hour", 99)) <= end]
        sess_entries = [s for s in sess_sigs if s.get("action") == "ENTRY"]
        result["session_breakdown"][sess_name] = {
            "signals": len(sess_sigs),
            "entries": len(sess_entries),
            "trigger_rate": round(len(sess_entries) / len(sess_sigs) * 100, 2) if sess_sigs else 0,
        }

    # Feature stats (for entries vs all)
    for feat in ["range_pct", "vol_z", "range_accel", "dist_from_low"]:
        all_vals = [_safe_float(s.get(feat)) for s in signals]
        all_vals = [v for v in all_vals if v is not None]
        entry_vals = [_safe_float(s.get(feat)) for s in entries]
        entry_vals = [v for v in entry_vals if v is not None]

        if all_vals:
            result["feature_stats"][feat] = {
                "all_mean": round(sum(all_vals) / len(all_vals), 6),
                "all_min": round(min(all_vals), 6),
                "all_max": round(max(all_vals), 6),
                "entry_mean": round(sum(entry_vals) / len(entry_vals), 6) if entry_vals else None,
                "entry_count": len(entry_vals),
            }

    # Direction split
    dir_counts = Counter(s.get("direction", "") for s in entries if s.get("direction"))
    result["direction_split"] = dict(dir_counts)

    return result


def main():
    parser = argparse.ArgumentParser(description="Signal Quality Analyzer")
    parser.add_argument("--symbol", help="Analyze specific symbol (e.g., GBPUSD)")
    parser.add_argument("--days", type=int, default=0, help="Limit to last N days (0=all)")
    args = parser.parse_args()

    runners = RUNNERS
    if args.symbol:
        runners = [r for r in RUNNERS if r["symbol"].upper() == args.symbol.upper()]
        if not runners:
            print(f"Unknown symbol: {args.symbol}")
            return

    print("=" * 70)
    period = f"last {args.days} days" if args.days else "all time"
    print(f"  Signal Quality Analysis -- {period}")
    print("=" * 70)

    for runner in runners:
        r = analyze_runner(runner, args.days)
        print(f"\n  {'=' * 60}")
        print(f"  {r['name']} ({r['symbol']})")
        print(f"  {'=' * 60}")

        if r["total_signals"] == 0:
            print("    No signals recorded yet.")
            continue

        print(f"    Signals: {r['total_signals']} | Entries: {r['entries']} | Trigger rate: {r['trigger_rate']}%")
        print(f"    Days: {r['days_covered']} | Signals/day: {r['signals_per_day']} | Entries/day: {r['entries_per_day']}")

        # Direction split
        if r["direction_split"]:
            parts = [f"{d}: {c}" for d, c in r["direction_split"].items()]
            print(f"    Direction: {' | '.join(parts)}")

        # Session breakdown
        print(f"\n    Session Breakdown:")
        print(f"    {'Session':>8s}  {'Signals':>8s}  {'Entries':>8s}  {'Trigger%':>8s}")
        for sess, data in r["session_breakdown"].items():
            if data["signals"] > 0:
                print(f"    {sess:>8s}  {data['signals']:>8d}  {data['entries']:>8d}  {data['trigger_rate']:>7.1f}%")

        # Hourly heatmap (text)
        print(f"\n    Hourly Entry Heatmap:")
        h_entries = r["hourly_distribution"].get("entries", {})
        if h_entries:
            max_e = max(h_entries.values()) if h_entries else 1
            for h in range(24):
                count = h_entries.get(h, 0)
                bar = "#" * int(count / max(max_e, 1) * 30) if count > 0 else ""
                if count > 0:
                    print(f"    {h:02d}:00  {count:>3d}  {bar}")

        # Feature comparison
        print(f"\n    Feature Stats (all signals vs entries):")
        print(f"    {'Feature':>12s}  {'All Mean':>10s}  {'Entry Mean':>10s}  {'All Range':>20s}")
        for feat, stats in r["feature_stats"].items():
            entry_mean = f"{stats['entry_mean']:.6f}" if stats["entry_mean"] is not None else "N/A"
            print(f"    {feat:>12s}  {stats['all_mean']:>10.6f}  {entry_mean:>10s}  [{stats['all_min']:.6f}, {stats['all_max']:.6f}]")

    print(f"\n{'=' * 70}")


if __name__ == "__main__":
    main()
