"""5/15 mid-cycle review — pre-ceremony decision matrix.

Prints per-strategy state vs the kill/promote/hold criteria locked in
project_2026_05_07_week_audit.md (the 4-agent post-mortem of 4/30->5/7).

The audit's 5/15 decision rules:

  PROMOTE (paper factor 1.0x -> 1.5x):
    vix_intraday    -> IF n>=35 AND PF>=1.05

  HOLD paper (continue accumulating):
    gld_pm_long, nq_overnight, spy_trend_follower

  KILL (stop runner):
    multi_orb       -> IF post-revert PF<1.0
    cadjpy, gbpusd  -> IF still 0 fills
    cuebanks/tori/mamba -> IF still 0 signals

Run weekly leading up to 5/15 to see the trajectory; run on 5/15 to make
the call. Output is a single console table — no JSON, no files.

    cd c:/Argus/repo
    C:/Argus/.venv/Scripts/python.exe -m ops.run_5_15_prep
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Window starts at the 4/30 paper-account reset (per audit).
WINDOW_START = datetime(2026, 4, 30, 0, 0, tzinfo=timezone.utc)

# Per-strategy: (csv_path, ts_column, valid_filter_required)
SOURCES: dict[str, tuple[str, str, bool]] = {
    "argus_usdjpy":               ("argus_flow/logs/usdjpy/trades.csv",  "ts", True),
    "argus_gbpusd":               ("argus_flow/logs/gbpusd/trades.csv",  "ts", True),
    "argus_cadjpy":               ("argus_flow/logs/cadjpy/trades.csv",  "ts", True),
    "forge_multi_orb":            ("forge/logs/multi_orb/trades.csv",            "ts", False),
    "forge_vix_intraday":         ("forge/logs/vix_intraday/trades.csv",         "ts", False),
    "forge_gld_pm_long":          ("forge/logs/gld_pm_long/trades.csv",          "ts", False),
    "forge_nq_overnight":         ("forge/logs/nq_overnight/trades.csv",         "ts", False),
    "forge_nq_london_close":      ("forge/logs/nq_london_close/trades.csv",      "ts", False),
    "forge_aud_asian_breakout":   ("forge/logs/aud_asian_breakout/trades.csv",   "ts", False),
    "forge_jpy_pm_short":         ("forge/logs/jpy_pm_short/trades.csv",         "ts", False),
    "forge_wick_gbpusd":          ("forge/logs/wick_gbpusd/trades.csv",          "ts", False),
    "forge_fomc_drift":           ("forge/logs/fomc_drift/trades.csv",           "ts", False),
    "forge_tom_international":    ("forge/logs/tom_international/trades.csv",    "ts", False),
    "forge_vix_revert":           ("forge/logs/vix_revert/trades.csv",           "ts", False),
    "forge_gdx_gld":              ("forge/logs/gdx_gld/trades.csv",              "exit_date", False),
    "forge_spy_trend_follower":   ("forge/logs/spy_trend_follower/trades.csv",   "ts", False),
    "forge_cuebanks":             ("forge/logs/cuebanks/trades.csv",             "ts", False),
    "forge_tori":                 ("forge/logs/tori/trades.csv",                 "ts", False),
    "forge_mamba":                ("forge/logs/mamba/trades.csv",                "ts", False),
}

# Decision criteria keyed by strategy. Tuple is (rule_label, eval_fn(stats) -> verdict).
def _verdict_vix_intraday(s: dict) -> str:
    if s["n"] >= 35 and s["pf"] >= 1.05:
        return "PROMOTE: 1.0x -> 1.5x"
    return f"HOLD: need n>=35 (have {s['n']}) AND PF>=1.05 (have {s['pf']:.2f})"

def _verdict_multi_orb(s: dict) -> str:
    # Window was reverted 32->24 on 5/7; trades after that are the test.
    if s["n"] < 5:
        return f"INSUFFICIENT: only {s['n']} trades since 4/30 (need >=5 to judge revert)"
    if s["pf"] >= 1.0:
        return f"HOLD: revert recovering (PF {s['pf']:.2f})"
    return f"KILL: revert failed (PF {s['pf']:.2f} < 1.0 over n={s['n']})"

def _verdict_dead_argus(s: dict) -> str:
    if s["n"] == 0:
        return "KILL: 0 fills since 4/30"
    return f"INVESTIGATE: {s['n']} fills (was supposed to be 0 — gate fix may have worked)"

def _verdict_dead_silent(s: dict) -> str:
    if s["n"] == 0:
        return "KILL: 0 signals since 4/30 (launch-flag fix didn't unblock)"
    return f"INVESTIGATE: {s['n']} fills - unblocked"

def _verdict_hold_accumulate(s: dict) -> str:
    if s["n"] == 0:
        return f"HOLD: 0 trades yet"
    return f"HOLD: n={s['n']}, PF={s['pf']:.2f}, PnL=${s['pnl']:+.0f}"

def _verdict_observe(s: dict) -> str:
    return f"OBSERVE: n={s['n']}, PF={s['pf']:.2f}, PnL=${s['pnl']:+.0f}"


VERDICTS: dict[str, callable] = {
    "forge_vix_intraday":       _verdict_vix_intraday,
    "forge_multi_orb":          _verdict_multi_orb,
    "argus_cadjpy":             _verdict_dead_argus,
    "argus_gbpusd":             _verdict_dead_argus,
    "argus_usdjpy":             _verdict_dead_argus,
    "forge_cuebanks":           _verdict_dead_silent,
    "forge_tori":               _verdict_dead_silent,
    "forge_mamba":              _verdict_dead_silent,
    "forge_gld_pm_long":        _verdict_hold_accumulate,
    "forge_nq_overnight":       _verdict_hold_accumulate,
    "forge_spy_trend_follower": _verdict_hold_accumulate,
}


def _parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s.split(".")[0], fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _stats_for(strategy: str, csv_path: str, ts_col: str, valid_required: bool) -> dict:
    p = REPO / csv_path
    stats = {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0, "pf": 0.0,
             "win_rate": 0.0, "exists": p.exists()}
    if not p.exists():
        return stats
    wins_sum = 0.0
    losses_sum = 0.0
    try:
        with open(p, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if valid_required and str(r.get("experiment_valid", "")).lower() != "true":
                    continue
                ts = _parse_ts(r.get(ts_col) or r.get("entry_ts") or r.get("entry_date") or "")
                if ts is None or ts < WINDOW_START:
                    continue
                pnl_raw = r.get("pnl_usd")
                if pnl_raw in (None, ""):
                    continue
                try:
                    pnl = float(pnl_raw)
                except (TypeError, ValueError):
                    continue
                stats["n"] += 1
                stats["pnl"] += pnl
                if pnl > 0:
                    stats["wins"] += 1
                    wins_sum += pnl
                elif pnl < 0:
                    stats["losses"] += 1
                    losses_sum += abs(pnl)
    except Exception:
        return stats
    if stats["n"] > 0:
        stats["win_rate"] = stats["wins"] / stats["n"]
    if losses_sum > 0:
        stats["pf"] = wins_sum / losses_sum
    elif wins_sum > 0:
        stats["pf"] = float("inf")
    return stats


def main() -> None:
    print("=" * 96)
    print(f"5/15 MID-CYCLE REVIEW - window starts {WINDOW_START.date()}")
    print(f"now = {datetime.now(timezone.utc).isoformat()}")
    print("=" * 96)
    print(f"{'STRATEGY':<30} {'N':>4} {'WIN%':>6} {'PF':>7} {'PnL':>10}  VERDICT")
    print("-" * 96)

    rows = []
    for strat, (path, ts_col, valid) in SOURCES.items():
        stats = _stats_for(strat, path, ts_col, valid)
        verdict_fn = VERDICTS.get(strat, _verdict_observe)
        verdict = verdict_fn(stats)
        rows.append((strat, stats, verdict))

    # Sort: KILL first (urgent), then PROMOTE, then HOLD/OBSERVE
    def _rank(v: str) -> int:
        if v.startswith("KILL"): return 0
        if v.startswith("PROMOTE"): return 1
        if v.startswith("INVESTIGATE"): return 2
        if v.startswith("HOLD"): return 3
        return 4
    rows.sort(key=lambda r: _rank(r[2]))

    for strat, s, v in rows:
        pf_str = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "  inf"
        print(f"{strat:<30} {s['n']:>4} {s['win_rate']*100:>5.0f}% "
              f"{pf_str:>7} ${s['pnl']:>+9.0f}  {v}")

    print("-" * 96)
    print("Decision day: 2026-05-15. Re-run this in the days leading up to see trajectory.")


if __name__ == "__main__":
    main()
