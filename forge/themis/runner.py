"""Themis — Congressional trading tracker runner."""

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from forge.themis.db import DB_PATH, get_connection, get_signal_scorecard, init_db
from forge.themis.fetcher import fetch_and_store
from forge.themis.signals import (
    detect_signals,
    format_signal_alert,
    get_top_signals,
    score_signals,
)

HEARTBEAT_DIR = Path(r"C:\Argus\repo\forge\logs\themis")
HEARTBEAT_PATH = HEARTBEAT_DIR / "heartbeat.json"


def _write_heartbeat(
    total_trades: int,
    total_signals: int,
    active_signals: int,
    last_fetch: str,
    new_signals: int,
    mode: str = "monitor",
) -> None:
    HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
    hb = {
        "system": "themis",
        "family": "forge",
        "ts": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "mode": mode,
        "total_trades": total_trades,
        "total_signals": total_signals,
        "active_signals": active_signals,
        "last_fetch": last_fetch,
        "new_signals_this_cycle": new_signals,
    }
    HEARTBEAT_PATH.write_text(json.dumps(hb, indent=2))


def _send_discord_alert(message: str) -> None:
    webhook = os.environ.get("DISCORD_WEBHOOK_THEMIS") or os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        return
    try:
        import requests
        requests.post(webhook, json={"content": message}, timeout=10)
    except Exception as e:
        print(f"[themis] Discord alert failed: {e}")


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _count_active(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM signals WHERE status = 'active'").fetchone()[0]


def run_fetch(conn) -> dict:
    print("[themis] Fetching trades...")
    result = fetch_and_store(conn)
    print(f"[themis] Fetch: {result['fetched']} fetched, {result['new']} new, {result['duplicates']} duplicates")
    return result


def run_scan(conn) -> list[dict]:
    print("[themis] Scanning for signals...")
    new_signals = detect_signals(conn)
    print(f"[themis] Found {len(new_signals)} new signals")
    for sig in new_signals:
        alert = format_signal_alert(sig)
        print(f"  {alert.encode('ascii', 'replace').decode('ascii')}")
        _send_discord_alert(alert)
    return new_signals


def run_score(conn) -> dict:
    print("[themis] Scoring mature signals...")
    result = score_signals(conn)
    print(f"[themis] Scored: {result['scored']}, errors: {result['errors']}")
    return result


def run_status(conn) -> None:
    total_trades = _count(conn, "trades")
    total_signals = _count(conn, "signals")
    active = _count_active(conn)

    print(f"\n{'='*60}")
    print(f"  THEMIS — Congressional Trading Tracker")
    print(f"{'='*60}")
    print(f"  Total trades in DB:  {total_trades}")
    print(f"  Total signals:       {total_signals}")
    print(f"  Active signals:      {active}")
    print()

    top = get_top_signals(conn, n=10)
    if top:
        print(f"  Recent Signals ({len(top)}):")
        print(f"  {'-'*50}")
        for s in top:
            members = s.get("members", "[]")
            if isinstance(members, str):
                try:
                    members = json.loads(members)
                except Exception:
                    members = [members]
            alpha_str = ""
            if s.get("alpha_30d") is not None:
                alpha_str = f" | alpha30={s['alpha_30d']:+.1%}"
            print(f"  [{s['signal_type']:20s}] {s['ticker']:6s} | {s.get('member_count', '?')} members{alpha_str}")
            print(f"    {s.get('description', '')}")
    else:
        print("  No signals detected yet.")

    print()


def run_scorecard(conn) -> None:
    sc = get_signal_scorecard(conn)
    print(f"\n{'='*60}")
    print(f"  THEMIS — Signal Scorecard")
    print(f"{'='*60}")
    print(f"  Total signals:  {sc['total_signals']}")
    print(f"  Scored 30d:     {sc['scored_30d']}")
    print(f"  Scored 60d:     {sc['scored_60d']}")
    print(f"  Scored 90d:     {sc['scored_90d']}")

    if sc["avg_alpha_30d"] is not None:
        print(f"  Avg alpha 30d:  {sc['avg_alpha_30d']:+.2%}")
    if sc["avg_alpha_60d"] is not None:
        print(f"  Avg alpha 60d:  {sc['avg_alpha_60d']:+.2%}")
    if sc["avg_alpha_90d"] is not None:
        print(f"  Avg alpha 90d:  {sc['avg_alpha_90d']:+.2%}")

    if sc["by_type"]:
        print(f"\n  By signal type:")
        print(f"  {'-'*50}")
        for stype, stats in sc["by_type"].items():
            a30 = f"{stats['avg_alpha_30d']:+.2%}" if stats["avg_alpha_30d"] is not None else "n/a"
            print(f"    {stype:22s} count={stats['count']:3d}  scored30={stats['scored_30d']:3d}  alpha30={a30}")
    print()


def run_loop(conn, interval_min: int = 360) -> None:
    print(f"[themis] Loop mode — interval {interval_min} min")
    while True:
        try:
            fetch_result = run_fetch(conn)
            new_signals = run_scan(conn)
            score_result = run_score(conn)

            _write_heartbeat(
                total_trades=_count(conn, "trades"),
                total_signals=_count(conn, "signals"),
                active_signals=_count_active(conn),
                last_fetch=datetime.now(timezone.utc).isoformat(),
                new_signals=len(new_signals),
            )

            print(f"[themis] Cycle complete — sleeping {interval_min} min")
            time.sleep(interval_min * 60)
        except KeyboardInterrupt:
            print("[themis] Interrupted — exiting")
            break
        except Exception as e:
            print(f"[themis] Error in loop: {e}")
            time.sleep(60)


def main():
    parser = argparse.ArgumentParser(description="Themis — Congressional Trading Tracker")
    parser.add_argument("--fetch", action="store_true", help="Fetch latest trades")
    parser.add_argument("--scan", action="store_true", help="Detect new signals")
    parser.add_argument("--score", action="store_true", help="Score mature signals")
    parser.add_argument("--status", action="store_true", help="Show status")
    parser.add_argument("--scorecard", action="store_true", help="Show signal scorecard")
    parser.add_argument("--loop", action="store_true", help="Run continuous loop")
    parser.add_argument("--interval-min", type=int, default=360, help="Loop interval in minutes")
    args = parser.parse_args()

    init_db()
    conn = get_connection()

    try:
        if args.fetch:
            run_fetch(conn)
        if args.scan:
            run_scan(conn)
        if args.score:
            run_score(conn)
        if args.status:
            run_status(conn)
        if args.scorecard:
            run_scorecard(conn)
        if args.loop:
            run_loop(conn, args.interval_min)

        # Default: if nothing specified, show help
        if not any([args.fetch, args.scan, args.score, args.status, args.scorecard, args.loop]):
            parser.print_help()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
