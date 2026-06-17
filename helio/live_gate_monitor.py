"""Live gate monitor — compares rolling live PF to the disciplined-gate baseline.

Built 2026-05-23 as the operational layer on top of the disciplined gate.
For each live strategy, this module:
    1. Loads recent trades (canonical_fills or strategy-specific trades.csv)
    2. Computes a rolling PF over the last N trades (default 30) and last
       K days (default 90)
    3. Compares to the baseline CI lower bound recorded in
       argus_flow/configs/promotion_gate_baseline.json
    4. Emits a per-strategy status: PASS_GATE / WARNING / FAIL /
       INSUFFICIENT_N / NO_BASELINE

Status thresholds:
    - PASS_GATE     : live_pf >= warning_pct (0.85) * ci_lower
    - WARNING       : fail_pct (0.70) * ci_lower <= live_pf < warning_pct
    - FAIL          : live_pf < fail_pct * ci_lower
    - INSUFFICIENT_N: live trade count < min_n (20)
    - NO_BASELINE   : strategy missing from baseline config

The monitor DOES NOT take any destructive action on its own. Operators
review the status and decide whether to adjust allocation or halt.

Future: a separate `helio/auto_pause.py` will optionally write
HALT.flag based on this monitor's output, gated behind an explicit
operator opt-in flag.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd


_REPO = Path(__file__).resolve().parents[1]
BASELINE_PATH = _REPO / "argus_flow" / "configs" / "promotion_gate_baseline.json"
ARCHIVE_ROOT = _REPO / "argus_flow" / "logs" / "_archive"
LOG_DIR = _REPO / "argus_flow" / "logs"
DEFAULT_REPORT_PATH = LOG_DIR / "cohort_gate_status.json"

# Per-strategy trades.csv locations (canonical_fills is the long-term store
# but each strategy also writes a trades.csv to its log dir)
STRATEGY_LOG_DIRS = {
    "forge_xs_momentum": _REPO / "forge" / "logs" / "xs_momentum",
    "forge_spy_trend_follower": _REPO / "forge" / "logs" / "spy_trend_follower",
    "forge_gld_pm_long": _REPO / "forge" / "logs" / "gld_pm_long",
    "forge_nq_overnight": _REPO / "forge" / "logs" / "nq_overnight",
    "forge_pead": _REPO / "forge" / "logs" / "pead",
}


@dataclass
class GateMonitorStatus:
    """Per-strategy live-gate status snapshot."""
    strategy: str
    verdict: str            # PASS_GATE / WARNING / FAIL / INSUFFICIENT_N / NO_BASELINE
    baseline_ci_lower: Optional[float] = None
    live_pf_30trades: Optional[float] = None
    live_pf_90days: Optional[float] = None
    n_live_trades: int = 0
    last_trade_ts: Optional[str] = None
    pct_of_ci_lower: Optional[float] = None
    notes: str = ""
    backtest_point_pf: Optional[float] = None
    backtest_n: Optional[int] = None
    config: str = ""
    # Data provenance — was the rolling-PF computed from LIVE trades or did
    # we fall back to a pre-reset archive (contaminated epoch)? Default
    # "none" = no trades found at all.
    trade_source: str = "none"   # "live" | "pre_reset_archive" | "none"

    def to_dict(self) -> dict:
        return asdict(self)


def load_baseline(path: Path = BASELINE_PATH) -> dict:
    """Read the disciplined-gate baseline JSON."""
    if not path.exists():
        raise FileNotFoundError(f"baseline not found at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_trades_csv(strategy: str, *,
                     fallback_archive: bool = False
                     ) -> tuple[Optional[pd.DataFrame], str]:
    """Load trades.csv for `strategy`. Returns (df, source_label).

    source_label:
        "live"               — read from forge/logs/<strategy>/trades.csv
        "pre_reset_archive"  — fell back to argus_flow/logs/_archive/pre_reset_*
        "none"               — no file found anywhere

    DEFAULT BEHAVIOR (fallback_archive=False, changed 2026-05-23 PM):
        Returns archive data ONLY when fallback_archive=True is explicitly
        passed. The prior default silently returned pre-reset archive data
        when no live file existed, which caused the disciplined-gate monitor
        to label April 2026 archived trades as "live PF" after the 5/22
        reset emptied canonical_fills. The 30-day clean evidence window
        starts fresh; we DO NOT mix pre-reset evidence into post-reset
        verdicts. Caller must opt in to archive lookup explicitly.
    """
    log_dir = STRATEGY_LOG_DIRS.get(strategy)
    if log_dir is None:
        return (None, "none")
    primary = log_dir / "trades.csv"
    if primary.exists():
        try:
            df = pd.read_csv(primary)
            if len(df) > 0:
                return (df, "live")
        except Exception:
            pass
    if not fallback_archive:
        return (None, "none")
    # Explicit opt-in: walk the pre-reset archives
    name = strategy.replace("forge_", "")
    archives = sorted(ARCHIVE_ROOT.glob(f"pre_reset_*/forge/logs/{name}/trades.csv"),
                      reverse=True)
    for archive in archives:
        try:
            df = pd.read_csv(archive)
            if len(df) > 0:
                return (df, "pre_reset_archive")
        except Exception:
            continue
    return (None, "none")


def _compute_pf(pnls: list[float]) -> Optional[float]:
    """Profit factor. Returns None if no losses (undefined) or no trades."""
    if not pnls:
        return None
    wins = sum(p for p in pnls if p > 0)
    losses = abs(sum(p for p in pnls if p < 0))
    if losses == 0:
        return float("inf") if wins > 0 else None
    return wins / losses


def _classify(live_pf: Optional[float], ci_lower: float,
              n: int, *, thresholds: dict) -> tuple[str, Optional[float]]:
    """Return (verdict_str, pct_of_ci_lower). Pure function."""
    min_n = int(thresholds.get("min_n_for_status", 20))
    warn_pct = float(thresholds.get("warning_pct_of_ci_lower", 0.85))
    fail_pct = float(thresholds.get("fail_pct_of_ci_lower", 0.70))
    if n < min_n:
        return ("INSUFFICIENT_N", None)
    if live_pf is None:
        return ("INSUFFICIENT_N", None)
    pct = live_pf / ci_lower if ci_lower > 0 else None
    if pct is None:
        return ("FAIL", pct)
    if pct >= warn_pct:
        return ("PASS_GATE", pct)
    if pct >= fail_pct:
        return ("WARNING", pct)
    return ("FAIL", pct)


def evaluate_strategy(
    strategy: str,
    *,
    baseline: dict,
    rolling_n: int = 30,
    rolling_days: int = 90,
    now: Optional[datetime] = None,
) -> GateMonitorStatus:
    """Compute the live-gate status for one strategy.

    `rolling_n`: trade count for the recent-trades window
    `rolling_days`: calendar days for the time window
    """
    spec = baseline.get("strategies", {}).get(strategy)
    if not spec:
        return GateMonitorStatus(
            strategy=strategy, verdict="NO_BASELINE",
            notes=f"strategy not in baseline config",
        )
    ci_lower = float(spec.get("ci_95_lower", 0.0))
    thresholds = baseline.get("alert_thresholds", {})
    status = GateMonitorStatus(
        strategy=strategy, verdict="INSUFFICIENT_N",
        baseline_ci_lower=ci_lower,
        backtest_point_pf=spec.get("point_pf"),
        backtest_n=spec.get("n_backtest"),
        config=spec.get("config", ""),
    )
    df, source = _load_trades_csv(strategy)
    status.trade_source = source
    if df is None or len(df) == 0:
        status.notes = "no trades file found (live or archive)"
        return status
    if source == "pre_reset_archive":
        # Explicit warning when we used archive data — caller chose to opt in.
        status.notes = ("WARNING: using pre_reset_archive data, NOT live. "
                        "Post-reset evidence epoch may be contaminated by "
                        "earlier-cohort verdicts.")

    # Identify pnl column — gld_pm_long uses pnl_pct_of_fleet, others use pnl_pct
    pnl_col = None
    for candidate in ("pnl_pct_of_fleet", "pnl_pct"):
        if candidate in df.columns:
            pnl_col = candidate
            break
    if pnl_col is None:
        status.notes = "no pnl column found in trades.csv"
        return status

    # Identify timestamp column
    ts_col = None
    for candidate in ("ts", "entry_ts", "entry_dt"):
        if candidate in df.columns:
            ts_col = candidate
            break

    # Parse + sort by timestamp
    if ts_col is not None:
        df[ts_col] = pd.to_datetime(df[ts_col], errors="coerce", utc=True)
        df = df.dropna(subset=[ts_col]).sort_values(ts_col)
        if len(df) > 0:
            status.last_trade_ts = df[ts_col].iloc[-1].isoformat()

    pnls = pd.to_numeric(df[pnl_col], errors="coerce").dropna().tolist()
    status.n_live_trades = len(pnls)

    # 30-trade rolling PF (most recent N)
    if len(pnls) >= 1:
        recent_n = pnls[-rolling_n:] if len(pnls) >= rolling_n else pnls
        status.live_pf_30trades = _compute_pf(recent_n)

    # 90-day rolling PF (date-windowed)
    if ts_col is not None and len(df) > 0:
        now_ts = pd.Timestamp(now or datetime.now(timezone.utc))
        if now_ts.tz is None:
            now_ts = now_ts.tz_localize("UTC")
        cutoff = now_ts - pd.Timedelta(days=rolling_days)
        recent_df = df[df[ts_col] >= cutoff]
        recent_pnls = pd.to_numeric(recent_df[pnl_col], errors="coerce").dropna().tolist()
        if recent_pnls:
            status.live_pf_90days = _compute_pf(recent_pnls)

    # Classify based on 30-trade PF if available (more responsive than 90d)
    primary_pf = status.live_pf_30trades or status.live_pf_90days
    verdict, pct = _classify(primary_pf, ci_lower, status.n_live_trades,
                              thresholds=thresholds)
    status.verdict = verdict
    status.pct_of_ci_lower = round(pct, 4) if pct is not None else None
    if pct is not None:
        status.notes = (f"live PF {primary_pf:.3f} vs gate CI lower "
                        f"{ci_lower:.3f} ({status.pct_of_ci_lower:.1%})")
    return status


def evaluate_cohort(
    baseline: Optional[dict] = None,
    *,
    rolling_n: int = 30,
    rolling_days: int = 90,
) -> dict:
    """Evaluate all strategies in the baseline config. Returns a dict
    suitable for JSON serialization to cohort_gate_status.json."""
    if baseline is None:
        baseline = load_baseline()
    statuses = []
    for strategy in baseline.get("strategies", {}):
        st = evaluate_strategy(
            strategy, baseline=baseline,
            rolling_n=rolling_n, rolling_days=rolling_days,
        )
        statuses.append(st.to_dict())
    # Counts per verdict
    counts: dict[str, int] = {}
    for s in statuses:
        counts[s["verdict"]] = counts.get(s["verdict"], 0) + 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "baseline_version": baseline.get("version", "?"),
        "rolling_n_trades": rolling_n,
        "rolling_days": rolling_days,
        "verdict_counts": counts,
        "strategies": statuses,
    }


def write_report(report: dict, path: Path = DEFAULT_REPORT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return path


def main() -> int:
    """CLI entry: evaluate cohort and write report."""
    baseline = load_baseline()
    report = evaluate_cohort(baseline)
    path = write_report(report)
    counts = report["verdict_counts"]
    print(f"Cohort gate status written to {path}")
    print(f"Verdicts: {counts}")
    for s in report["strategies"]:
        line = (f"  {s['strategy']:<28} {s['verdict']:<18} "
                f"n={s['n_live_trades']:>3}  "
                f"live_pf={s.get('live_pf_30trades') or 'n/a':<6}  "
                f"baseline_ci_lower={s['baseline_ci_lower']}")
        print(line)
    # Exit code reflects worst verdict (0 OK, 1 WARNING, 2 FAIL)
    if "FAIL" in counts:
        return 2
    if "WARNING" in counts:
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
