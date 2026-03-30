"""Trade Tracker — auto-generate instrument performance grades.

Usage:
    python -m argus_flow.ops.trade_tracker
"""
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

REPO = Path(__file__).resolve().parents[2]
LOGS = REPO / "argus_flow" / "logs"

# Grade definitions
GRADES = {
    "A": "ANCHOR — validated, research-closed, live-proven",
    "B": "PROBATIONARY — strong backtest, collecting live data",
    "C": "COLLECTING — running but unvalidated",
    "C-": "WATCH — collecting but showing early concern",
    "D": "QUARANTINED — known issues, needs specific fix",
    "F": "KILLED — proven non-viable",
}

# Manual overrides (from fleet doctrine)
MANUAL_GRADES = {
    "EURUSD": "A",
    "GBPUSD": "B",
    "USDJPY": "D",
    "NKD": "F",
}

KILLED = {
    "NKD": {"date": "2026-03-29", "reason": "0/7 WR, -550 pips, every trade stopped out", "min_months_before_retry": 3},
}


def load_trades(symbol: str) -> list[dict]:
    tf = LOGS / symbol.lower() / "trades.csv"
    if not tf.exists():
        return []
    with open(tf) as f:
        return list(csv.DictReader(f))


def grade_instrument(symbol: str) -> dict:
    """Auto-grade an instrument based on trade performance."""
    trades = load_trades(symbol)
    valid = [t for t in trades if t.get("experiment_valid", "").lower() == "true"]

    # Manual override
    if symbol in MANUAL_GRADES:
        grade = MANUAL_GRADES[symbol]
    elif symbol in KILLED:
        grade = "F"
    elif len(valid) == 0:
        grade = "C"
    elif len(valid) < 5:
        # Check early losses
        pnl_field = "pnl_pips" if valid and "pnl_pips" in valid[0] else "pnl_pts"
        pnls = [float(t.get(pnl_field, 0)) for t in valid]
        wins = len([p for p in pnls if p > 0])
        if wins == 0 and len(valid) >= 3:
            grade = "C-"  # Watch — all losses
        else:
            grade = "C"
    else:
        pnl_field = "pnl_pips" if "pnl_pips" in valid[0] else "pnl_pts"
        pnls = [float(t.get(pnl_field, 0)) for t in valid]
        wins = len([p for p in pnls if p > 0])
        wr = wins / len(pnls)
        net = sum(pnls)

        if wr >= 0.45 and net > 0:
            grade = "B" if symbol not in MANUAL_GRADES else MANUAL_GRADES[symbol]
        elif wr < 0.3 or net < -50:
            grade = "D"
        else:
            grade = "C"

    # Compute stats
    pnl_field = "pnl_pips" if valid and "pnl_pips" in valid[0] else "pnl_pts"
    pnls = [float(t.get(pnl_field, 0)) for t in valid] if valid else []
    wins = len([p for p in pnls if p > 0])
    net = sum(pnls) if pnls else 0

    return {
        "symbol": symbol,
        "grade": grade,
        "grade_desc": GRADES.get(grade, ""),
        "total_trades": len(trades),
        "valid_trades": len(valid),
        "wins": wins,
        "losses": len(valid) - wins,
        "win_rate": round(wins / len(valid), 2) if valid else 0,
        "net_pnl": round(net, 1),
        "avg_pnl": round(net / len(valid), 2) if valid else 0,
        "killed": KILLED.get(symbol),
    }


def main():
    symbols = [
        "EURUSD", "GBPUSD", "EURJPY", "GBPJPY", "CADJPY", "AUDJPY", "USDJPY", "AUDUSD",
        "MES", "MNQ", "MYM", "M2K", "MGC", "MCL", "NKD",
    ]

    results = [grade_instrument(sym) for sym in symbols]

    print(f"\n{'='*75}")
    print(f"  ARGUS INSTRUMENT TRACKER — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"{'='*75}\n")

    grade_colors = {"A": "\033[32m", "B": "\033[36m", "C": "\033[33m", "C-": "\033[33m", "D": "\033[35m", "F": "\033[31m"}
    reset = "\033[0m"

    print(f"  {'Symbol':<10} {'Grade':<5} {'Trades':<8} {'WR':<8} {'Net PnL':<10} {'Status'}")
    print(f"  {'-'*70}")

    for r in sorted(results, key=lambda x: "ABCDF".index(x["grade"][0])):
        c = grade_colors.get(r["grade"], "")
        wr_str = f"{r['wins']}/{r['valid_trades']}" if r["valid_trades"] > 0 else "-"
        pnl_str = f"{r['net_pnl']:+.1f}" if r["valid_trades"] > 0 else "-"
        killed_str = f" (KILLED {r['killed']['date']})" if r.get("killed") else ""
        print(f"  {r['symbol']:<10} {c}{r['grade']:<5}{reset} {r['valid_trades']:<8} {wr_str:<8} {pnl_str:<10} {r['grade_desc']}{killed_str}")

    # Summary
    active = [r for r in results if r["grade"] not in ("F", "D")]
    active_pnl = sum(r["net_pnl"] for r in active)
    active_trades = sum(r["valid_trades"] for r in active)
    print(f"\n  Active fleet: {len(active)} instruments | {active_trades} trades | {active_pnl:+.1f} pips")

    killed_list = [r for r in results if r["grade"] == "F"]
    if killed_list:
        print(f"  Killed: {', '.join(r['symbol'] for r in killed_list)}")

    quarantined = [r for r in results if r["grade"] == "D"]
    if quarantined:
        print(f"  Quarantined: {', '.join(r['symbol'] for r in quarantined)}")

    print(f"\n{'='*75}\n")


if __name__ == "__main__":
    main()
