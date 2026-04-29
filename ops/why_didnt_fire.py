"""why_didnt_fire — diagnostic for "I expected strategy X to fire and it didn't."

Walks the available evidence trail (runner.log, signals.csv, heartbeat.json,
launch_stderr.log) for a strategy + optional time window and reports what the
runner was doing — was it alive? did it evaluate? what did the gate say? did
the order block? Turns "huh, multi_orb didn't fire today" into a concrete
diagnosis instead of a guessing game.

Usage:
    cd c:/Argus/repo
    C:/Argus/.venv/Scripts/python.exe -m ops.why_didnt_fire <strategy> [--at YYYY-MM-DDTHH:MM] [--around-min N]

Examples:
    # What was multi_orb doing all day?
    python -m ops.why_didnt_fire forge_multi_orb

    # Was vix_intraday alive at 14:30 UTC today, +/- 30 min?
    python -m ops.why_didnt_fire forge_vix_intraday --at 2026-04-29T14:30 --around-min 30
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


# Mirrors STRATEGY_REGISTRY in ops/operational_vetting.py — single-source-of-truth
# would be nicer but this is a 1-file diagnostic and registry rarely changes.
STRATEGY_LOG_DIRS: dict[str, str] = {
    "argus_usdjpy": "argus_flow/logs/usdjpy",
    "argus_gbpusd": "argus_flow/logs/gbpusd",
    "argus_cadjpy": "argus_flow/logs/cadjpy",
    "forge_gdx_gld": "forge/logs/gdx_gld",
    "forge_gld_pm_long": "forge/logs/gld_pm_long",
    "forge_jpy_pm_short": "forge/logs/jpy_pm_short",
    "forge_nq_overnight": "forge/logs/nq_overnight",
    "forge_nq_london_close": "forge/logs/nq_london_close",
    "forge_aud_asian_breakout": "forge/logs/aud_asian_breakout",
    "forge_spy_mean_rev": "forge/logs/spy_mean_rev",
    "forge_multi_orb": "forge/logs/multi_orb",
    "forge_vix_intraday": "forge/logs/vix_intraday",
    "forge_mamba": "forge/logs/mamba",
    "forge_tori": "forge/logs/tori",
    "forge_cuebanks": "forge/logs/cuebanks",
    "forge_vix_revert": "forge/logs/vix_revert",
    "forge_rebalance": "forge/logs/rebalance",
    "forge_wick_gbpusd": "forge/logs/wick_gbpusd",
    "forge_fomc_drift": "forge/logs/fomc_drift",
    "forge_tom_international": "forge/logs/tom_international",
    "forge_atlas": "forge/logs/atlas",
    "forge_themis": "forge/logs/themis",
    "apollo": "apollo/logs",
    "hermes": "hermes/logs",
    "titan": "titan/logs",
}


def _try_parse_ts(s: str) -> datetime | None:
    """Best-effort parse of various log timestamp formats."""
    if not s:
        return None
    s = s.strip()
    # Try ISO format first (handles Z + tz-aware)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass
    # Try common log format: "YYYY-MM-DD HH:MM:SS,mmm" or "YYYY-MM-DDTHH:MM:SSZ"
    for fmt in ("%Y-%m-%d %H:%M:%S,%f", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _filter_window(lines: list[tuple[datetime | None, str]],
                   anchor: datetime | None,
                   around_min: int) -> list[tuple[datetime | None, str]]:
    """Filter (ts, line) pairs to the [anchor - around_min, anchor + around_min] window.
    If anchor is None, return all lines (full-day view)."""
    if anchor is None:
        return lines
    lo = anchor - timedelta(minutes=around_min)
    hi = anchor + timedelta(minutes=around_min)
    return [(ts, ln) for ts, ln in lines if ts is None or (lo <= ts <= hi)]


def _read_runner_log(log_dir: Path, anchor: datetime | None, around_min: int) -> list[tuple[datetime | None, str]]:
    """Pull lines from runner.log + <strategy>.log + launch_stderr.log."""
    candidates = [
        log_dir / "runner.log",
        log_dir / f"{log_dir.name}.log",  # e.g. rebalance.log
        log_dir / "launch_stderr.log",
    ]
    out: list[tuple[datetime | None, str]] = []
    for p in candidates:
        if not p.exists():
            continue
        try:
            with open(p, encoding="utf-8", errors="ignore") as f:
                # Tail — read up to 10K lines to cap memory
                lines = f.readlines()[-10000:]
            for ln in lines:
                ln = ln.rstrip()
                if not ln:
                    continue
                # Try to extract a leading timestamp
                m = re.match(r"^(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}[.,]?\d*Z?)", ln)
                ts = _try_parse_ts(m.group(1)) if m else None
                out.append((ts, f"[{p.name}] {ln}"))
        except Exception as e:
            out.append((None, f"[{p.name}] ERROR reading file: {e}"))
    return _filter_window(out, anchor, around_min)


def _read_signals(log_dir: Path, anchor: datetime | None, around_min: int) -> list[dict]:
    """Pull signals.csv (or <short>_signals.csv) entries within the window."""
    short = log_dir.name
    candidates = [
        log_dir / "signals.csv",
        log_dir / f"{short}_signals.csv",
    ]
    sig_path = next((p for p in candidates if p.exists()), None)
    if sig_path is None:
        return []
    try:
        with open(sig_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception:
        return []
    # Pick the first column that looks like a timestamp
    if not rows:
        return []
    ts_keys = ["ts", "timestamp", "entry_ts", "signal_ts"]
    ts_key = next((k for k in ts_keys if k in rows[0]), None)
    if ts_key is None:
        return rows[-50:]  # fallback: last 50 rows if we can't identify the ts column
    if anchor is None:
        return rows[-100:]
    lo = anchor - timedelta(minutes=around_min)
    hi = anchor + timedelta(minutes=around_min)
    in_window = []
    for r in rows:
        ts = _try_parse_ts(r.get(ts_key, ""))
        if ts is None:
            continue
        if lo <= ts <= hi:
            in_window.append(r)
    return in_window


def _read_heartbeat(log_dir: Path) -> dict | None:
    p = log_dir / "heartbeat.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _strategy_fills_in_window(strategy: str, anchor: datetime | None, around_min: int) -> list[dict]:
    """Read canonical_fills.jsonl + per-strategy trades.csv, return any closed
    trades for the strategy in the window."""
    fills_path = REPO / "argus_flow" / "logs" / "canonical_fills.jsonl"
    if not fills_path.exists():
        return []
    out = []
    try:
        with open(fills_path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("strategy") != strategy:
                    continue
                ts = _try_parse_ts(r.get("entry_ts", ""))
                if ts is None:
                    continue
                if anchor is None:
                    out.append(r)
                    continue
                if abs((ts - anchor).total_seconds()) <= around_min * 60:
                    out.append(r)
    except Exception:
        pass
    return out[-20:]  # cap


def diagnose(strategy: str, anchor: datetime | None, around_min: int) -> dict:
    if strategy not in STRATEGY_LOG_DIRS:
        return {"status": "unknown_strategy",
                "message": f"Strategy '{strategy}' not in registry. Known: {sorted(STRATEGY_LOG_DIRS.keys())}"}
    log_dir = REPO / STRATEGY_LOG_DIRS[strategy]
    if not log_dir.exists():
        return {"status": "missing_log_dir", "message": f"log dir not found: {log_dir}"}

    hb = _read_heartbeat(log_dir)
    runner_lines = _read_runner_log(log_dir, anchor, around_min)
    signals = _read_signals(log_dir, anchor, around_min)
    fills = _strategy_fills_in_window(strategy, anchor, around_min)

    # Verdict heuristics
    verdict = "unknown"
    explanation = []
    if hb is None:
        verdict = "NO_HEARTBEAT"
        explanation.append("No heartbeat.json — runner may have never started or crashed during init.")
    else:
        hb_ts = _try_parse_ts(hb.get("ts") or hb.get("timestamp", ""))
        if hb_ts:
            age_min = (datetime.now(timezone.utc) - hb_ts).total_seconds() / 60
            if age_min > 60:
                verdict = "STALE_HEARTBEAT"
                explanation.append(f"Heartbeat is {age_min:.1f} min old — runner likely down or off-cycle.")
            else:
                explanation.append(f"Heartbeat fresh ({age_min:.1f} min ago, mode={hb.get('mode', '?')}).")
        if hb.get("mode") == "research_only":
            verdict = "RESEARCH_ONLY"
            explanation.append("Mode is research_only — strategy intentionally does not submit trades.")

    if fills:
        verdict = "FIRED"
        explanation.append(f"Strategy DID fire in window: {len(fills)} fills.")
    elif signals:
        # Look for verdict-style columns
        if any("verdict" in (r.get("verdict", "") + r.get("status", "")).lower() for r in signals):
            verdict_counts: dict[str, int] = {}
            for r in signals:
                v = r.get("verdict") or r.get("status") or "(none)"
                verdict_counts[v] = verdict_counts.get(v, 0) + 1
            if verdict == "unknown":
                verdict = "GATED"
            explanation.append("Signal evaluations in window: " + ", ".join(f"{k}={v}" for k, v in verdict_counts.items()))
        else:
            if verdict == "unknown":
                verdict = "EVALUATED"
            explanation.append(f"{len(signals)} signal rows in window (no verdict column to summarize).")

    if runner_lines and verdict == "unknown":
        # No fills, no signals — but log activity → strategy was alive but didn't reach the gate
        verdict = "ALIVE_NO_FIRE"
        explanation.append(f"{len(runner_lines)} log lines in window but no signals or fills — "
                           f"strategy was running but the gate didn't trip. "
                           f"Likely: regime filter, session window, or no-trigger condition.")

    if not runner_lines and not signals and not fills:
        if verdict == "unknown":
            verdict = "NO_EVIDENCE"
        explanation.append("No log/signal/fill activity in the requested window.")

    return {
        "status": "ok",
        "strategy": strategy,
        "anchor_utc": anchor.isoformat() if anchor else None,
        "around_min": around_min,
        "log_dir": str(log_dir.relative_to(REPO)),
        "verdict": verdict,
        "explanation": explanation,
        "heartbeat": hb,
        "fills_in_window": fills,
        "signals_in_window": signals[:20],  # cap output
        "runner_log_tail": [(ts.isoformat() if ts else None, ln) for ts, ln in runner_lines[-30:]],
    }


def _print_report(diag: dict) -> None:
    if diag.get("status") != "ok":
        print(f"ERROR: {diag.get('message', diag)}")
        return
    print("=" * 80)
    print(f"  WHY DIDN'T {diag['strategy']} FIRE?")
    if diag.get("anchor_utc"):
        print(f"  Window: {diag['anchor_utc']} +/- {diag['around_min']} min")
    else:
        print(f"  Window: full available log tail")
    print("=" * 80)
    print()
    print(f"VERDICT: {diag['verdict']}")
    print()
    for line in diag["explanation"]:
        print(f"  - {line}")
    print()
    hb = diag.get("heartbeat") or {}
    if hb:
        print(f"Heartbeat:")
        print(f"  ts:           {hb.get('ts') or hb.get('timestamp')}")
        print(f"  mode:         {hb.get('mode')}")
        print(f"  status:       {hb.get('status')}")
        print(f"  position:     {hb.get('position') or hb.get('open_trade_count')}")
        print()
    fills = diag.get("fills_in_window") or []
    if fills:
        print(f"Fills in window ({len(fills)}):")
        for r in fills[:5]:
            print(f"  - entry={r.get('entry_ts','?')} exit={r.get('exit_ts','?')} "
                  f"pnl=${r.get('pnl_usd', 0):+.2f} reason={r.get('exit_reason', '?')}")
        print()
    signals = diag.get("signals_in_window") or []
    if signals:
        print(f"Signal rows in window ({len(signals)}, showing first 5):")
        for r in signals[:5]:
            # Compact one-liner of the row's interesting fields
            key_fields = ["ts", "timestamp", "entry_ts", "verdict", "status",
                          "direction", "score", "reason", "factors"]
            parts = [f"{k}={r[k]!s:.40}" for k in key_fields if k in r and r[k]]
            print(f"  - {' | '.join(parts)}")
        print()
    runner_tail = diag.get("runner_log_tail") or []
    if runner_tail:
        print(f"Runner log tail in window ({len(runner_tail)}, last 10 shown):")
        for ts, line in runner_tail[-10:]:
            tsfmt = ts[:19] if ts else "?"
            print(f"  {tsfmt}  {line[:140]}")
        print()
    print("=" * 80)


def main() -> int:
    ap = argparse.ArgumentParser(description="Why didn't strategy X fire?")
    ap.add_argument("strategy", help="Strategy name (e.g. forge_multi_orb)")
    ap.add_argument("--at", help="Anchor timestamp UTC (ISO format, e.g. 2026-04-29T14:30)")
    ap.add_argument("--around-min", type=int, default=60, help="Window minutes (default 60)")
    ap.add_argument("--json", action="store_true", help="Output raw JSON instead of formatted report")
    args = ap.parse_args()

    anchor = _try_parse_ts(args.at) if args.at else None
    if args.at and anchor is None:
        print(f"ERROR: could not parse --at {args.at!r} (try ISO format like 2026-04-29T14:30)")
        return 2

    diag = diagnose(args.strategy, anchor, args.around_min)
    if args.json:
        print(json.dumps(diag, indent=2, default=str))
    else:
        _print_report(diag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
