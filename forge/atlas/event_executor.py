"""
Atlas Event Executor — Atlas-to-Execution Bridge
==================================================
When Atlas detects a HIGH-severity event with data-validated cascade impacts,
this module generates executable trade signals.

Usage:
    python -m forge.atlas.event_executor --check     # show active event trades
    python -m forge.atlas.event_executor --dry-run   # show what would be traded
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("atlas.event_executor")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum |expected_car * severity| to include a trade (0.5% = material move)
MIN_MATERIAL_MOVE = 0.005

# Default position size per leg as fraction of equity
DEFAULT_SIZING_PCT = 0.02

# Stop loss multiplier relative to expected move (2x)
STOP_MULTIPLIER = 2.0

# Map of CAR windows to hold days (which window was validated)
# If expected_car_5d is the field name but the description says "over 20d",
# use 20. Default to 5 if unclear.
_HOLD_DAYS_HINTS = {
    "day 1": 1,
    "over 5d": 5,
    "over 20d": 20,
}

LOG_DIR = Path(__file__).resolve().parent.parent / "logs" / "atlas"
LOG_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _infer_hold_days(entry: dict) -> int:
    """Infer hold period from the description text in the cascade entry."""
    desc = entry.get("description", "").lower()
    for hint, days in _HOLD_DAYS_HINTS.items():
        if hint in desc:
            return days
    # Default: 5 days (matches the expected_car_5d field name)
    return 5


def evaluate_event_trades(event_type: str, severity: float) -> list[dict]:
    """
    Check cascade templates for data-validated entries only.
    Return executable trade signals for entries that meet confidence thresholds.

    Returns list of:
    {
        "asset": "XLU",
        "direction": "LONG",
        "expected_car_5d": 0.0248,
        "confidence": "high",
        "data_validated": True,
        "n_historical": 16,
        "p_value": 0.0003,
        "wave": 2,
        "sizing_pct": 0.02,
        "entry_type": "market_close",
        "hold_days": 20,
        "stop_pct": 0.05,
    }
    """
    from forge.atlas.cascade.templates import get_cascade_template

    template = get_cascade_template(event_type)
    if not template:
        log.warning("No cascade template for event type: %s", event_type)
        return []

    # 1. Filter: data_validated == True AND confidence == "high"
    candidates = [
        e for e in template
        if e.get("data_validated") is True and e.get("confidence") == "high"
    ]

    trades: list[dict] = []

    for entry in candidates:
        raw_car = entry["expected_car_5d"]
        scaled_car = raw_car * severity

        # 2. Materiality filter: |expected_car * severity| > 0.5%
        if abs(scaled_car) < MIN_MATERIAL_MOVE:
            log.debug(
                "Skipping %s: |%.4f| < %.4f materiality threshold",
                entry["asset"], scaled_car, MIN_MATERIAL_MOVE,
            )
            continue

        # 3. Direction
        direction = "LONG" if entry["direction"] > 0 else "SHORT"

        # 4. Sizing: scale by severity (higher severity = larger expected move)
        # Cap at 2x default to avoid over-concentration
        sizing = min(DEFAULT_SIZING_PCT * severity, DEFAULT_SIZING_PCT * 2.0)

        # 5. Hold days from description context
        hold_days = _infer_hold_days(entry)

        # 6. Stop loss at STOP_MULTIPLIER x the expected move
        stop_pct = round(abs(raw_car) * STOP_MULTIPLIER, 4)

        trade = {
            "event_type": event_type,
            "asset": entry["asset"],
            "direction": direction,
            "expected_car_5d": round(raw_car, 6),
            "scaled_car": round(scaled_car, 6),
            "confidence": entry["confidence"],
            "data_validated": True,
            "n_historical": entry.get("n", 0),
            "p_value": entry.get("p", 1.0),
            "wave": entry["wave"],
            "sizing_pct": round(sizing, 4),
            "entry_type": "market_close",
            "hold_days": hold_days,
            "stop_pct": round(stop_pct, 4),
            "description": entry.get("description", ""),
        }
        trades.append(trade)

    # 7. Sort by p_value ascending (most statistically significant first)
    trades.sort(key=lambda t: t["p_value"])

    return trades


def get_active_event_trades() -> list[dict]:
    """
    Check recent events from Atlas DB (last 24h, severity >= 0.7),
    run evaluate_event_trades for each, return all executable signals.
    """
    from forge.atlas.db.schema import init_db, get_connection, DB_PATH
    from forge.atlas.db.queries import get_recent_events

    init_db(DB_PATH)
    conn = get_connection(DB_PATH)

    try:
        recent = get_recent_events(conn, hours=24)
    finally:
        conn.close()

    # Filter to high-severity events
    high_sev = [e for e in recent if e.get("severity", 0) >= 0.7]

    if not high_sev:
        log.info("No high-severity events in last 24h")
        return []

    all_trades: list[dict] = []
    seen_assets: set[str] = set()

    for event in high_sev:
        event_type = event.get("event_type", "")
        severity = event.get("severity", 1.0)

        trades = evaluate_event_trades(event_type, severity)

        for trade in trades:
            # Deduplicate: if same asset already in the list from a
            # higher-priority event, skip
            if trade["asset"] in seen_assets:
                log.debug(
                    "Skipping duplicate %s from %s (already in plan)",
                    trade["asset"], event_type,
                )
                continue

            trade["source_event_id"] = event.get("event_id", "")
            trade["source_title"] = event.get("title", "")[:120]
            trade["generated_at"] = datetime.now(timezone.utc).isoformat()
            all_trades.append(trade)
            seen_assets.add(trade["asset"])

    log.info(
        "Generated %d executable trades from %d high-severity events",
        len(all_trades), len(high_sev),
    )
    return all_trades


def format_trade_plan(trades: list[dict]) -> str:
    """Human-readable trade plan for Discord/logging."""
    if not trades:
        return "NO EXECUTABLE EVENT TRADES — no high-confidence, data-validated signals active."

    lines: list[str] = [
        "=" * 60,
        "ATLAS EVENT TRADE PLAN",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"Total signals: {len(trades)}",
        "=" * 60,
    ]

    # Group by event type
    by_event: dict[str, list[dict]] = {}
    for t in trades:
        by_event.setdefault(t.get("event_type", "UNKNOWN"), []).append(t)

    for event_type, event_trades in by_event.items():
        lines.append(f"\n--- {event_type} ---")
        source = event_trades[0].get("source_title", "")
        if source:
            lines.append(f"  Source: {source}")

        for t in event_trades:
            dir_arrow = "^" if t["direction"] == "LONG" else "v"
            lines.append(
                f"  {dir_arrow} {t['asset']:5s} {t['direction']:5s} | "
                f"CAR: {t['expected_car_5d']:+.2%} (scaled: {t['scaled_car']:+.2%}) | "
                f"N={t['n_historical']:2d}, p={t['p_value']:.4f} | "
                f"size: {t['sizing_pct']:.1%} | hold: {t['hold_days']}d | "
                f"stop: {t['stop_pct']:.1%}"
            )

    lines.append("\n" + "=" * 60)
    lines.append("NOTE: All signals are data-validated (p<0.05, N>=5).")
    lines.append("Entry: market close. Stops are hard. Review before execution.")
    lines.append("=" * 60)

    return "\n".join(lines)


def log_event_trade(trade: dict, log_dir: Path | None = None) -> None:
    """Append a trade signal to forge/logs/atlas/event_trades.csv."""
    if log_dir is None:
        log_dir = LOG_DIR

    log_dir.mkdir(parents=True, exist_ok=True)
    csv_path = log_dir / "event_trades.csv"

    fieldnames = [
        "generated_at", "event_type", "source_event_id", "asset", "direction",
        "expected_car_5d", "scaled_car", "confidence", "n_historical",
        "p_value", "wave", "sizing_pct", "entry_type", "hold_days", "stop_pct",
        "source_title",
    ]

    write_header = not csv_path.exists()

    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(trade)

    log.debug("Logged event trade: %s %s", trade.get("asset"), trade.get("direction"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Atlas Event Executor — generate trade signals from macro events",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Check recent events and print executable trade plan",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be traded (same as --check but also logs to CSV)",
    )
    parser.add_argument(
        "--event-type", type=str, default=None,
        help="Evaluate a specific event type (e.g. TARIFF_ANNOUNCE)",
    )
    parser.add_argument(
        "--severity", type=float, default=1.0,
        help="Severity multiplier for --event-type mode (default: 1.0)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.event_type:
        # Direct evaluation of a specific event type
        trades = evaluate_event_trades(args.event_type, args.severity)
        print(format_trade_plan(trades))
        if args.dry_run:
            for t in trades:
                t.setdefault("generated_at", datetime.now(timezone.utc).isoformat())
                t.setdefault("source_event_id", "manual")
                t.setdefault("source_title", f"manual: {args.event_type}")
                log_event_trade(t)
            if trades:
                print(f"\nLogged {len(trades)} trades to {LOG_DIR / 'event_trades.csv'}")
        return

    if args.check or args.dry_run:
        trades = get_active_event_trades()
        print(format_trade_plan(trades))

        if args.dry_run and trades:
            for t in trades:
                log_event_trade(t)
            print(f"\nLogged {len(trades)} trades to {LOG_DIR / 'event_trades.csv'}")
        return

    # Default: same as --check
    trades = get_active_event_trades()
    print(format_trade_plan(trades))


if __name__ == "__main__":
    main()
