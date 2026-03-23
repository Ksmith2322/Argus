"""Discord Alerts for IBKR Fleet — entry/exit notifications, daily summary, errors.

Usage:
    python -m argus_flow.ops.discord_alerts --test     # send test message
    python -m argus_flow.ops.discord_alerts --watch    # watch for new trades and alert
    python -m argus_flow.ops.discord_alerts --summary  # send daily summary
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

REPO = Path(__file__).resolve().parents[2]
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

RUNNERS = [
    {"name": "EUR/USD", "log_dir": "argus_flow/logs/eurusd", "unit": "pips"},
    {"name": "MNQ", "log_dir": "argus_flow/logs/mnq", "unit": "pts"},
    {"name": "GBP/USD", "log_dir": "argus_flow/logs/gbpusd", "unit": "pips"},
]


def send_discord(content: str = "", embeds: list | None = None) -> bool:
    if not WEBHOOK_URL:
        print("No DISCORD_WEBHOOK_URL set in .env")
        return False
    payload = {}
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        return r.status_code in (200, 204)
    except Exception as e:
        print(f"Discord error: {e}")
        return False


def send_trade_alert(runner_name: str, trade: dict, unit: str):
    pnl_field = "pnl_pips" if "pnl_pips" in trade else "pnl_pts"
    pnl = float(trade.get(pnl_field, 0))
    direction = trade.get("direction", "?").upper()
    reason = trade.get("exit_reason", "?")
    entry = trade.get("entry_px", "?")
    exit_px = trade.get("exit_px", "?")
    dur = trade.get("duration_min", "?")

    color = 0x00FF88 if pnl >= 0 else 0xFF4444
    emoji = "+" if pnl >= 0 else ""

    embed = {
        "title": f"{'WIN' if pnl >= 0 else 'LOSS'} — {runner_name} {direction}",
        "color": color,
        "fields": [
            {"name": "Entry", "value": str(entry), "inline": True},
            {"name": "Exit", "value": str(exit_px), "inline": True},
            {"name": "PnL", "value": f"{emoji}{pnl:.1f} {unit}", "inline": True},
            {"name": "Reason", "value": reason, "inline": True},
            {"name": "Duration", "value": f"{dur}m" if dur != "?" else "?", "inline": True},
        ],
        "timestamp": trade.get("ts", datetime.now(timezone.utc).isoformat()),
    }
    send_discord(embeds=[embed])


def send_daily_summary():
    from argus_flow.ops.daily_report import generate_report
    report = generate_report()

    lines = [f"**IBKR Fleet Daily Report — {report['date']}**\n"]
    for r in report["runners"]:
        pnl = r.get("total_pnl", 0)
        emoji = "+" if pnl >= 0 else ""
        lines.append(f"**{r['name']}**: {r['trades_total']} trades | WR {r.get('win_rate', 0):.0%} | PnL {emoji}{pnl:.1f} {r['unit']}")

    total_pnl = sum(r.get("total_pnl", 0) for r in report["runners"])
    lines.append(f"\n**Fleet PnL**: {'+'if total_pnl>=0 else ''}{total_pnl:.1f}")
    send_discord(content="\n".join(lines))


def send_test():
    ok = send_discord(content="**Argus IBKR Fleet** — Discord alerts connected! Monitoring EUR/USD, MNQ, GBP/USD.")
    print("Test message sent!" if ok else "Failed to send test message.")


def watch_trades():
    """Poll trade files for new trades and send alerts."""
    print("Watching for new trades... (Ctrl+C to stop)")

    # Track last known trade count per runner
    last_counts = {}
    for runner in RUNNERS:
        trade_file = REPO / runner["log_dir"] / "trades.csv"
        if trade_file.exists():
            with open(trade_file) as f:
                last_counts[runner["name"]] = sum(1 for _ in f) - 1
        else:
            last_counts[runner["name"]] = 0

    while True:
        for runner in RUNNERS:
            trade_file = REPO / runner["log_dir"] / "trades.csv"
            if not trade_file.exists():
                continue

            with open(trade_file) as f:
                rows = list(csv.DictReader(f))

            current_count = len(rows)
            prev_count = last_counts.get(runner["name"], 0)

            if current_count > prev_count:
                # New trades!
                for trade in rows[prev_count:]:
                    print(f"  New trade: {runner['name']} {trade.get('direction', '?')} PnL={trade.get('pnl_pips', trade.get('pnl_pts', '?'))}")
                    send_trade_alert(runner["name"], trade, runner["unit"])
                last_counts[runner["name"]] = current_count

        time.sleep(30)


def main():
    parser = argparse.ArgumentParser(description="Discord Alerts for IBKR Fleet")
    parser.add_argument("--test", action="store_true", help="Send test message")
    parser.add_argument("--watch", action="store_true", help="Watch for new trades")
    parser.add_argument("--summary", action="store_true", help="Send daily summary")
    args = parser.parse_args()

    if args.test:
        send_test()
    elif args.watch:
        watch_trades()
    elif args.summary:
        send_daily_summary()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()