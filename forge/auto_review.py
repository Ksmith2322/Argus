"""
Automated Friday Review Report
================================
Auto-generates the weekly burn-in review.
Aggregates all fleet data into a single formatted report.

Usage:
    from forge.auto_review import generate_weekly_review

    report = generate_weekly_review()
    print(report)

CLI:
    python -m forge.auto_review --generate
    python -m forge.auto_review --discord
    python -m forge.auto_review --save
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent
FORGE = REPO / "forge"
FORGE_DATA = FORGE / "data"
REVIEWS_DIR = FORGE_DATA / "reviews"

# Discord
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# Heartbeat file locations
HEARTBEAT_SOURCES = {
    "titan": REPO / "titan" / "logs" / "heartbeat.json",
    "hermes": REPO / "hermes" / "logs" / "heartbeat.json",
    "apollo": REPO / "apollo" / "logs" / "heartbeat.json",
    "ares": REPO / "ares" / "logs" / "heartbeat.json",
    "atlas": FORGE / "logs" / "atlas" / "heartbeat.json",
    "gdx_gld": FORGE / "logs" / "gdx_gld" / "heartbeat.json",
    "themis": FORGE / "logs" / "themis" / "heartbeat.json",
    "rebalance": FORGE / "logs" / "rebalance" / "heartbeat.json",
}

# Trade CSV locations (matches benchmark.py)
SYSTEM_TRADE_PATHS = {
    "argus": REPO / "argus_flow" / "logs",
    "apollo": REPO / "helio" / "logs",
    "forge_gdx_gld": FORGE / "logs" / "gdx_gld",
    "titan": REPO / "titan" / "logs",
    "hermes": REPO / "hermes" / "logs",
}

# Argus heartbeats (one per pair)
ARGUS_LOGS = REPO / "argus_flow" / "logs"

STALE_THRESHOLD_MINUTES = 15


# =========================================================================
# Section 1: Fleet Health
# =========================================================================

def _section_fleet_health() -> str:
    lines = ["## 1. Fleet Health\n"]
    now = datetime.now(timezone.utc)

    # Check named systems
    for system, path in HEARTBEAT_SOURCES.items():
        if not path.exists():
            lines.append(f"- **{system}**: no heartbeat file")
            continue
        try:
            hb = json.loads(path.read_text(encoding="utf-8"))
            ts_str = hb.get("ts", "")
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_min = (now - ts).total_seconds() / 60
            status = "ALIVE" if age_min < STALE_THRESHOLD_MINUTES else f"STALE ({age_min:.0f}m)"
            extra = ""
            if "regime" in hb:
                extra += f" regime={hb['regime']}"
            if "mode" in hb:
                extra += f" mode={hb['mode']}"
            lines.append(f"- **{system}**: {status}{extra}")
        except Exception as e:
            lines.append(f"- **{system}**: error reading heartbeat ({e})")

    # Check Argus pairs
    argus_alive = 0
    argus_stale = 0
    argus_total = 0
    if ARGUS_LOGS.exists():
        for hb_path in sorted(ARGUS_LOGS.glob("*/heartbeat.json")):
            argus_total += 1
            try:
                hb = json.loads(hb_path.read_text(encoding="utf-8"))
                ts = datetime.fromisoformat(hb.get("ts", ""))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age_min = (now - ts).total_seconds() / 60
                if age_min < STALE_THRESHOLD_MINUTES:
                    argus_alive += 1
                else:
                    argus_stale += 1
            except Exception:
                argus_stale += 1

    lines.append(
        f"- **argus**: {argus_alive}/{argus_total} pairs alive, "
        f"{argus_stale} stale"
    )
    lines.append("")
    return "\n".join(lines)


# =========================================================================
# Section 2: Trade Summary
# =========================================================================

def _read_trades_for_period(csv_path: Path, start: date, end: date) -> list[dict]:
    """Read closed trades in date range from a CSV."""
    trades = []
    try:
        with open(csv_path, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Try common date fields
                trade_date = None
                for field in ("ts", "exit_date", "entry_date"):
                    val = row.get(field, "")
                    if val:
                        try:
                            trade_date = datetime.fromisoformat(val).date()
                            break
                        except (ValueError, TypeError):
                            try:
                                trade_date = date.fromisoformat(val[:10])
                                break
                            except (ValueError, TypeError):
                                continue

                if trade_date is None or trade_date < start or trade_date > end:
                    continue

                pnl_pct = None
                pnl_usd = None
                for pfield in ("pnl_pct",):
                    if row.get(pfield):
                        try:
                            pnl_pct = float(row[pfield])
                        except (ValueError, TypeError):
                            pass
                for ufield in ("pnl_usd",):
                    if row.get(ufield):
                        try:
                            pnl_usd = float(row[ufield])
                        except (ValueError, TypeError):
                            pass

                trades.append({
                    "date": trade_date.isoformat(),
                    "pnl_pct": pnl_pct,
                    "pnl_usd": pnl_usd,
                    "direction": row.get("direction", ""),
                })
    except Exception:
        pass
    return trades


def _section_trade_summary(days: int) -> str:
    end = date.today()
    start = end - timedelta(days=days)

    lines = [f"## 2. Trade Summary (last {days}d: {start} to {end})\n"]

    total_trades = 0
    total_wins = 0
    total_pnl_pct = 0.0
    total_pnl_usd = 0.0

    system_rows = []

    for sys_name, base_path in SYSTEM_TRADE_PATHS.items():
        if not base_path.exists():
            continue

        # Find trades.csv files
        csvs = []
        if sys_name == "argus":
            csvs = sorted(base_path.glob("*/trades.csv"))
        elif sys_name == "apollo":
            csvs = sorted(base_path.glob("apollo_*/trades.csv"))
        else:
            tc = base_path / "trades.csv"
            if tc.exists():
                csvs = [tc]

        sys_trades = []
        for cp in csvs:
            sys_trades.extend(_read_trades_for_period(cp, start, end))

        if not sys_trades:
            continue

        wins = sum(1 for t in sys_trades if (t["pnl_pct"] or 0) > 0 or
                   (t["pnl_pct"] is None and (t["pnl_usd"] or 0) > 0))
        pnl_pct = sum(t["pnl_pct"] for t in sys_trades if t["pnl_pct"] is not None)
        pnl_usd = sum(t["pnl_usd"] for t in sys_trades if t["pnl_usd"] is not None)
        wr = wins / len(sys_trades) if sys_trades else 0

        system_rows.append((sys_name, len(sys_trades), wins, wr, pnl_pct, pnl_usd))
        total_trades += len(sys_trades)
        total_wins += wins
        total_pnl_pct += pnl_pct
        total_pnl_usd += pnl_usd

    if not system_rows:
        lines.append("No trades this period.\n")
        return "\n".join(lines)

    lines.append("| System | Trades | Wins | WR | P&L % | P&L $ |")
    lines.append("|--------|--------|------|----|-------|-------|")
    for name, count, wins, wr, pnl_p, pnl_u in system_rows:
        lines.append(
            f"| {name} | {count} | {wins} | {wr:.0%} | "
            f"{pnl_p:+.2f}% | ${pnl_u:+.2f} |"
        )

    total_wr = total_wins / total_trades if total_trades else 0
    lines.append(
        f"| **TOTAL** | **{total_trades}** | **{total_wins}** | "
        f"**{total_wr:.0%}** | **{total_pnl_pct:+.2f}%** | "
        f"**${total_pnl_usd:+.2f}** |"
    )
    lines.append("")
    return "\n".join(lines)


# =========================================================================
# Section 3: Benchmark
# =========================================================================

def _section_benchmark() -> str:
    lines = ["## 3. Fleet vs SPY Benchmark\n"]
    try:
        from forge.benchmark import compute_fleet_benchmark
        result = compute_fleet_benchmark()
        f = result["fleet"]
        b = result["benchmark"]
        a = result["alpha"]

        pct_str = f"{f['total_pnl_pct']:.3f}%" if f["total_pnl_pct"] is not None else "N/A"
        lines.append(f"- Fleet cumulative P&L: {pct_str} (${f['total_pnl_usd']:,.2f})")
        lines.append(f"- SPY buy-and-hold: {b['spy_return_pct']:.3f}%")
        if a["pct"] is not None:
            sign = "+" if a["pct"] >= 0 else ""
            status = "BEATING" if a["beating_spy"] else "BEHIND"
            lines.append(f"- Alpha: {sign}{a['pct']:.3f}% ({status})")
        else:
            lines.append("- Alpha: N/A")
    except Exception as e:
        lines.append(f"- Benchmark unavailable: {e}")
    lines.append("")
    return "\n".join(lines)


# =========================================================================
# Section 4: Slippage
# =========================================================================

def _section_slippage() -> str:
    lines = ["## 4. Slippage Report\n"]
    try:
        from forge.slippage import measure_slippage
        results = measure_slippage()

        for sys_name, data in results.items():
            if isinstance(data, dict) and "mean_bps" in data:
                lines.append(
                    f"- **{sys_name}**: mean {data['mean_bps']:.1f} bps, "
                    f"median {data.get('median_bps', 0):.1f} bps "
                    f"({data.get('trades', 0)} trades)"
                )
            elif isinstance(data, dict) and "error" in data:
                lines.append(f"- **{sys_name}**: {data['error']}")
    except Exception as e:
        lines.append(f"- Slippage data unavailable: {e}")
    lines.append("")
    return "\n".join(lines)


# =========================================================================
# Section 5: Atlas Intelligence
# =========================================================================

def _section_atlas() -> str:
    lines = ["## 5. Atlas Intelligence\n"]
    try:
        from forge.atlas.fleet_gate import check_atlas
        gate = check_atlas()

        lines.append(f"- Regime: **{gate.regime}**")
        lines.append(f"- Alert level: {gate.alert_level}")
        lines.append(f"- Size modifier: {gate.size_modifier}")
        if gate.sectors_avoid:
            lines.append(f"- Sectors to avoid: {', '.join(gate.sectors_avoid)}")
        if gate.sectors_favor:
            lines.append(f"- Sectors favored: {', '.join(gate.sectors_favor)}")
        if gate.event_window:
            lines.append(f"- Upcoming event: {gate.upcoming_event}")
        if not gate.available:
            lines.append("- Status: UNAVAILABLE" + (" (stale)" if gate.stale else ""))
    except Exception as e:
        lines.append(f"- Atlas unavailable: {e}")
    lines.append("")
    return "\n".join(lines)


# =========================================================================
# Section 6: Themis Signals
# =========================================================================

def _section_themis() -> str:
    lines = ["## 6. Themis Congressional Signals\n"]
    try:
        from forge.themis.db import get_connection, DB_PATH
        if not Path(DB_PATH).exists():
            lines.append("- Themis DB not found")
            return "\n".join(lines) + "\n"

        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        conn = get_connection()
        try:
            rows = conn.execute("""
                SELECT ticker, signal_type, member_count, description, detected_at
                FROM signals
                WHERE detected_at >= ?
                ORDER BY detected_at DESC
                LIMIT 10
            """, (cutoff,)).fetchall()

            if not rows:
                lines.append("- No new signals this week")
            else:
                for row in rows:
                    lines.append(
                        f"- {row['detected_at']}: **{row['ticker']}** "
                        f"{row['signal_type']} ({row['member_count']} members) "
                        f"-- {row['description']}"
                    )
        finally:
            conn.close()
    except Exception as e:
        lines.append(f"- Themis unavailable: {e}")
    lines.append("")
    return "\n".join(lines)


# =========================================================================
# Section 7: GDX/GLD Status
# =========================================================================

def _section_gdx_gld() -> str:
    lines = ["## 7. GDX/GLD Pairs Status\n"]

    # Read current z-score from heartbeat
    hb_path = FORGE / "logs" / "gdx_gld" / "heartbeat.json"
    if hb_path.exists():
        try:
            hb = json.loads(hb_path.read_text(encoding="utf-8"))
            lines.append(f"- Z-score: {hb.get('z_score', 'N/A')}")
            lines.append(f"- Position: {hb.get('position', 'flat')}")
            lines.append(f"- Last update: {hb.get('ts', 'N/A')}")
        except Exception:
            lines.append("- Heartbeat unreadable")
    else:
        lines.append("- No heartbeat file")

    # Recent trades
    trades_path = FORGE / "logs" / "gdx_gld" / "trades.csv"
    if trades_path.exists():
        week_ago = date.today() - timedelta(days=7)
        recent = _read_trades_for_period(trades_path, week_ago, date.today())
        lines.append(f"- Trades this week: {len(recent)}")
    else:
        lines.append("- No trades file")

    lines.append("")
    return "\n".join(lines)


# =========================================================================
# Section 8: Position Aging
# =========================================================================

def _section_aging() -> str:
    lines = ["## 8. Position Aging\n"]
    try:
        from forge.position_aging import scan_aging_positions
        positions = scan_aging_positions()

        if not positions:
            lines.append("- No open positions")
        else:
            for pos in positions:
                pnl_str = (
                    f"{pos['unrealized_pnl_pct']:+.1f}%"
                    if pos.get("unrealized_pnl_pct") is not None
                    else "N/A"
                )
                lines.append(
                    f"- [{pos['status']}] **{pos['system']}** {pos['ticker']} "
                    f"{pos['direction']} -- {pos['days_held']}d, {pnl_str}"
                )
    except Exception as e:
        lines.append(f"- Position aging scan failed: {e}")
    lines.append("")
    return "\n".join(lines)


# =========================================================================
# Section 9: Drawdown Status
# =========================================================================

def _section_drawdown() -> str:
    lines = ["## 9. Drawdown Status\n"]
    try:
        from forge.drawdown_recovery import get_drawdown_status
        s = get_drawdown_status()

        lines.append(f"- Level: **{s.get('level', 'unknown')}**")
        lines.append(f"- Drawdown: {s.get('drawdown_pct', 0):.2f}%")
        lines.append(f"- Recovery multiplier: {s.get('recovery_multiplier', 1.0):.2f}x")
        peak = s.get("peak_equity")
        current = s.get("current_equity")
        if peak:
            lines.append(f"- Peak equity: ${peak:,.2f}")
        if current:
            lines.append(f"- Current equity: ${current:,.2f}")
    except Exception as e:
        lines.append(f"- Drawdown data unavailable: {e}")
    lines.append("")
    return "\n".join(lines)


# =========================================================================
# Section 10: Upcoming Events
# =========================================================================

def _section_upcoming_events() -> str:
    lines = ["## 10. Upcoming Events (next 7 days)\n"]
    try:
        import sqlite3
        db_path = FORGE_DATA / "atlas.db"
        if not db_path.exists():
            lines.append("- Atlas DB not found")
            return "\n".join(lines) + "\n"

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        today = date.today()
        next_week = today + timedelta(days=7)

        rows = conn.execute(
            "SELECT event_date, event_type, description, importance "
            "FROM scheduled_events "
            "WHERE event_date BETWEEN ? AND ? "
            "ORDER BY event_date",
            (today.isoformat(), next_week.isoformat()),
        ).fetchall()
        conn.close()

        if not rows:
            lines.append("- No major scheduled events")
        else:
            for row in rows:
                imp = row["importance"].upper() if row["importance"] else ""
                lines.append(
                    f"- {row['event_date']}: [{imp}] {row['event_type']} -- "
                    f"{row['description']}"
                )
    except Exception as e:
        lines.append(f"- Event calendar unavailable: {e}")
    lines.append("")
    return "\n".join(lines)


# =========================================================================
# Section 11: Conviction Score Snapshot
# =========================================================================

def _section_conviction_snapshot() -> str:
    lines = ["## 11. Conviction Score Snapshot\n"]

    # Top watchlist names from Titan's data directory
    watchlist = []
    titan_data = REPO / "titan" / "data"
    if titan_data.exists():
        seen = set()
        for f in sorted(titan_data.glob("*_daily.csv")):
            ticker = f.stem.replace("_daily", "")
            if ticker not in seen:
                seen.add(ticker)
                watchlist.append(ticker)

    if not watchlist:
        watchlist = ["MSFT", "AAPL", "NVDA", "AMD", "TSLA"]

    # Take top 5
    watchlist = watchlist[:5]

    try:
        from forge.conviction import score_conviction

        for ticker in watchlist:
            try:
                result = score_conviction(
                    system="titan", ticker=ticker, direction="LONG"
                )
                mult = result["size_multiplier"]
                conv = result["conviction"]
                score = result["total_score"]
                lines.append(
                    f"- **{ticker}** LONG: {conv} ({mult}x), "
                    f"total={score:.2f}"
                )
            except Exception as e:
                lines.append(f"- **{ticker}**: error ({e})")
    except ImportError:
        lines.append("- Conviction scorer unavailable")
    lines.append("")
    return "\n".join(lines)


# =========================================================================
# Section 12: Recommendations
# =========================================================================

def _section_recommendations(days: int) -> str:
    lines = ["## 12. Recommendations\n"]

    end = date.today()
    start = end - timedelta(days=days)

    # Gather per-system trade counts and P&L
    system_stats = {}
    for sys_name, base_path in SYSTEM_TRADE_PATHS.items():
        if not base_path.exists():
            continue
        csvs = []
        if sys_name == "argus":
            csvs = sorted(base_path.glob("*/trades.csv"))
        elif sys_name == "apollo":
            csvs = sorted(base_path.glob("apollo_*/trades.csv"))
        else:
            tc = base_path / "trades.csv"
            if tc.exists():
                csvs = [tc]

        trades = []
        for cp in csvs:
            trades.extend(_read_trades_for_period(cp, start, end))

        if trades:
            wins = sum(1 for t in trades if (t["pnl_pct"] or 0) > 0)
            wr = wins / len(trades)
            pnl = sum(t["pnl_pct"] for t in trades if t["pnl_pct"] is not None)
            system_stats[sys_name] = {
                "trades": len(trades),
                "win_rate": wr,
                "pnl_pct": pnl,
            }

    if not system_stats:
        lines.append("- No trade data to base recommendations on")
        lines.append("- **All systems**: KEEP -- continue burn-in, collect more data")
        lines.append("")
        return "\n".join(lines)

    for sys_name, stats in system_stats.items():
        trades = stats["trades"]
        wr = stats["win_rate"]
        pnl = stats["pnl_pct"]

        if trades < 5:
            verdict = "KEEP -- insufficient data, continue monitoring"
        elif wr < 0.35 and pnl < -2.0:
            verdict = "REVIEW -- low win rate and negative P&L, investigate"
        elif wr < 0.40:
            verdict = "TUNE -- win rate below target, check signal quality"
        elif pnl < 0:
            verdict = "KEEP -- winning trades but net negative, watch sizing"
        elif wr >= 0.55 and pnl > 0:
            verdict = "KEEP -- performing well"
        else:
            verdict = "KEEP -- adequate performance, continue burn-in"

        lines.append(
            f"- **{sys_name}**: {verdict} "
            f"({trades} trades, {wr:.0%} WR, {pnl:+.2f}%)"
        )

    lines.append("")
    return "\n".join(lines)


# =========================================================================
# Main generator
# =========================================================================

def generate_weekly_review(days: int = 7) -> str:
    """
    Generate the full Friday review report.

    Returns formatted markdown string covering:
    1. Fleet Health
    2. Trade Summary
    3. Benchmark
    4. Slippage
    5. Atlas Intelligence
    6. Themis Signals
    7. GDX/GLD Status
    8. Position Aging
    9. Drawdown Status
    10. Upcoming Events
    11. Conviction Score Snapshot
    12. Recommendations
    """
    now = datetime.now(timezone.utc)
    header = (
        f"# Helio Fleet Weekly Review\n"
        f"**Generated**: {now.strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"**Period**: last {days} days\n\n"
        f"---\n\n"
    )

    sections = [
        header,
        _section_fleet_health(),
        _section_trade_summary(days),
        _section_benchmark(),
        _section_slippage(),
        _section_atlas(),
        _section_themis(),
        _section_gdx_gld(),
        _section_aging(),
        _section_drawdown(),
        _section_upcoming_events(),
        _section_conviction_snapshot(),
        _section_recommendations(days),
    ]

    report = "\n".join(sections)
    report += "\n---\n*Auto-generated by forge.auto_review*\n"
    return report


# =========================================================================
# Discord delivery
# =========================================================================

def _send_discord(report: str) -> None:
    """Send report to Discord, splitting at 2000 char boundary."""
    import requests

    webhook = DISCORD_WEBHOOK_URL
    if not webhook:
        print("ERROR: DISCORD_WEBHOOK_URL not set in environment")
        return

    # Split into chunks respecting line breaks
    chunks = []
    current = ""
    for line in report.split("\n"):
        if len(current) + len(line) + 1 > 1900:
            chunks.append(current)
            current = line
        else:
            current += "\n" + line if current else line
    if current:
        chunks.append(current)

    for i, chunk in enumerate(chunks):
        payload = {"content": chunk}
        try:
            resp = requests.post(webhook, json=payload, timeout=10)
            resp.raise_for_status()
            print(f"  Sent chunk {i + 1}/{len(chunks)} ({len(chunk)} chars)")
        except Exception as e:
            print(f"  ERROR sending chunk {i + 1}: {e}")


# =========================================================================
# CLI
# =========================================================================

def main():
    parser = argparse.ArgumentParser(description="Automated Friday Review Report")
    parser.add_argument(
        "--generate", action="store_true",
        help="Generate full report, print to stdout",
    )
    parser.add_argument(
        "--discord", action="store_true",
        help="Generate and send to Discord webhook",
    )
    parser.add_argument(
        "--save", action="store_true",
        help="Save to forge/data/reviews/review_YYYY-MM-DD.md",
    )
    parser.add_argument(
        "--days", type=int, default=7,
        help="Review period in days (default: 7)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )

    if not any([args.generate, args.discord, args.save]):
        args.generate = True

    report = generate_weekly_review(days=args.days)

    if args.generate:
        print(report)

    if args.save:
        REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"review_{date.today().isoformat()}.md"
        out_path = REVIEWS_DIR / filename
        out_path.write_text(report, encoding="utf-8")
        print(f"\nSaved to {out_path}")

    if args.discord:
        _send_discord(report)


if __name__ == "__main__":
    main()
