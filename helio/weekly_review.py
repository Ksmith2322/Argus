"""helio/weekly_review.py -- Weekly fleet performance review.

Aggregates 7 days of trades, snapshots, and signals across all systems.
Posts a comprehensive Discord report. Run every Friday or on demand.

Usage:
    python -m helio.weekly_review
    python -m helio.weekly_review --days 7
    python -m helio.weekly_review --dry-run
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv
load_dotenv(REPO / ".env")

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
SNAPSHOT_DIR = REPO / "argus_flow" / "logs" / "fleet_snapshots"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("weekly_review")


def load_argus_trades(days: int) -> list[dict]:
    """Load Argus FX trades from the last N days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    all_trades = []
    for sym in ["audjpy", "usdjpy", "gbpusd", "cadjpy"]:
        path = REPO / "argus_flow" / "logs" / sym / "trades.csv"
        if not path.exists():
            continue
        try:
            with open(path) as f:
                for row in csv.DictReader(f):
                    if row.get("experiment_valid", "").lower() != "true":
                        continue
                    try:
                        ts = datetime.fromisoformat(row.get("ts", "").replace("Z", "+00:00"))
                        if ts >= cutoff:
                            row["_symbol"] = sym.upper()
                            row["_pnl"] = float(row.get("pnl_pips", 0))
                            row["_unit"] = "pips"
                            all_trades.append(row)
                    except Exception:
                        continue
        except Exception:
            continue
    return all_trades


def load_system_trades(system: str, days: int) -> list[dict]:
    """Load trades from titan/hermes/apollo/ares."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    path = REPO / system / "logs" / "trades.csv"
    if not path.exists():
        return []

    trades = []
    try:
        with open(path) as f:
            for row in csv.DictReader(f):
                # Use entry_date or ts if available
                date_field = row.get("exit_date") or row.get("entry_date") or row.get("ts", "")
                try:
                    if "T" in date_field:
                        ts = datetime.fromisoformat(date_field.replace("Z", "+00:00"))
                    else:
                        ts = datetime.strptime(date_field[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    if ts < cutoff:
                        continue
                except Exception:
                    continue

                row["_pnl"] = float(row.get("pnl_pct", 0))
                row["_unit"] = "%"
                row["_system"] = system
                trades.append(row)
    except Exception:
        pass
    return trades


def system_stats(trades: list[dict]) -> dict:
    """Compute summary stats for a list of trades."""
    if not trades:
        return {"trades": 0, "wins": 0, "losses": 0, "wr": 0, "pf": 0, "pnl": 0}

    pnls = [t["_pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "wr": round(len(wins) / len(pnls) * 100, 1),
        "pf": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else 999,
        "pnl": round(sum(pnls), 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
    }


def get_snapshot_progression(days: int) -> list[dict]:
    """Read snapshots from the last N days to track progression."""
    if not SNAPSHOT_DIR.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    snaps = []
    for f in sorted(SNAPSHOT_DIR.glob("snapshot_*.json")):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mtime >= cutoff:
                snaps.append(json.loads(f.read_text()))
        except Exception:
            continue
    return snaps


def generate_report(days: int = 7) -> str:
    """Generate the full weekly review report."""
    now = datetime.now(timezone.utc)
    period_start = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    period_end = now.strftime("%Y-%m-%d")

    lines = [f"**HELIO FLEET WEEKLY REVIEW**"]
    lines.append(f"_{period_start} to {period_end} ({days} days)_")
    lines.append("")

    # ── Per-system performance ──────────────────────────────
    lines.append("**SYSTEM PERFORMANCE**")
    lines.append("")

    # Argus
    argus = load_argus_trades(days)
    a_stats = system_stats(argus)
    if argus:
        # Per-pair breakdown
        by_pair = defaultdict(list)
        for t in argus:
            by_pair[t["_symbol"]].append(t)
        pair_lines = []
        for pair, ts in by_pair.items():
            ps = system_stats(ts)
            pair_lines.append(f"  {pair}: {ps['trades']}t {ps['wr']}%WR {ps['pnl']:+.1f}p")
        lines.append(f"**Argus** (FX): {a_stats['trades']} trades | "
                     f"WR {a_stats['wr']}% | PF {a_stats['pf']} | "
                     f"PnL {a_stats['pnl']:+.1f} pips")
        lines.extend(pair_lines)
    else:
        lines.append(f"**Argus** (FX): no trades this period")
    lines.append("")

    # Titan
    titan = load_system_trades("titan", days)
    t_stats = system_stats(titan)
    if titan:
        lines.append(f"**Titan** (Stock Swing): {t_stats['trades']}t | WR {t_stats['wr']}% | "
                     f"PF {t_stats['pf']} | PnL {t_stats['pnl']:+.1f}%")
    else:
        lines.append(f"**Titan** (Stock Swing): no trades")
    lines.append("")

    # Hermes
    hermes = load_system_trades("hermes", days)
    h_stats = system_stats(hermes)
    if hermes:
        lines.append(f"**Hermes** (Gap Fill): {h_stats['trades']}t | WR {h_stats['wr']}% | "
                     f"PF {h_stats['pf']} | PnL {h_stats['pnl']:+.1f}%")
    else:
        lines.append(f"**Hermes** (Gap Fill): no trades")
    lines.append("")

    # Apollo
    apollo = load_system_trades("apollo", days)
    ap_stats = system_stats(apollo)
    if apollo:
        lines.append(f"**Apollo** (Earnings): {ap_stats['trades']}t | WR {ap_stats['wr']}% | "
                     f"PF {ap_stats['pf']} | PnL {ap_stats['pnl']:+.1f}%")
    else:
        lines.append(f"**Apollo** (Earnings): no trades")
    lines.append("")

    # Ares
    ares = load_system_trades("ares", days)
    if ares:
        ar_stats = system_stats(ares)
        lines.append(f"**Ares** (Sector Rotation): {ar_stats['trades']}t | PnL {ar_stats['pnl']:+.1f}%")
    else:
        lines.append(f"**Ares** (Sector Rotation): no rebalances (monthly cadence)")
    lines.append("")

    # ── Fleet totals ────────────────────────────────────────
    total_trades = a_stats['trades'] + t_stats['trades'] + h_stats['trades'] + ap_stats['trades']
    lines.append("**FLEET TOTALS**")
    lines.append(f"  Total trades: {total_trades}")
    if total_trades > 0:
        all_wins = a_stats['wins'] + t_stats['wins'] + h_stats['wins'] + ap_stats['wins']
        lines.append(f"  Total wins: {all_wins} ({all_wins/total_trades*100:.0f}%)")
    lines.append("")

    # ── Snapshot progression ────────────────────────────────
    snaps = get_snapshot_progression(days)
    if len(snaps) >= 2:
        first = snaps[0]
        last = snaps[-1]
        lines.append(f"**WEEK PROGRESSION** ({len(snaps)} snapshots)")
        for sys_name in ["argus", "titan", "hermes", "apollo", "ares"]:
            f_data = first["systems"].get(sys_name, {})
            l_data = last["systems"].get(sys_name, {})
            f_trades = f_data.get("trades", 0)
            l_trades = l_data.get("trades", 0)
            f_pnl = f_data.get("total_pnl", f_data.get("pnl", 0))
            l_pnl = l_data.get("total_pnl", l_data.get("pnl", 0))
            delta_t = l_trades - f_trades
            delta_p = round(l_pnl - f_pnl, 2)
            if delta_t > 0 or abs(delta_p) > 0.01:
                lines.append(f"  {sys_name.title()}: +{delta_t} trades, {delta_p:+.1f} PnL")
        lines.append("")

    # ── Action items ────────────────────────────────────────
    lines.append("**ACTION ITEMS**")
    actions = []

    if a_stats['trades'] == 0 and len(snaps) >= 2:
        actions.append("- Argus: 0 trades this week — check signal generation + gates")
    if t_stats['trades'] == 0 and len(snaps) >= 5:
        actions.append("- Titan: 0 trades — verify scanner is finding setups")

    # Bad performers
    for name, stats in [("Argus", a_stats), ("Titan", t_stats), ("Hermes", h_stats), ("Apollo", ap_stats)]:
        if stats['trades'] >= 5 and stats['pf'] < 0.8:
            actions.append(f"- {name}: PF {stats['pf']} after {stats['trades']} trades — investigate or kill")

    if not actions:
        actions.append("- All systems healthy. Continue monitoring.")
    lines.extend(actions)
    lines.append("")

    lines.append("_Next review: " + (now + timedelta(days=days)).strftime("%Y-%m-%d") + "_")

    return "\n".join(lines)


def send_discord(content: str) -> bool:
    if not WEBHOOK_URL:
        return False
    try:
        import requests
        # Discord max 2000 chars per message — split if needed
        chunks = [content[i:i+1900] for i in range(0, len(content), 1900)]
        ok = True
        for chunk in chunks:
            r = requests.post(WEBHOOK_URL, json={"content": chunk}, timeout=10)
            ok = ok and r.status_code in (200, 204)
        return ok
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="Weekly Fleet Review")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    report = generate_report(days=args.days)
    print(report)
    print()

    if not args.dry_run:
        ok = send_discord(report)
        print(f"Discord: {'sent' if ok else 'FAILED'}")

    # Save to file
    out = REPO / "argus_flow" / "logs" / f"weekly_review_{datetime.now().strftime('%Y%m%d')}.md"
    out.write_text(report, encoding="utf-8")
    print(f"Saved: {out}")


if __name__ == "__main__":
    main()
