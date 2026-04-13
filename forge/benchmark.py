"""
Fleet P&L vs SPY Benchmark Tracker
===================================
Aggregates all fleet system P&L and compares to SPY buy-and-hold.
Answers the #1 question: "Am I beating the market?"

Usage:
    python -m forge.benchmark --report
    python -m forge.benchmark --daily
    python -m forge.benchmark --history
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
FORGE_DATA = REPO / "forge" / "data"
HISTORY_FILE = FORGE_DATA / "benchmark_history.json"
DEFAULT_START = "2026-04-11"

# ---------------------------------------------------------------------------
# Trade CSV discovery & parsing
# ---------------------------------------------------------------------------

SYSTEM_TRADE_PATHS = {
    "argus": REPO / "argus_flow" / "logs",
    "apollo": REPO / "helio" / "logs",
    "gld": REPO / "helio" / "logs" / "gld",
    "forge_gdx_gld": REPO / "forge" / "logs" / "gdx_gld",
    "titan": REPO / "titan" / "logs",
    "hermes": REPO / "hermes" / "logs",
}


def _find_trades_csvs() -> dict[str, list[Path]]:
    """Return {system_name: [trades.csv paths]}."""
    found: dict[str, list[Path]] = {}

    # Argus: argus_flow/logs/*/trades.csv
    argus_root = SYSTEM_TRADE_PATHS["argus"]
    if argus_root.exists():
        csvs = sorted(argus_root.glob("*/trades.csv"))
        if csvs:
            found["argus"] = csvs

    # Apollo: helio/logs/apollo_*/trades.csv
    apollo_root = SYSTEM_TRADE_PATHS["apollo"]
    if apollo_root.exists():
        csvs = sorted(apollo_root.glob("apollo_*/trades.csv"))
        if csvs:
            found["apollo"] = csvs

    # GLD: helio/logs/gld/trades.csv
    gld_path = SYSTEM_TRADE_PATHS["gld"] / "trades.csv"
    if gld_path.exists():
        found["gld"] = [gld_path]

    # Forge GDX/GLD
    forge_path = SYSTEM_TRADE_PATHS["forge_gdx_gld"] / "trades.csv"
    if forge_path.exists():
        found["forge_gdx_gld"] = [forge_path]

    # Titan
    titan_path = SYSTEM_TRADE_PATHS["titan"] / "trades.csv"
    if titan_path.exists():
        found["titan"] = [titan_path]

    # Hermes
    hermes_path = SYSTEM_TRADE_PATHS["hermes"] / "trades.csv"
    if hermes_path.exists():
        found["hermes"] = [hermes_path]

    return found


def _parse_date_from_row(row: dict) -> Optional[date]:
    """Extract a trade date from any CSV format."""
    # Argus format: ts column (ISO timestamp)
    if "ts" in row and row["ts"]:
        try:
            return datetime.fromisoformat(row["ts"]).date()
        except (ValueError, TypeError):
            pass
    # Apollo/Helio/Forge format: exit_date column
    if "exit_date" in row and row["exit_date"]:
        try:
            return date.fromisoformat(row["exit_date"])
        except (ValueError, TypeError):
            pass
    # entry_date fallback
    if "entry_date" in row and row["entry_date"]:
        try:
            return date.fromisoformat(row["entry_date"])
        except (ValueError, TypeError):
            pass
    return None


def _parse_pnl_from_row(row: dict) -> Optional[float]:
    """Extract P&L percentage from any CSV format."""
    # Argus: has pnl_usd but not pnl_pct — we'll use pnl_usd
    # Apollo/Helio: pnl_pct
    # Forge GDX/GLD: pnl_pct
    if "pnl_pct" in row and row["pnl_pct"]:
        try:
            return float(row["pnl_pct"])
        except (ValueError, TypeError):
            pass
    return None


def _parse_pnl_usd_from_row(row: dict) -> Optional[float]:
    """Extract P&L in USD from any CSV format."""
    if "pnl_usd" in row and row["pnl_usd"]:
        try:
            return float(row["pnl_usd"])
        except (ValueError, TypeError):
            pass
    return None


def _read_trades(csv_path: Path, start: date, end: date) -> list[dict]:
    """Read trades from a CSV file, filter by date range."""
    trades = []
    try:
        with open(csv_path, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trade_date = _parse_date_from_row(row)
                if trade_date is None:
                    continue
                if trade_date < start or trade_date > end:
                    continue
                pnl_pct = _parse_pnl_from_row(row)
                pnl_usd = _parse_pnl_usd_from_row(row)
                direction = row.get("direction", "unknown")
                trades.append({
                    "date": trade_date.isoformat(),
                    "pnl_pct": pnl_pct,
                    "pnl_usd": pnl_usd,
                    "direction": direction,
                })
    except Exception:
        pass
    return trades


# ---------------------------------------------------------------------------
# SPY benchmark
# ---------------------------------------------------------------------------

def _get_spy_return(start: date, end: date) -> Optional[dict]:
    """Get SPY buy-and-hold return over the period."""
    try:
        import yfinance as yf
        # Fetch one extra day on each side for safety
        fetch_start = start - timedelta(days=5)
        fetch_end = end + timedelta(days=3)
        spy = yf.download(
            "SPY", start=fetch_start.isoformat(), end=fetch_end.isoformat(),
            progress=False, auto_adjust=True,
        )
        if spy.empty:
            return None

        # Flatten MultiIndex columns if present
        if hasattr(spy.columns, 'levels') and spy.columns.nlevels > 1:
            spy.columns = spy.columns.get_level_values(0)

        spy = spy.sort_index()
        # Find closest available dates
        spy_dates = spy.index
        start_rows = spy_dates[spy_dates >= str(start)]
        if len(start_rows) == 0:
            return None
        start_px = float(spy.loc[start_rows[0], "Close"])

        end_rows = spy_dates[spy_dates <= str(end)]
        if len(end_rows) == 0:
            return None
        end_px = float(spy.loc[end_rows[-1], "Close"])

        ret_pct = (end_px - start_px) / start_px * 100
        return {
            "spy_return_pct": round(ret_pct, 4),
            "spy_start_px": round(start_px, 2),
            "spy_end_px": round(end_px, 2),
        }
    except Exception as e:
        return {"spy_return_pct": 0.0, "error": str(e)}


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def compute_fleet_benchmark(start_date: str = None, end_date: str = None) -> dict:
    """
    Aggregate all fleet system P&L and compare to SPY buy-and-hold.

    Returns dict with period, fleet, benchmark, and alpha sections.
    """
    start = date.fromisoformat(start_date or DEFAULT_START)
    end = date.fromisoformat(end_date) if end_date else date.today()

    all_csvs = _find_trades_csvs()

    systems = {}
    total_trades = 0
    total_wins = 0
    total_pnl_pct = 0.0
    total_pnl_usd = 0.0
    has_pct = False
    has_usd = False

    for sys_name, csv_paths in all_csvs.items():
        sys_trades = []
        for p in csv_paths:
            sys_trades.extend(_read_trades(p, start, end))

        if not sys_trades:
            continue

        sys_pnl_pct = 0.0
        sys_pnl_usd = 0.0
        sys_wins = 0
        for t in sys_trades:
            if t["pnl_pct"] is not None:
                sys_pnl_pct += t["pnl_pct"]
                has_pct = True
                if t["pnl_pct"] > 0:
                    sys_wins += 1
            if t["pnl_usd"] is not None:
                sys_pnl_usd += t["pnl_usd"]
                has_usd = True
                # If no pnl_pct but has pnl_usd, count wins by usd
                if t["pnl_pct"] is None and t["pnl_usd"] > 0:
                    sys_wins += 1

        systems[sys_name] = {
            "pnl_pct": round(sys_pnl_pct, 4),
            "pnl_usd": round(sys_pnl_usd, 2),
            "trades": len(sys_trades),
            "wins": sys_wins,
            "win_rate": round(sys_wins / len(sys_trades), 4) if sys_trades else 0,
            "csvs": len(csv_paths),
        }

        total_trades += len(sys_trades)
        total_wins += sys_wins
        total_pnl_pct += sys_pnl_pct
        total_pnl_usd += sys_pnl_usd

    # SPY benchmark
    spy = _get_spy_return(start, end) or {"spy_return_pct": 0.0}
    spy_return_pct = spy.get("spy_return_pct", 0.0)

    # Alpha = fleet return - SPY
    # Use pct if available, else note USD-only
    fleet_pct = round(total_pnl_pct, 4) if has_pct else None
    alpha_pct = round(fleet_pct - spy_return_pct, 4) if fleet_pct is not None else None

    result = {
        "period": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": (end - start).days,
        },
        "fleet": {
            "total_pnl_pct": fleet_pct,
            "total_pnl_usd": round(total_pnl_usd, 2),
            "trades": total_trades,
            "win_rate": round(total_wins / total_trades, 4) if total_trades else 0,
            "systems": systems,
        },
        "benchmark": {
            "spy_return_pct": round(spy_return_pct, 4),
            "spy_return_usd": None,  # Would need capital base to compute
        },
        "alpha": {
            "pct": alpha_pct,
            "beating_spy": alpha_pct > 0 if alpha_pct is not None else None,
        },
    }
    return result


# ---------------------------------------------------------------------------
# History / snapshots
# ---------------------------------------------------------------------------

def save_daily_snapshot(result: dict) -> None:
    """Append today's result to benchmark history JSON."""
    FORGE_DATA.mkdir(parents=True, exist_ok=True)

    history = []
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            history = []

    today = date.today().isoformat()
    # Replace existing entry for today if present
    history = [h for h in history if h.get("date") != today]
    history.append({
        "date": today,
        "fleet_pnl_pct": result["fleet"]["total_pnl_pct"],
        "fleet_pnl_usd": result["fleet"]["total_pnl_usd"],
        "fleet_trades": result["fleet"]["trades"],
        "fleet_win_rate": result["fleet"]["win_rate"],
        "spy_return_pct": result["benchmark"]["spy_return_pct"],
        "alpha_pct": result["alpha"]["pct"],
        "beating_spy": result["alpha"]["beating_spy"],
    })
    history.sort(key=lambda h: h["date"])
    HISTORY_FILE.write_text(json.dumps(history, indent=2))
    print(f"  Snapshot saved to {HISTORY_FILE}")


def show_history() -> None:
    """Print running alpha history."""
    if not HISTORY_FILE.exists():
        print("No benchmark history found. Run --daily first.")
        return

    history = json.loads(HISTORY_FILE.read_text())
    if not history:
        print("History file is empty.")
        return

    print("\n" + "=" * 70)
    print("BENCHMARK HISTORY — Fleet Alpha vs SPY")
    print("=" * 70)
    print(f"{'Date':<14} {'Fleet%':>8} {'SPY%':>8} {'Alpha%':>8} {'Trades':>7} {'WR':>6} {'Status':>10}")
    print("-" * 70)
    for h in history:
        fleet_pct = h.get("fleet_pnl_pct")
        spy_pct = h.get("spy_return_pct", 0)
        alpha = h.get("alpha_pct")
        status = "BEATING" if h.get("beating_spy") else "BEHIND"
        fleet_str = f"{fleet_pct:>8.3f}" if fleet_pct is not None else "     N/A"
        alpha_str = f"{alpha:>8.3f}" if alpha is not None else "     N/A"
        print(f"{h['date']:<14} {fleet_str} {spy_pct:>8.3f} {alpha_str} {h.get('fleet_trades', 0):>7} {h.get('fleet_win_rate', 0):>6.1%} {status:>10}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(result: dict) -> None:
    """Print formatted benchmark report."""
    p = result["period"]
    f = result["fleet"]
    b = result["benchmark"]
    a = result["alpha"]

    print("\n" + "=" * 70)
    print("FLEET BENCHMARK REPORT — Fleet P&L vs SPY Buy-and-Hold")
    print("=" * 70)
    print(f"  Period: {p['start']} to {p['end']} ({p['days']} days)")
    print()

    # Fleet summary
    print("  FLEET PERFORMANCE")
    print("  -" * 30)
    pct_str = f"{f['total_pnl_pct']:.3f}%" if f['total_pnl_pct'] is not None else "N/A (USD only)"
    print(f"    Total P&L:     {pct_str}")
    print(f"    Total P&L USD: ${f['total_pnl_usd']:,.2f}")
    print(f"    Trades:        {f['trades']}")
    print(f"    Win Rate:      {f['win_rate']:.1%}")
    print()

    # Per-system breakdown
    if f["systems"]:
        print("  PER-SYSTEM BREAKDOWN")
        print("  -" * 30)
        print(f"    {'System':<18} {'P&L %':>9} {'P&L $':>10} {'Trades':>7} {'WR':>6} {'CSVs':>5}")
        print("    " + "-" * 55)
        for name, s in sorted(f["systems"].items()):
            pct = f"{s['pnl_pct']:.3f}" if s['pnl_pct'] else "0.000"
            print(f"    {name:<18} {pct:>8}% ${s['pnl_usd']:>9,.2f} {s['trades']:>7} {s['win_rate']:>5.0%} {s['csvs']:>5}")
        print()

    # SPY benchmark
    print("  SPY BENCHMARK")
    print("  -" * 30)
    print(f"    SPY Return:    {b['spy_return_pct']:.3f}%")
    print()

    # Alpha
    print("  ALPHA")
    print("  -" * 30)
    if a["pct"] is not None:
        sign = "+" if a["pct"] >= 0 else ""
        status = "BEATING SPY" if a["beating_spy"] else "BEHIND SPY"
        print(f"    Alpha:         {sign}{a['pct']:.3f}%")
        print(f"    Status:        {status}")
    else:
        print("    Alpha:         N/A (no percentage P&L data)")
    print("=" * 70)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Fleet P&L vs SPY Benchmark")
    parser.add_argument("--report", action="store_true", help="Print full benchmark comparison")
    parser.add_argument("--daily", action="store_true", help="Add today's snapshot to history")
    parser.add_argument("--history", action="store_true", help="Show running alpha over time")
    parser.add_argument("--start", type=str, default=None, help=f"Start date (default: {DEFAULT_START})")
    parser.add_argument("--end", type=str, default=None, help="End date (default: today)")
    args = parser.parse_args()

    if not any([args.report, args.daily, args.history]):
        args.report = True

    if args.report or args.daily:
        result = compute_fleet_benchmark(start_date=args.start, end_date=args.end)
        if args.report:
            print_report(result)
        if args.daily:
            save_daily_snapshot(result)

    if args.history:
        show_history()


if __name__ == "__main__":
    main()
