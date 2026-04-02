"""Edge-Weighted Allocation — concentrate capital into proven winners.

Instead of equal risk across all pairs, this module computes an allocation
weight for each pair based on realized edge strength:
  - Profit factor
  - Expectancy
  - Sample size (confidence)
  - Drawdown history

Outputs a per-pair risk_multiplier that the sizing stack can consume.
Pairs with no proven edge get base allocation. Pairs with strong edge
get up to 2x base. Pairs with negative edge get reduced allocation.

Currently LOG_ONLY — outputs a report for dashboard consumption.
Will be wired into the sizing stack after sufficient data accumulates.

Usage:
    python -m argus_flow.ops.edge_allocation
"""
from __future__ import annotations

import csv
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LOGS_ROOT = REPO / "argus_flow" / "logs"
OUTPUT_PATH = LOGS_ROOT / "edge_allocation_report.json"

# Minimum trades before edge weighting kicks in
MIN_TRADES_FOR_EDGE = 20

# Allocation bounds
MAX_ALLOCATION_MULT = 2.0   # best pair gets at most 2x base risk
MIN_ALLOCATION_MULT = 0.5   # worst pair gets at most 0.5x base risk
DEFAULT_MULT = 1.0           # pairs without enough data


def _safe_float(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _read_trades(log_dir: Path) -> list[dict]:
    trade_file = log_dir / "trades.csv"
    if not trade_file.exists():
        return []
    try:
        with open(trade_file, "r", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _compute_edge_score(trades: list[dict]) -> dict:
    """Compute edge score for a set of trades. Returns metrics + allocation multiplier."""
    if len(trades) < MIN_TRADES_FOR_EDGE:
        return {
            "trade_count": len(trades),
            "edge_score": 0,
            "allocation_mult": DEFAULT_MULT,
            "status": "INSUFFICIENT_DATA",
            "reason": f"{len(trades)}/{MIN_TRADES_FOR_EDGE} trades needed",
        }

    pnl_field = "pnl_pips" if "pnl_pips" in trades[0] else "pnl_pts"
    pnls = [_safe_float(t.get(pnl_field, 0)) for t in trades]
    pnl_usd = [_safe_float(t.get("pnl_usd", 0)) for t in trades]

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    win_rate = len(wins) / len(pnls) if pnls else 0
    expectancy = statistics.mean(pnls) if pnls else 0
    gross_wins = sum(p for p in pnl_usd if p > 0)
    gross_losses = abs(sum(p for p in pnl_usd if p < 0))
    pf = gross_wins / gross_losses if gross_losses > 0 else (10.0 if gross_wins > 0 else 0)

    # Edge score: combines PF, win rate, and sample confidence
    # PF contribution: 0-1 (PF=1.0 -> 0, PF=2.0 -> 1.0, capped)
    pf_score = max(0, min(1.0, (pf - 1.0) / 1.0))

    # Win rate contribution: 0-1 (centered around 0.5)
    wr_score = max(0, min(1.0, (win_rate - 0.3) / 0.4))

    # Sample confidence: 0-1 (20 trades -> 0, 100 trades -> 1)
    conf_score = min(1.0, (len(trades) - MIN_TRADES_FOR_EDGE) / 80)

    # Combined edge score (PF dominates)
    edge_score = round(pf_score * 0.5 + wr_score * 0.3 + conf_score * 0.2, 3)

    # Map to allocation multiplier
    # edge_score 0 -> MIN_ALLOCATION_MULT, edge_score 1 -> MAX_ALLOCATION_MULT
    allocation_mult = round(
        MIN_ALLOCATION_MULT + edge_score * (MAX_ALLOCATION_MULT - MIN_ALLOCATION_MULT),
        3,
    )

    # Override: negative expectancy -> reduce further
    if expectancy < 0 and len(trades) >= 30:
        allocation_mult = min(allocation_mult, MIN_ALLOCATION_MULT)

    status = "POSITIVE" if pf > 1.0 else "NEGATIVE" if pf < 1.0 else "BREAK_EVEN"

    return {
        "trade_count": len(trades),
        "win_rate": round(win_rate, 3),
        "expectancy": round(expectancy, 4),
        "profit_factor": round(pf, 3),
        "total_pnl_usd": round(sum(pnl_usd), 2),
        "edge_score": edge_score,
        "allocation_mult": allocation_mult,
        "status": status,
        "components": {
            "pf_score": round(pf_score, 3),
            "wr_score": round(wr_score, 3),
            "conf_score": round(conf_score, 3),
        },
    }


def generate_report() -> dict:
    """Generate edge-weighted allocation report for all instruments."""
    instruments = {}

    for log_dir in sorted(LOGS_ROOT.iterdir()):
        if not log_dir.is_dir() or log_dir.name.startswith("_"):
            continue
        trades = _read_trades(log_dir)
        if not trades:
            continue
        symbol = log_dir.name.upper()
        instruments[symbol] = _compute_edge_score(trades)

    # Rank by edge score
    ranked = sorted(instruments.items(), key=lambda x: -x[1].get("edge_score", 0))

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "LOG_ONLY",
        "min_trades": MIN_TRADES_FOR_EDGE,
        "instruments": dict(ranked),
        "summary": {
            "total_instruments": len(instruments),
            "with_sufficient_data": sum(1 for v in instruments.values() if v["status"] != "INSUFFICIENT_DATA"),
            "positive_edge": sum(1 for v in instruments.values() if v["status"] == "POSITIVE"),
            "negative_edge": sum(1 for v in instruments.values() if v["status"] == "NEGATIVE"),
        },
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    return report


def main():
    print("=" * 55)
    print(f"  Edge-Weighted Allocation -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"  Mode: LOG_ONLY (not yet driving sizing)")
    print("=" * 55)

    report = generate_report()

    for sym, data in report["instruments"].items():
        status_colors = {"POSITIVE": "+", "NEGATIVE": "-", "BREAK_EVEN": "=", "INSUFFICIENT_DATA": "?"}
        marker = status_colors.get(data["status"], "?")
        print(f"  [{marker}] {sym:>8s}: {data['trade_count']:>3d} trades | "
              f"PF={data.get('profit_factor', 0):.2f} | "
              f"edge={data.get('edge_score', 0):.3f} | "
              f"alloc={data.get('allocation_mult', 1.0):.2f}x | "
              f"{data['status']}")

    s = report["summary"]
    print(f"\n  {s['total_instruments']} instruments | "
          f"{s['with_sufficient_data']} with data | "
          f"{s['positive_edge']} positive | "
          f"{s['negative_edge']} negative")
    print(f"\n  Report: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
