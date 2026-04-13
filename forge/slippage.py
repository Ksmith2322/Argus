"""
Slippage Measurement
====================
Compares expected fills to actual fills across all systems.
Measures how much execution cost eats into theoretical edge.

Usage:
    python -m forge.slippage --report
"""

import argparse
import csv
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------
# Argus trades.csv has: signal_mid, entry_px, direction, slippage_pips, pnl_usd
# Apollo/Helio trades don't have signal price — skip those for slippage
# Orders.csv in ops/logs has fill data but different format

ARGUS_LOGS = REPO / "argus_flow" / "logs"
OPS_ORDERS = REPO / "ops" / "logs" / "orders.csv"


def _parse_argus_slippage() -> dict:
    """
    Parse slippage from Argus trades.csv files.
    Argus has signal_mid (price when signal fired) and entry_px (actual fill).
    Also has slippage_pips column directly.
    """
    trades = []
    if not ARGUS_LOGS.exists():
        return {"trades": 0, "error": "argus logs not found"}

    for csv_path in sorted(ARGUS_LOGS.glob("*/trades.csv")):
        pair = csv_path.parent.name
        try:
            with open(csv_path, "r", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    direction = row.get("direction", "").lower()
                    entry_px_str = row.get("entry_px", "")
                    signal_mid_str = row.get("signal_mid", "")
                    slip_pips_str = row.get("slippage_pips", "")
                    pnl_usd_str = row.get("pnl_usd", "")

                    if not entry_px_str or not direction:
                        continue

                    try:
                        entry_px = float(entry_px_str)
                    except (ValueError, TypeError):
                        continue

                    # Use slippage_pips if available
                    slip_pips = None
                    if slip_pips_str:
                        try:
                            slip_pips = float(slip_pips_str)
                        except (ValueError, TypeError):
                            pass

                    # Compute from signal_mid if available
                    signal_mid = None
                    slip_bps = None
                    if signal_mid_str:
                        try:
                            signal_mid = float(signal_mid_str)
                        except (ValueError, TypeError):
                            pass

                    if signal_mid and signal_mid > 0 and entry_px > 0:
                        raw_slip = (entry_px - signal_mid) / signal_mid * 10000
                        # For buys: positive = bad (paid more)
                        # For sells: negative = bad (received less)
                        if direction in ("short", "sell"):
                            slip_bps = -raw_slip  # Flip for sells
                        else:
                            slip_bps = raw_slip
                    elif slip_pips is not None:
                        # Convert pips to approx bps (rough: 1 pip ~ 1 bps for FX)
                        slip_bps = slip_pips

                    pnl_usd = None
                    if pnl_usd_str:
                        try:
                            pnl_usd = float(pnl_usd_str)
                        except (ValueError, TypeError):
                            pass

                    trades.append({
                        "pair": pair,
                        "direction": direction,
                        "entry_px": entry_px,
                        "signal_mid": signal_mid,
                        "slip_bps": slip_bps,
                        "slip_pips": slip_pips,
                        "pnl_usd": pnl_usd,
                    })
        except Exception:
            continue

    return _aggregate_trades("argus", trades)


def _parse_orders_csv_slippage() -> dict:
    """
    Parse slippage from ops/logs/orders.csv.
    Has limit_px vs avg_fill_px for filled orders.
    """
    trades = []
    if not OPS_ORDERS.exists():
        return {"trades": 0, "note": "orders.csv not found"}

    try:
        with open(OPS_ORDERS, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                status = row.get("status", "")
                if status != "FILLED":
                    continue

                side = row.get("side", "").upper()
                symbol = row.get("symbol", "unknown")
                avg_fill_str = row.get("avg_fill_px", "")
                limit_str = row.get("limit_px", "")

                try:
                    fill_px = float(avg_fill_str) if avg_fill_str else None
                except (ValueError, TypeError):
                    fill_px = None

                try:
                    limit_px = float(limit_str) if limit_str else None
                except (ValueError, TypeError):
                    limit_px = None

                if fill_px is None or fill_px == 0:
                    continue

                slip_bps = None
                signal_px = limit_px  # limit price is the intended fill

                if signal_px and signal_px > 0:
                    raw_slip = (fill_px - signal_px) / signal_px * 10000
                    if side == "SELL":
                        slip_bps = -raw_slip
                    else:
                        slip_bps = raw_slip

                trades.append({
                    "pair": symbol,
                    "direction": side.lower(),
                    "entry_px": fill_px,
                    "signal_mid": signal_px,
                    "slip_bps": slip_bps,
                    "slip_pips": None,
                    "pnl_usd": None,
                })
    except Exception:
        return {"trades": 0, "error": "failed to parse orders.csv"}

    return _aggregate_trades("ops_orders", trades)


def _aggregate_trades(system: str, trades: list[dict]) -> dict:
    """Aggregate trade-level slippage into system summary."""
    if not trades:
        return {"system": system, "trades": 0}

    slips = [t["slip_bps"] for t in trades if t["slip_bps"] is not None]
    pnls = [t["pnl_usd"] for t in trades if t["pnl_usd"] is not None]

    result = {
        "system": system,
        "trades": len(trades),
        "trades_with_slippage_data": len(slips),
    }

    if slips:
        avg_slip = sum(slips) / len(slips)
        total_slip_usd = 0.0  # Approximate from pnl relationship
        worst = max(slips, key=abs)
        worst_trade = None
        for t in trades:
            if t["slip_bps"] is not None and abs(t["slip_bps"] - worst) < 0.001:
                worst_trade = t
                break

        result.update({
            "avg_slippage_bps": round(avg_slip, 2),
            "median_slippage_bps": round(sorted(slips)[len(slips) // 2], 2),
            "max_slippage_bps": round(max(slips), 2),
            "min_slippage_bps": round(min(slips), 2),
            "std_slippage_bps": round(_std(slips), 2),
            "pct_positive_slip": round(sum(1 for s in slips if s > 0) / len(slips) * 100, 1),
        })

        if worst_trade:
            result["worst_fill"] = {
                "ticker": worst_trade["pair"],
                "direction": worst_trade["direction"],
                "expected": worst_trade["signal_mid"],
                "actual": worst_trade["entry_px"],
                "slip_bps": round(worst, 2),
            }

        # Estimate slippage cost as % of total P&L
        if pnls:
            gross_pnl = sum(abs(p) for p in pnls)
            if gross_pnl > 0:
                # Rough: avg slip in bps * number of trades * avg position
                # Simpler: just report the avg and let user interpret
                result["total_gross_pnl_usd"] = round(sum(pnls), 2)

    return result


def _std(values: list[float]) -> float:
    """Standard deviation."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return variance ** 0.5


# ---------------------------------------------------------------------------
# Main measurement
# ---------------------------------------------------------------------------

def measure_slippage() -> dict:
    """
    Returns per-system slippage analysis.
    """
    results = {}

    # Argus (has signal_mid in trades.csv)
    argus = _parse_argus_slippage()
    if argus.get("trades", 0) > 0:
        results["argus"] = argus

    # OMS orders (ops/logs/orders.csv)
    ops = _parse_orders_csv_slippage()
    if ops.get("trades", 0) > 0:
        results["ops_orders"] = ops

    # Titan, Hermes, Apollo — no signal price data in their CSVs yet
    for sys_name in ["titan", "hermes", "apollo"]:
        results[sys_name] = {
            "system": sys_name,
            "trades": 0,
            "note": "No signal price data in CSV — slippage measurement not available yet",
        }

    return results


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(results: dict) -> None:
    """Print formatted slippage report."""
    print("\n" + "=" * 70)
    print("SLIPPAGE ANALYSIS REPORT")
    print("=" * 70)

    for sys_name, data in sorted(results.items()):
        print(f"\n  {sys_name.upper()}")
        print("  " + "-" * 40)

        if data.get("note"):
            print(f"    {data['note']}")
            continue

        if data.get("error"):
            print(f"    Error: {data['error']}")
            continue

        trades = data.get("trades", 0)
        if trades == 0:
            print("    No trades found")
            continue

        with_data = data.get("trades_with_slippage_data", 0)
        print(f"    Trades:              {trades}")
        print(f"    With slippage data:  {with_data}")

        if "avg_slippage_bps" in data:
            avg = data["avg_slippage_bps"]
            med = data["median_slippage_bps"]
            std = data["std_slippage_bps"]
            pct_pos = data["pct_positive_slip"]
            print(f"    Avg slippage:        {avg:+.2f} bps")
            print(f"    Median slippage:     {med:+.2f} bps")
            print(f"    Std dev:             {std:.2f} bps")
            print(f"    Max adverse:         {data['max_slippage_bps']:+.2f} bps")
            print(f"    Min (favorable):     {data['min_slippage_bps']:+.2f} bps")
            print(f"    % adverse fills:     {pct_pos:.1f}%")

            if "total_gross_pnl_usd" in data:
                print(f"    Total gross P&L:     ${data['total_gross_pnl_usd']:,.2f}")

            if "worst_fill" in data:
                wf = data["worst_fill"]
                print(f"    Worst fill:          {wf['ticker']} {wf['direction']}")
                exp = f"${wf['expected']:.4f}" if wf['expected'] else "N/A"
                print(f"      Expected: {exp}  Actual: ${wf['actual']:.4f}  Slip: {wf['slip_bps']:+.2f} bps")

    print("\n" + "=" * 70)
    print("  Note: Positive slippage = adverse (paid more on buys / received less on sells)")
    print("  Argus records signal_mid and slippage_pips in trades.csv.")
    print("  Other systems need signal price logging to enable slippage tracking.")
    print("=" * 70)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Slippage Measurement")
    parser.add_argument("--report", action="store_true", help="Print full slippage analysis")
    args = parser.parse_args()

    # Default to report
    results = measure_slippage()
    print_report(results)


if __name__ == "__main__":
    main()
