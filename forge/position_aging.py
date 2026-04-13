"""
Position Aging / Time Stops
=============================
Fleet-wide scan of all open positions across all systems.
Flags aging positions that are tying up capital.

Usage:
    from forge.position_aging import scan_aging_positions

    aging = scan_aging_positions()
    for pos in aging:
        if pos["status"] in ("WARNING", "FORCE_REVIEW"):
            alert(pos)

CLI:
    python -m forge.position_aging --scan
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Position file locations
# ---------------------------------------------------------------------------

POSITION_SOURCES = {
    "titan": REPO / "titan" / "logs" / "positions.json",
    "hermes": REPO / "hermes" / "logs" / "positions.json",
    "apollo": REPO / "apollo" / "logs" / "positions.json",
    "ares": REPO / "ares" / "logs" / "positions.json",
}

GDX_GLD_TRADES = REPO / "forge" / "logs" / "gdx_gld" / "trades.csv"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_entry_date(raw: str) -> Optional[date]:
    """Parse entry_date from various formats."""
    if not raw:
        return None
    try:
        # ISO timestamp with timezone
        return datetime.fromisoformat(raw).date()
    except (ValueError, TypeError):
        pass
    try:
        return date.fromisoformat(raw[:10])
    except (ValueError, TypeError):
        pass
    return None


def _get_current_price(ticker: str) -> Optional[float]:
    """Get latest price for unrealized P&L calculation."""
    try:
        import yfinance as yf
        tk = yf.Ticker(ticker)
        hist = tk.history(period="1d")
        if hist is not None and not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception as e:
        log.debug("Price fetch failed for %s: %s", ticker, e)
    return None


def _compute_unrealized_pnl(entry_price: float, current_price: float,
                             direction: str) -> float:
    """Return unrealized P&L as a percentage."""
    if entry_price <= 0:
        return 0.0
    if direction.upper() in ("LONG", "LONG_SPREAD"):
        return round((current_price - entry_price) / entry_price * 100, 2)
    else:
        return round((entry_price - current_price) / entry_price * 100, 2)


# ---------------------------------------------------------------------------
# Core scan
# ---------------------------------------------------------------------------

def _load_json_positions(system: str, path: Path) -> list[dict]:
    """Load positions from a JSON positions file."""
    if not path.exists():
        return []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.debug("Failed to read %s: %s", path, e)
        return []

    if not data or not isinstance(data, dict):
        return []

    positions = []
    today = date.today()

    for ticker, info in data.items():
        direction = info.get("direction", "LONG")
        entry_date_str = info.get("entry_date", "")
        entry_price = info.get("entry_price", 0.0)

        entry_dt = _parse_entry_date(entry_date_str)
        if entry_dt is None:
            log.debug("No entry date for %s in %s, using today", ticker, system)
            entry_dt = today

        days_held = (today - entry_dt).days

        # Try to compute unrealized P&L
        current_price = _get_current_price(ticker)
        if current_price is not None and entry_price:
            unrealized = _compute_unrealized_pnl(entry_price, current_price, direction)
        else:
            unrealized = None

        positions.append({
            "system": system,
            "ticker": ticker,
            "direction": direction,
            "entry_date": entry_dt.isoformat(),
            "entry_price": entry_price,
            "current_price": current_price,
            "days_held": days_held,
            "unrealized_pnl_pct": unrealized,
        })

    return positions


def _load_gdx_gld_open() -> list[dict]:
    """Check GDX/GLD trades.csv for any open positions (no exit_date)."""
    if not GDX_GLD_TRADES.exists():
        return []

    positions = []
    today = date.today()

    try:
        with open(GDX_GLD_TRADES, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                exit_date = row.get("exit_date", "").strip()
                # Open position = no exit date or empty
                if exit_date:
                    continue

                entry_date_str = row.get("entry_date", "")
                entry_dt = _parse_entry_date(entry_date_str)
                if entry_dt is None:
                    continue

                direction = row.get("direction", "LONG_SPREAD")
                days_held = (today - entry_dt).days

                positions.append({
                    "system": "gdx_gld",
                    "ticker": "GDX/GLD",
                    "direction": direction,
                    "entry_date": entry_dt.isoformat(),
                    "entry_price": None,
                    "current_price": None,
                    "days_held": days_held,
                    "unrealized_pnl_pct": None,
                })
    except Exception as e:
        log.debug("Failed to read GDX/GLD trades: %s", e)

    return positions


def _classify_position(pos: dict, warn_days: int, force_days: int) -> dict:
    """Add status and recommendation to a position dict."""
    days = pos["days_held"]
    pnl = pos["unrealized_pnl_pct"]

    if days < warn_days:
        pos["status"] = "OK"
        pnl_str = f"{pnl:+.1f}%" if pnl is not None else "N/A"
        pos["recommendation"] = f"Held {days}d, P&L {pnl_str} -- within normal range"

    elif days < force_days:
        if pnl is not None and pnl > 2.0:
            pos["status"] = "OK"
            pos["recommendation"] = f"Held {days}d but +{pnl:.1f}% gain -- let it run"
        elif pnl is not None and pnl < -2.0:
            pos["status"] = "WARNING"
            pos["recommendation"] = (
                f"Review -- held {days}d with {pnl:+.1f}% loss, consider cutting"
            )
        else:
            pos["status"] = "WARNING"
            pnl_str = f"{pnl:+.1f}%" if pnl is not None else "N/A"
            pos["recommendation"] = (
                f"Review -- held {days}d with only {pnl_str} gain, capital tied up"
            )

    else:
        pos["status"] = "FORCE_REVIEW"
        pnl_str = f"{pnl:+.1f}%" if pnl is not None else "N/A"
        pos["recommendation"] = (
            f"FORCE REVIEW -- held {days}d (>{force_days}d), P&L {pnl_str}"
        )

    return pos


def scan_aging_positions(
    warn_days: int = 20,
    force_days: int = 40,
) -> list[dict]:
    """
    Scan all systems for positions held too long.

    Returns list of position dicts with status/recommendation fields.

    Thresholds:
        < warn_days: OK
        warn_days to force_days: WARNING if unrealized < 2%, OK if > 2%
        > force_days: FORCE_REVIEW regardless of P&L
    """
    all_positions = []

    # JSON position files
    for system, path in POSITION_SOURCES.items():
        positions = _load_json_positions(system, path)
        all_positions.extend(positions)

    # GDX/GLD open trades
    all_positions.extend(_load_gdx_gld_open())

    # Classify each position
    for pos in all_positions:
        _classify_position(pos, warn_days, force_days)

    # Sort: worst status first, then by days held descending
    status_order = {"FORCE_REVIEW": 0, "WARNING": 1, "OK": 2}
    all_positions.sort(
        key=lambda p: (status_order.get(p["status"], 3), -p["days_held"])
    )

    return all_positions


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_scan(positions: list[dict]) -> None:
    """Pretty-print the aging scan results."""
    print()
    print("=" * 75)
    print("POSITION AGING SCAN")
    print("=" * 75)

    if not positions:
        print("  No open positions found across any system.")
        print("=" * 75)
        return

    print(
        f"  {'System':<10} {'Ticker':<10} {'Dir':<8} {'Days':>5} "
        f"{'P&L':>8} {'Status':<14} Recommendation"
    )
    print("  " + "-" * 73)

    for pos in positions:
        pnl_str = f"{pos['unrealized_pnl_pct']:+.1f}%" if pos["unrealized_pnl_pct"] is not None else "N/A"
        print(
            f"  {pos['system']:<10} {pos['ticker']:<10} {pos['direction']:<8} "
            f"{pos['days_held']:>5}d {pnl_str:>8} {pos['status']:<14} "
            f"{pos['recommendation']}"
        )

    print()

    # Summary
    statuses = [p["status"] for p in positions]
    n_ok = statuses.count("OK")
    n_warn = statuses.count("WARNING")
    n_force = statuses.count("FORCE_REVIEW")

    print(
        f"  Summary: {len(positions)} positions | "
        f"{n_ok} OK | {n_warn} WARNING | {n_force} FORCE_REVIEW"
    )
    print("=" * 75)
    print()


def main():
    parser = argparse.ArgumentParser(description="Position Aging / Time Stops")
    parser.add_argument(
        "--scan", action="store_true",
        help="Scan all positions and print aging status",
    )
    parser.add_argument(
        "--warn-days", type=int, default=20,
        help="Days before WARNING threshold (default: 20)",
    )
    parser.add_argument(
        "--force-days", type=int, default=40,
        help="Days before FORCE_REVIEW threshold (default: 40)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )

    if args.scan:
        positions = scan_aging_positions(
            warn_days=args.warn_days,
            force_days=args.force_days,
        )
        _print_scan(positions)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
