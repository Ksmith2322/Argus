#!/usr/bin/env python3
# analytics/risk_model.py
#
# Phase 13 — Risk-of-Ruin Modeling
#
# Reads:
#   trade_journal_*.csv (all runs combined, from log_dir)
#
# Writes:
#   risk_model_<date>.json — machine-readable risk model output
#
# Computes:
#   * per-trade PnL sequence (exact from trade_journal, no inference)
#   * win rate / expectancy / variance / Sharpe
#   * drawdown distribution (historical + Monte Carlo)
#   * max consecutive losses
#   * Monte Carlo equity paths (10,000 paths minimum)
#   * risk-per-trade sensitivity
#   * risk-of-ruin at current position sizing
#   * Kelly fraction calculation
#   * time to ruin distribution
#
# TRUTH-SURFACE DOCTRINE: reads only from trade_journal_*.csv (canonical lifecycle truth).
# PnL values are taken exactly as recorded — no inference, no re-derivation.
# This module is pure analytics: it reads lower-layer truth and produces
# a higher-layer report. It must never become runtime control state.
#
# NOTE: Monte Carlo statistics are not meaningful below 50 trades.
#       The report flags data_quality.sufficient_data = false and continues.
#
# Usage:
#   python -m analytics.risk_model
#   python -m analytics.risk_model --log-dir ops/logs --out-dir ops/logs

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

MINIMUM_TRADES = 50              # Phase 13 entry criterion for meaningful Monte Carlo
MONTE_CARLO_PATHS = 10_000       # required minimum per roadmap spec
REPORT_VERSION = "1.0"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_float(v: Any, default: float = 0.0) -> float:
    if v is None or str(v).strip() == "":
        return default
    try:
        return float(str(v).strip())
    except Exception:
        return default


def _percentiles(data: List[float]) -> Dict[str, Any]:
    """Return p5/p25/p50/p75/p95/p99/min/max/mean/count dict."""
    if not data:
        return {
            "p5": None, "p25": None, "p50": None, "p75": None,
            "p95": None, "p99": None,
            "min": None, "max": None, "mean": None, "count": 0,
        }
    s = sorted(data)
    n = len(s)

    def pct(p: float) -> float:
        idx = (p / 100.0) * (n - 1)
        lo, frac = int(idx), idx - int(idx)
        if lo + 1 >= n:
            return s[lo]
        return s[lo] + frac * (s[lo + 1] - s[lo])

    return {
        "p5":  round(pct(5),  8),
        "p25": round(pct(25), 8),
        "p50": round(pct(50), 8),
        "p75": round(pct(75), 8),
        "p95": round(pct(95), 8),
        "p99": round(pct(99), 8),
        "min":  round(s[0], 8),
        "max":  round(s[-1], 8),
        "mean": round(sum(s) / n, 8),
        "count": n,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Readers
# ─────────────────────────────────────────────────────────────────────────────

def _read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _read_all_trade_journals(log_dir: Path) -> List[Dict[str, str]]:
    """Read all trade_journal_*.csv from log_dir (non-recursive, sorted)."""
    rows: List[Dict[str, str]] = []
    for p in sorted(log_dir.glob("trade_journal_*.csv")):
        rows.extend(_read_csv(p))
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Core statistics
# ─────────────────────────────────────────────────────────────────────────────

def _compute_basic_stats(pnl_seq: List[float]) -> Dict[str, Any]:
    """
    Compute win rate, expectancy, variance, Sharpe, and related stats
    from a per-trade PnL sequence.
    """
    n = len(pnl_seq)
    if n == 0:
        return {
            "n_trades": 0,
            "win_rate": None,
            "loss_rate": None,
            "expectancy": None,
            "variance": None,
            "std_dev": None,
            "sharpe_per_trade": None,
            "avg_win": None,
            "avg_loss": None,
            "win_loss_ratio": None,
            "profit_factor": None,
            "total_pnl": None,
        }

    wins  = [p for p in pnl_seq if p > 0]
    losses = [p for p in pnl_seq if p <= 0]

    win_rate   = len(wins) / n
    loss_rate  = 1.0 - win_rate
    expectancy = sum(pnl_seq) / n
    variance   = sum((p - expectancy) ** 2 for p in pnl_seq) / n
    std_dev    = math.sqrt(variance)
    sharpe     = (expectancy / std_dev) if std_dev > 0 else None

    avg_win  = (sum(wins) / len(wins))   if wins   else None
    avg_loss = (sum(losses) / len(losses)) if losses else None

    win_loss_ratio = (
        abs(avg_win / avg_loss)
        if avg_win is not None and avg_loss is not None and avg_loss != 0
        else None
    )

    gross_wins   = sum(wins)
    gross_losses = abs(sum(losses))
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else None

    return {
        "n_trades":         n,
        "win_rate":         round(win_rate,   6),
        "loss_rate":        round(loss_rate,  6),
        "expectancy":       round(expectancy, 8),
        "variance":         round(variance,   8),
        "std_dev":          round(std_dev,    8),
        "sharpe_per_trade": round(sharpe,     6) if sharpe is not None else None,
        "avg_win":          round(avg_win,    8) if avg_win  is not None else None,
        "avg_loss":         round(avg_loss,   8) if avg_loss is not None else None,
        "win_loss_ratio":   round(win_loss_ratio, 6) if win_loss_ratio is not None else None,
        "profit_factor":    round(profit_factor,  6) if profit_factor  is not None else None,
        "total_pnl":        round(sum(pnl_seq), 8),
    }


def _compute_max_consecutive_losses(pnl_seq: List[float]) -> int:
    """Return the maximum count of consecutive losing (pnl <= 0) trades."""
    max_streak = 0
    current    = 0
    for p in pnl_seq:
        if p <= 0:
            current += 1
            if current > max_streak:
                max_streak = current
        else:
            current = 0
    return max_streak


def _compute_historical_drawdown(
    pnl_seq: List[float],
) -> Tuple[float, List[float], List[float]]:
    """
    Walk the PnL sequence and compute an equity curve starting from 0.

    Returns:
        max_drawdown   — most negative single drawdown value (absolute, <= 0)
        equity_curve   — cumulative PnL at each trade
        drawdown_series — underwater equity at each trade (0 or negative)
    """
    equity          = 0.0
    peak            = 0.0
    equity_curve:   List[float] = []
    drawdown_series: List[float] = []

    for pnl in pnl_seq:
        equity += pnl
        equity_curve.append(equity)
        if equity > peak:
            peak = equity
        dd = equity - peak          # <= 0 when underwater
        drawdown_series.append(dd)

    max_dd = min(drawdown_series) if drawdown_series else 0.0
    return max_dd, equity_curve, drawdown_series


# ─────────────────────────────────────────────────────────────────────────────
# Kelly fraction
# ─────────────────────────────────────────────────────────────────────────────

def _compute_kelly(
    win_rate: Optional[float],
    avg_win:  Optional[float],
    avg_loss: Optional[float],
) -> Optional[float]:
    """
    Kelly criterion: f* = (W * avg_win − (1−W) * |avg_loss|) / avg_win

    Positive result = fraction of bankroll to risk per trade.
    Returns None if inputs are undefined or degenerate.
    """
    if win_rate is None or avg_win is None or avg_loss is None:
        return None
    if avg_win <= 0 or avg_loss == 0:
        return None
    abs_loss = abs(avg_loss)
    kelly = (win_rate * avg_win - (1.0 - win_rate) * abs_loss) / avg_win
    return round(kelly, 6)


# ─────────────────────────────────────────────────────────────────────────────
# Monte Carlo simulation
# ─────────────────────────────────────────────────────────────────────────────

def _monte_carlo(
    pnl_seq:         List[float],
    n_paths:         int,
    n_steps:         int,
    starting_equity: float,
    ruin_threshold:  float,
    rng_seed:        int = 42,
) -> Dict[str, Any]:
    """
    Bootstrap Monte Carlo: resample (with replacement) from pnl_seq per step.

    For each of n_paths equity paths of length n_steps:
      - Start at starting_equity
      - Each step: add a randomly sampled trade PnL
      - Track max drawdown and whether equity ever falls to ruin_threshold

    Outputs:
      risk_of_ruin           — fraction of paths that breach ruin_threshold
      time_to_ruin           — trade index at first ruin (ruined paths only)
      max_drawdown_abs/pct   — distribution across all paths
      final_equity           — distribution of terminal equity
      equity_path_percentiles — per-step p5/p25/p50/p75/p95 (sampled every N steps
                                 to keep JSON size manageable)
    """
    rng = random.Random(rng_seed)
    n   = len(pnl_seq)

    max_dd_abs:     List[float] = []
    max_dd_pct:     List[float] = []
    final_equities: List[float] = []
    ruined_paths    = 0
    time_to_ruin:   List[int]   = []

    # Store per-step equity for percentile calculation.
    # Limit to at most 500 steps in the output to keep JSON size reasonable;
    # sample uniformly if n_steps > 500.
    sample_step_count = min(n_steps, 500)
    if n_steps <= sample_step_count:
        sampled_steps = list(range(n_steps))
    else:
        # Include first, last, and evenly-spaced intermediates
        step_size = (n_steps - 1) / (sample_step_count - 1)
        sampled_steps = [round(i * step_size) for i in range(sample_step_count)]
        sampled_steps[-1] = n_steps - 1  # ensure last step included

    step_equity_buckets: Dict[int, List[float]] = {s: [] for s in sampled_steps}

    for _ in range(n_paths):
        eq     = starting_equity
        peak   = starting_equity
        path_max_dd = 0.0
        ruined = False
        ruin_step: Optional[int] = None

        for step in range(n_steps):
            eq += rng.choice(pnl_seq)
            if eq > peak:
                peak = eq
            dd = peak - eq              # positive value = depth of drawdown
            if dd > path_max_dd:
                path_max_dd = dd

            if not ruined and eq <= ruin_threshold:
                ruined    = True
                ruin_step = step + 1   # 1-indexed

            if step in step_equity_buckets:
                step_equity_buckets[step].append(eq)

        max_dd_abs.append(path_max_dd)
        if starting_equity > 0:
            max_dd_pct.append(path_max_dd / starting_equity * 100.0)
        final_equities.append(eq)

        if ruined:
            ruined_paths += 1
            if ruin_step is not None:
                time_to_ruin.append(ruin_step)

    risk_of_ruin = ruined_paths / n_paths

    # Equity path percentile table (one entry per sampled step)
    equity_path_percentiles: List[Dict[str, Any]] = []
    for step in sampled_steps:
        vals = step_equity_buckets[step]
        if vals:
            p = _percentiles(vals)
            equity_path_percentiles.append({
                "step":  step + 1,       # 1-indexed trade number
                "p5":    p["p5"],
                "p25":   p["p25"],
                "p50":   p["p50"],
                "p75":   p["p75"],
                "p95":   p["p95"],
            })

    return {
        "n_paths":         n_paths,
        "n_steps":         n_steps,
        "starting_equity": starting_equity,
        "ruin_threshold":  ruin_threshold,
        "risk_of_ruin":    round(risk_of_ruin, 6),
        "ruined_paths":    ruined_paths,
        "max_drawdown_abs": _percentiles(max_dd_abs),
        "max_drawdown_pct": _percentiles(max_dd_pct),
        "final_equity":     _percentiles(final_equities),
        "time_to_ruin":     _percentiles(time_to_ruin) if time_to_ruin else {
            "p5": None, "p25": None, "p50": None, "p75": None,
            "p95": None, "p99": None, "min": None, "max": None,
            "mean": None, "count": 0,
        },
        "equity_path_percentiles": equity_path_percentiles,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Risk-per-trade sensitivity table
# ─────────────────────────────────────────────────────────────────────────────

def _risk_sensitivity(
    win_rate:        Optional[float],
    avg_win:         Optional[float],
    avg_loss:        Optional[float],
    starting_equity: float,
    kelly:           Optional[float],
    risk_fractions:  Optional[List[float]] = None,
) -> List[Dict[str, Any]]:
    """
    For each candidate risk fraction (as % of bankroll), compute:
      - USD at risk at starting_equity
      - fraction of Kelly being used
      - whether we are at or above full Kelly
    """
    if risk_fractions is None:
        risk_fractions = [0.005, 0.01, 0.02, 0.03, 0.05, 0.10]

    rows = []
    for rf in risk_fractions:
        pct_of_kelly = (
            round(rf / kelly, 4)
            if kelly is not None and kelly > 0
            else None
        )
        rows.append({
            "risk_fraction":       rf,
            "risk_usd_at_start":   round(starting_equity * rf, 4),
            "pct_of_kelly":        pct_of_kelly,
            "at_or_above_kelly":   (kelly is not None and kelly > 0 and rf >= kelly),
        })
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Main model assembly
# ─────────────────────────────────────────────────────────────────────────────

def build_model(
    log_dir:             Path,
    starting_equity:     float = 1000.0,
    ruin_threshold_pct:  float = 0.50,
    n_mc_paths:          int   = MONTE_CARLO_PATHS,
    mc_steps:            Optional[int] = None,
    rng_seed:            int   = 42,
) -> Dict[str, Any]:
    """
    Build the complete risk model from all trade_journal_*.csv in log_dir.

    Args:
        log_dir:            directory containing trade_journal_*.csv files
        starting_equity:    hypothetical starting equity for MC (USD)
        ruin_threshold_pct: fraction of starting_equity = ruin level
                            (0.50 → ruin declared when equity falls to 50% of start)
        n_mc_paths:         Monte Carlo paths — enforced >= MONTE_CARLO_PATHS
        mc_steps:           steps per MC path — defaults to max(n_trades, 200)
        rng_seed:           for deterministic / reproducible output
    """
    n_mc_paths = max(n_mc_paths, MONTE_CARLO_PATHS)  # enforce 10,000 minimum

    trade_rows = _read_all_trade_journals(log_dir)

    # PnL sequence — exact values from canonical source, no re-derivation
    pnl_seq: List[float] = []
    for row in trade_rows:
        raw = row.get("pnl", "").strip()
        if raw:
            pnl_seq.append(_safe_float(raw))

    n_trades = len(pnl_seq)
    warnings: List[str] = []

    if n_trades < MINIMUM_TRADES:
        warnings.append(
            f"Only {n_trades} trades found; {MINIMUM_TRADES}+ required for meaningful "
            "Monte Carlo distributions. Accumulate more live paper trades before "
            "relying on risk-of-ruin or Kelly estimates."
        )

    # ── Basic stats
    stats = _compute_basic_stats(pnl_seq)

    # ── Kelly
    kelly = _compute_kelly(
        win_rate=stats.get("win_rate"),
        avg_win=stats.get("avg_win"),
        avg_loss=stats.get("avg_loss"),
    )

    # ── Max consecutive losses
    max_consec_losses = _compute_max_consecutive_losses(pnl_seq)

    # ── Historical drawdown
    max_dd_hist, equity_curve, dd_series = _compute_historical_drawdown(pnl_seq)
    hist_dd_depth_vals = [abs(d) for d in dd_series if d < 0]

    ruin_threshold = starting_equity * ruin_threshold_pct
    steps = mc_steps if mc_steps is not None else max(n_trades, 200)

    # ── Monte Carlo (requires at least 2 distinct observations to resample)
    if n_trades >= 2:
        mc = _monte_carlo(
            pnl_seq=pnl_seq,
            n_paths=n_mc_paths,
            n_steps=steps,
            starting_equity=starting_equity,
            ruin_threshold=ruin_threshold,
            rng_seed=rng_seed,
        )
    else:
        mc = {
            "n_paths":          0,
            "n_steps":          0,
            "starting_equity":  starting_equity,
            "ruin_threshold":   ruin_threshold,
            "risk_of_ruin":     None,
            "ruined_paths":     0,
            "max_drawdown_abs": _percentiles([]),
            "max_drawdown_pct": _percentiles([]),
            "final_equity":     _percentiles([]),
            "time_to_ruin":     _percentiles([]),
            "equity_path_percentiles": [],
            "note": "Insufficient trades for Monte Carlo simulation (need >= 2).",
        }

    # ── Risk-per-trade sensitivity
    sensitivity = _risk_sensitivity(
        win_rate=stats.get("win_rate"),
        avg_win=stats.get("avg_win"),
        avg_loss=stats.get("avg_loss"),
        starting_equity=starting_equity,
        kelly=kelly,
    )

    return {
        "version":       REPORT_VERSION,
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "log_dir":       str(log_dir),
        "configuration": {
            "starting_equity":    starting_equity,
            "ruin_threshold":     ruin_threshold,
            "ruin_threshold_pct": ruin_threshold_pct,
            "n_mc_paths":         n_mc_paths,
            "mc_steps":           steps,
            "rng_seed":           rng_seed,
        },
        "data_quality": {
            "sufficient_data":  n_trades >= MINIMUM_TRADES,
            "n_trades":         n_trades,
            "minimum_required": MINIMUM_TRADES,
            "warnings":         warnings,
        },
        "basic_stats":           stats,
        "kelly_fraction":        kelly,
        "max_consecutive_losses": max_consec_losses,
        "historical_drawdown": {
            "max_drawdown_abs":           round(max_dd_hist, 8),
            "max_drawdown_pct_of_start":  (
                round(abs(max_dd_hist) / starting_equity * 100.0, 4)
                if starting_equity > 0 else None
            ),
            "drawdown_depth_distribution": _percentiles(hist_dd_depth_vals),
            "equity_curve":               [round(e, 8) for e in equity_curve],
            "drawdown_series":            [round(d, 8) for d in dd_series],
        },
        "monte_carlo":     mc,
        "risk_sensitivity": sensitivity,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Output writer
# ─────────────────────────────────────────────────────────────────────────────

def write_model(model: Dict[str, Any], out_dir: Path, date_str: str) -> Path:
    """Write risk_model_<date_str>.json to out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"risk_model_{date_str}.json"
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(model, f, indent=2, default=str)
    return json_path


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _default_date_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Phase 13 risk model — reads trade_journal_*.csv, "
            "writes risk_model_<date>.json"
        )
    )
    parser.add_argument(
        "--log-dir",
        default="ops/logs",
        help="Directory containing trade_journal_*.csv files (default: ops/logs)",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Output directory for risk_model JSON (default: same as --log-dir)",
    )
    parser.add_argument(
        "--starting-equity",
        type=float,
        default=1000.0,
        help="Hypothetical starting equity for Monte Carlo (default: 1000.0)",
    )
    parser.add_argument(
        "--ruin-threshold",
        type=float,
        default=0.50,
        help=(
            "Ruin threshold as fraction of starting equity "
            "(default: 0.50 = 50%% drawdown from start)"
        ),
    )
    parser.add_argument(
        "--mc-paths",
        type=int,
        default=MONTE_CARLO_PATHS,
        help=(
            f"Monte Carlo path count "
            f"(minimum {MONTE_CARLO_PATHS}, default: {MONTE_CARLO_PATHS})"
        ),
    )
    parser.add_argument(
        "--mc-steps",
        type=int,
        default=None,
        help="Steps per Monte Carlo path (default: max(n_trades, 200))",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="RNG seed for Monte Carlo reproducibility (default: 42)",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Date string for output filename (default: current UTC date YYYYMMDD)",
    )
    args = parser.parse_args(argv)

    log_dir  = Path(args.log_dir).resolve()
    out_dir  = Path(args.out_dir).resolve() if args.out_dir else log_dir
    date_str = args.date or _default_date_str()

    if not log_dir.exists():
        print(f"ERROR: log_dir does not exist: {log_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Building risk model from {log_dir} ...")
    model = build_model(
        log_dir=log_dir,
        starting_equity=args.starting_equity,
        ruin_threshold_pct=args.ruin_threshold,
        n_mc_paths=args.mc_paths,
        mc_steps=args.mc_steps,
        rng_seed=args.seed,
    )

    json_path = write_model(model, out_dir, date_str)

    dq  = model["data_quality"]
    bs  = model["basic_stats"]
    mc  = model["monte_carlo"]
    hdd = model["historical_drawdown"]

    for w in dq["warnings"]:
        print(f"  WARNING: {w}")

    print(f"  Trades analyzed:      {dq['n_trades']}")
    print(f"  Sufficient data:      {dq['sufficient_data']}")
    print(f"  Win rate:             {bs.get('win_rate')}")
    print(f"  Expectancy (USD):     {bs.get('expectancy')}")
    print(f"  Std dev (USD):        {bs.get('std_dev')}")
    print(f"  Sharpe (per-trade):   {bs.get('sharpe_per_trade')}")
    print(f"  Profit factor:        {bs.get('profit_factor')}")
    print(f"  Kelly fraction:       {model['kelly_fraction']}")
    print(f"  Max consec losses:    {model['max_consecutive_losses']}")
    print(f"  Hist max drawdown:    {hdd['max_drawdown_abs']}")

    ror = mc.get("risk_of_ruin")
    if ror is not None:
        print(
            f"  Risk of ruin:         {ror:.4%}  "
            f"({mc.get('ruined_paths')}/{mc.get('n_paths')} paths, "
            f"ruin = equity <= {mc.get('ruin_threshold')})"
        )
    else:
        print("  Risk of ruin:         n/a (insufficient data)")

    print(f"\n  JSON -> {json_path}")


if __name__ == "__main__":
    main()
