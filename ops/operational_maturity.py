"""ops/operational_maturity.py — Per-strategy operational maturity report.

The 5/1 deliverable (foundation for validating whether live edge matches
backtest edge). Runs nightly, produces one JSON + one markdown report
per-day, emits a per-strategy verdict:

    INSUFFICIENT_DATA — <10 live trades (any verdict is noise)
    EMERGING          — 10+ trades, live PF within 75% of backtest PF
    VALIDATED         — 30+ trades, live PF >= backtest PF × 0.90
    DEGRADED          — >=10 trades AND live PF <= backtest PF × 0.60
    WAITING           — 0 live trades (strategy is running but hasn't fired)

Output:
    argus_flow/logs/operational_maturity_latest.json
    argus_flow/logs/operational_maturity_latest.md
    argus_flow/logs/operational_maturity_YYYYMMDD.json  (dated archive)

Run:
    python -m ops.operational_maturity             # writes report
    python -m ops.operational_maturity --stdout    # also prints markdown

The post-clamp cutoff (2026-04-23T14:00:00Z by default) filters out all
pre-reset bug-sized trades so verdicts reflect honest sizing only.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
LOGS_DIR = REPO / "argus_flow" / "logs"
POST_CLAMP_CUTOFF = datetime(2026, 4, 23, 14, 0, tzinfo=timezone.utc)  # reset day

# Strategies to track. Each entry maps strategy_id -> (csv_path, ts_col, backtest_pf).
# backtest_pf is the published expectation; live_pf will be compared against it.
# Backtest values match dashboard strategy_performance artifact sources.
STRATEGIES: list[dict[str, Any]] = [
    {"id": "argus_usdjpy",        "csv": "argus_flow/logs/usdjpy/trades.csv",         "ts_col": "ts",        "pnl_col": "pnl_usd",  "bt_pf": 1.20, "valid_filter": True},
    {"id": "argus_gbpusd",        "csv": "argus_flow/logs/gbpusd/trades.csv",         "ts_col": "ts",        "pnl_col": "pnl_usd",  "bt_pf": 1.20, "valid_filter": True},
    {"id": "argus_cadjpy",        "csv": "argus_flow/logs/cadjpy/trades.csv",         "ts_col": "ts",        "pnl_col": "pnl_usd",  "bt_pf": 1.01, "valid_filter": True},
    {"id": "forge_gdx_gld",       "csv": "forge/logs/gdx_gld/trades.csv",             "ts_col": "exit_date", "pnl_col": "pnl_usd",  "bt_pf": 1.56, "valid_filter": False},
    {"id": "forge_gld_pm_long",   "csv": "forge/logs/gld_pm_long/trades.csv",         "ts_col": "ts",        "pnl_col": "pnl_usd",  "bt_pf": 1.73, "valid_filter": False},
    {"id": "forge_jpy_pm_short",  "csv": "forge/logs/jpy_pm_short/trades.csv",        "ts_col": "ts",        "pnl_col": "pnl_usd",  "bt_pf": 1.40, "valid_filter": False},
    {"id": "forge_nq_overnight",  "csv": "forge/logs/nq_overnight/trades.csv",        "ts_col": "ts",        "pnl_col": "pnl_usd",  "bt_pf": 1.30, "valid_filter": False},
    {"id": "forge_spy_mean_rev",  "csv": "forge/logs/spy_mean_rev/trades.csv",        "ts_col": "ts",        "pnl_col": "pnl_usd",  "bt_pf": 1.45, "valid_filter": False},
    {"id": "forge_multi_orb",     "csv": "forge/logs/multi_orb/trades.csv",           "ts_col": "ts",        "pnl_col": "pnl_usd",  "bt_pf": 1.25, "valid_filter": False},
    {"id": "forge_vix_intraday",  "csv": "forge/logs/vix_intraday/trades.csv",        "ts_col": "ts",        "pnl_col": "pnl_usd",  "bt_pf": 1.30, "valid_filter": False},
    {"id": "forge_nq_london_close",    "csv": "forge/logs/nq_london_close/trades.csv",    "ts_col": "ts", "pnl_col": "pnl_usd", "bt_pf": 1.20, "valid_filter": False},
    {"id": "forge_aud_asian_breakout", "csv": "forge/logs/aud_asian_breakout/trades.csv", "ts_col": "ts", "pnl_col": "pnl_usd", "bt_pf": 1.20, "valid_filter": False},
    {"id": "forge_mamba",         "csv": "forge/logs/mamba/trades.csv",               "ts_col": "ts",        "pnl_col": "pnl_usd",  "bt_pf": 0.68, "valid_filter": False},
    {"id": "forge_tori",          "csv": "forge/logs/tori/trades.csv",                "ts_col": "ts",        "pnl_col": "pnl_usd",  "bt_pf": 0.61, "valid_filter": False},
    {"id": "forge_cuebanks",      "csv": "forge/logs/cuebanks/trades.csv",            "ts_col": "ts",        "pnl_col": "pnl_usd",  "bt_pf": 0.89, "valid_filter": False},
    {"id": "forge_vix_revert",    "csv": "forge/logs/vix_revert/trades.csv",          "ts_col": "ts",        "pnl_col": "pnl_usd",  "bt_pf": 2.40, "valid_filter": False},
    {"id": "forge_rebalance",     "csv": "forge/logs/rebalance/trades.csv",           "ts_col": "ts",        "pnl_col": "pnl_usd",  "bt_pf": 6.65, "valid_filter": False},
    {"id": "forge_wick_gbpusd",   "csv": "forge/logs/wick_gbpusd/trades.csv",         "ts_col": "ts",        "pnl_col": "pnl_usd",  "bt_pf": 1.50, "valid_filter": False},
    {"id": "apollo",              "csv": "apollo/logs/trades.csv",                    "ts_col": "ts",        "pnl_col": "pnl_usd",  "bt_pf": 2.00, "valid_filter": False},
    {"id": "hermes",              "csv": "hermes/logs/trades.csv",                    "ts_col": "ts",        "pnl_col": "pnl_usd",  "bt_pf": 1.11, "valid_filter": False},
    {"id": "titan",               "csv": "titan/logs/trades.csv",                     "ts_col": "ts",        "pnl_col": "pnl_usd",  "bt_pf": 1.52, "valid_filter": False},
    {"id": "forge_fomc_drift",    "csv": "forge/logs/fomc_drift/trades.csv",          "ts_col": "entry_ts",  "pnl_col": "pnl_usd",  "bt_pf": 1.58, "valid_filter": False},
    {"id": "forge_tom_international", "csv": "forge/logs/tom_international/trades.csv", "ts_col": "entry_ts", "pnl_col": "pnl_usd", "bt_pf": 1.31, "valid_filter": False},
]


@dataclass
class StrategyReport:
    strategy: str
    live_trades: int = 0
    live_wins: int = 0
    live_losses: int = 0
    live_pf: float | None = None
    live_wr: float | None = None
    live_expectancy_usd: float | None = None
    live_total_pnl_usd: float = 0.0
    backtest_pf: float | None = None
    drift_ratio: float | None = None  # live_pf / backtest_pf
    verdict: str = "INSUFFICIENT_DATA"
    verdict_reason: str = ""
    sample_window_start: str = ""
    sample_window_end: str = ""


def _parse_ts(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    # Try date-only format (forge_gdx_gld uses exit_date)
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S%z"):
        try:
            dt = datetime.strptime(raw.strip(), fmt)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except Exception:
            continue
    return None


def compute_strategy_report(spec: dict, cutoff: datetime) -> StrategyReport:
    """Load trades.csv for one strategy, filter to post-cutoff, compute stats + verdict."""
    rep = StrategyReport(strategy=spec["id"])
    rep.backtest_pf = spec["bt_pf"]
    csv_path = REPO / spec["csv"]
    if not csv_path.exists():
        rep.verdict = "WAITING"
        rep.verdict_reason = f"no trades.csv at {spec['csv']}"
        return rep

    try:
        with csv_path.open(encoding="utf-8") as f:
            all_rows = list(csv.DictReader(f))
    except Exception as e:
        rep.verdict = "INSUFFICIENT_DATA"
        rep.verdict_reason = f"csv read error: {e}"
        return rep

    if spec.get("valid_filter"):
        all_rows = [r for r in all_rows if str(r.get("experiment_valid", "")).lower() == "true"]

    post_rows = []
    earliest_ts = None
    latest_ts = None
    for r in all_rows:
        ts = _parse_ts(r.get(spec["ts_col"], "") or "")
        if ts is None:
            continue
        if ts < cutoff:
            continue
        post_rows.append((ts, r))
        if earliest_ts is None or ts < earliest_ts:
            earliest_ts = ts
        if latest_ts is None or ts > latest_ts:
            latest_ts = ts

    rep.live_trades = len(post_rows)
    if earliest_ts:
        rep.sample_window_start = earliest_ts.isoformat()
    if latest_ts:
        rep.sample_window_end = latest_ts.isoformat()

    pnls = []
    for ts, r in post_rows:
        raw = r.get(spec["pnl_col"])
        if raw in (None, ""):
            continue
        try:
            pnls.append(float(raw))
        except ValueError:
            continue

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    rep.live_wins = len(wins)
    rep.live_losses = len(losses)
    rep.live_total_pnl_usd = round(sum(pnls), 2)
    if pnls:
        rep.live_expectancy_usd = round(sum(pnls) / len(pnls), 2)
        if losses:
            rep.live_pf = round(sum(wins) / -sum(losses), 3) if sum(losses) != 0 else None
        else:
            rep.live_pf = float("inf") if wins else None
        if rep.live_trades > 0:
            rep.live_wr = round(rep.live_wins / rep.live_trades * 100, 1)

    # Verdict gate
    n = rep.live_trades
    bt = rep.backtest_pf or 0
    lp = rep.live_pf

    if n == 0:
        rep.verdict = "WAITING"
        rep.verdict_reason = "no post-cutoff trades yet"
    elif n < 10:
        rep.verdict = "INSUFFICIENT_DATA"
        rep.verdict_reason = f"only {n} post-cutoff trades; need 10+ to form a verdict"
    else:
        if lp is None or lp == float("inf"):
            ratio = None
        else:
            ratio = round(lp / bt, 3) if bt > 0 else None
            rep.drift_ratio = ratio

        if n >= 30 and ratio is not None and ratio >= 0.90:
            rep.verdict = "VALIDATED"
            rep.verdict_reason = f"n={n}, live_pf={lp}, backtest_pf={bt}, ratio={ratio} (>= 0.90)"
        elif n >= 10 and ratio is not None and ratio <= 0.60:
            rep.verdict = "DEGRADED"
            rep.verdict_reason = f"n={n}, live_pf={lp}, backtest_pf={bt}, ratio={ratio} (<= 0.60)"
        elif n >= 10:
            rep.verdict = "EMERGING"
            rep.verdict_reason = f"n={n}, live_pf={lp}, backtest_pf={bt}, ratio={ratio if ratio is not None else 'N/A'}"
        else:
            rep.verdict = "INSUFFICIENT_DATA"
            rep.verdict_reason = f"n={n} below 10-trade bar"

    return rep


def build_report(cutoff: datetime) -> dict:
    reports = [compute_strategy_report(spec, cutoff) for spec in STRATEGIES]
    totals = {
        "strategies_count": len(reports),
        "total_live_trades": sum(r.live_trades for r in reports),
        "verdict_counts": {},
        "total_live_pnl_usd": round(sum(r.live_total_pnl_usd for r in reports), 2),
    }
    for r in reports:
        totals["verdict_counts"][r.verdict] = totals["verdict_counts"].get(r.verdict, 0) + 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "post_clamp_cutoff": cutoff.isoformat(),
        "totals": totals,
        "strategies": [asdict(r) for r in reports],
    }


def to_markdown(report: dict) -> str:
    lines = [
        f"# Operational Maturity — {report['generated_at'][:10]}",
        f"",
        f"Post-clamp cutoff: **{report['post_clamp_cutoff']}** (all pre-reset trades excluded from honest-sizing view)",
        f"",
        f"## Summary",
        f"",
        f"- Strategies tracked: **{report['totals']['strategies_count']}**",
        f"- Total live trades (post-cutoff): **{report['totals']['total_live_trades']}**",
        f"- Total live PnL: **${report['totals']['total_live_pnl_usd']:,.2f}**",
        f"- Verdict distribution: {report['totals']['verdict_counts']}",
        f"",
        f"## Per-strategy",
        f"",
        f"| Strategy | Verdict | N | WR | Live PF | BT PF | Drift | PnL (USD) | Notes |",
        f"|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for s in report["strategies"]:
        wr = f"{s['live_wr']}%" if s["live_wr"] is not None else "—"
        live_pf = "∞" if s["live_pf"] == float("inf") else (f"{s['live_pf']:.2f}" if s["live_pf"] is not None else "—")
        bt_pf = f"{s['backtest_pf']:.2f}" if s["backtest_pf"] is not None else "—"
        drift = f"{s['drift_ratio']:.2f}" if s["drift_ratio"] is not None else "—"
        pnl = f"${s['live_total_pnl_usd']:,.2f}"
        lines.append(
            f"| {s['strategy']} | **{s['verdict']}** | {s['live_trades']} | {wr} | {live_pf} | {bt_pf} | {drift} | {pnl} | {s['verdict_reason']} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutoff", help="ISO timestamp for post-clamp cutoff (default: 2026-04-23T14:00:00Z)")
    parser.add_argument("--stdout", action="store_true", help="Also print markdown to stdout")
    args = parser.parse_args(argv)

    cutoff = POST_CLAMP_CUTOFF
    if args.cutoff:
        parsed = _parse_ts(args.cutoff)
        if parsed:
            cutoff = parsed

    report = build_report(cutoff)

    # Write latest + dated
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    json_latest = LOGS_DIR / "operational_maturity_latest.json"
    json_dated  = LOGS_DIR / f"operational_maturity_{stamp}.json"
    md_latest   = LOGS_DIR / "operational_maturity_latest.md"
    md_dated    = LOGS_DIR / f"operational_maturity_{stamp}.md"

    json_latest.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    json_dated.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    md = to_markdown(report)
    md_latest.write_text(md, encoding="utf-8")
    md_dated.write_text(md, encoding="utf-8")

    if args.stdout:
        print(md)

    print(f"[maturity] wrote: {json_latest.name}, {json_dated.name}, {md_latest.name}, {md_dated.name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
