"""Quick fleet status check — are we trading or blocked?

Usage:
    python -m argus_flow.ops.status
"""
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

REPO = Path(__file__).resolve().parents[2]
LOGS = REPO / "argus_flow" / "logs"


def main():
    now = datetime.now(timezone.utc)
    print(f"\n{'='*65}")
    print(f"  ARGUS STATUS — {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"{'='*65}\n")

    # Check heartbeats
    alive = 0
    dead = 0
    blocked = 0
    positions = []

    pairs = sorted(d.name for d in LOGS.iterdir() if d.is_dir() and (d / "heartbeat.json").exists())

    print(f"  {'Pair':<10} {'Status':<12} {'Position':<8} {'Trades':<7} {'Blocked':<10} {'Last Bar'}")
    print(f"  {'-'*70}")

    for pair in pairs:
        hb_file = LOGS / pair / "heartbeat.json"
        sig_file = LOGS / pair / "signals.csv"
        trade_file = LOGS / pair / "trades.csv"

        # Heartbeat
        try:
            hb = json.loads(hb_file.read_text())
            hb_age = (now - datetime.fromisoformat(hb["ts"])).total_seconds()
            connected = hb.get("broker_connected", False)
            mode = hb.get("runtime_mode", "?")
            pos = hb.get("position", "FLAT")
            quarantined = hb.get("quarantined", False)
            last_bar = hb.get("last_bar_ts", "?")
            if last_bar and len(str(last_bar)) > 16:
                last_bar = str(last_bar)[:16]
        except Exception:
            hb_age = 9999
            connected = False
            mode = "?"
            pos = "?"
            quarantined = False
            last_bar = "?"

        # Status
        if hb_age > 600:
            status = "\033[31mDEAD\033[0m"
            dead += 1
        elif not connected:
            status = "\033[31mDISCONN\033[0m"
            dead += 1
        elif quarantined:
            status = "\033[33mQUARANTINE\033[0m"
            blocked += 1
        else:
            status = "\033[32mALIVE\033[0m"
            alive += 1

        if pos != "FLAT":
            positions.append(f"{pair.upper()}={pos}")

        # Trade count
        trade_count = 0
        if trade_file.exists():
            try:
                with open(trade_file) as f:
                    trade_count = sum(1 for _ in f) - 1
            except Exception:
                pass

        # Blocked count
        block_count = 0
        if sig_file.exists():
            try:
                with open(sig_file) as f:
                    for row in csv.DictReader(f):
                        if "BLOCKED" in row.get("action", ""):
                            block_count += 1
            except Exception:
                pass

        block_str = f"\033[31m{block_count}\033[0m" if block_count > 0 else "0"

        print(f"  {pair.upper():<10} {status:<20} {pos:<8} {trade_count:<7} {block_str:<18} {last_bar}")

    # Summary
    print(f"\n  Alive: {alive} | Dead: {dead} | Blocked: {blocked}")
    if positions:
        print(f"  Open positions: {', '.join(positions)}")
    else:
        print(f"  Open positions: none (all FLAT)")

    # Check degradation control
    control_file = LOGS / "degradation_control.json"
    if control_file.exists():
        try:
            ctrl = json.loads(control_file.read_text())
            paused = [sym for sym, v in ctrl.items() if v.get("status") == "HARD_PAUSE"]
            warned = [sym for sym, v in ctrl.items() if v.get("status") == "WARN"]
            if paused:
                print(f"\n  \033[31mHARD PAUSED: {', '.join(paused)}\033[0m")
            if warned:
                print(f"  \033[33mWARNING: {', '.join(warned)}\033[0m")
            if not paused and not warned:
                print(f"\n  Health: \033[32mALL OK\033[0m")
        except Exception:
            pass

    # Total fleet PnL
    total_valid = 0
    total_pnl = 0
    for pair in pairs:
        tf = LOGS / pair / "trades.csv"
        if not tf.exists():
            continue
        try:
            with open(tf) as f:
                for row in csv.DictReader(f):
                    if row.get("experiment_valid", "").lower() == "true":
                        total_valid += 1
                        pnl_field = "pnl_pips" if "pnl_pips" in row else "pnl_pts"
                        total_pnl += float(row.get(pnl_field, 0))
        except Exception:
            pass

    print(f"\n  Valid trades: {total_valid} / 30 target")
    print(f"  Fleet PnL: {total_pnl:+.1f} pips")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
