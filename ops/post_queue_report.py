#!/usr/bin/env python3
"""ops/post_queue_report.py -- Generate leaderboard from recent runs and send to Discord.

Usage:
    python ops/post_queue_report.py              # leaderboard of all runs with labels
    python ops/post_queue_report.py --min-trades 10  # filter low-trade runs
    python ops/post_queue_report.py --no-discord  # print only, don't send to Discord
"""
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOGS = REPO / "ops" / "logs"

PHASE_165_EXIT = {"win_rate_pct": 35.0, "profit_factor": 1.20}


def load_labeled_runs(min_trades: int = 0) -> list[dict]:
    """Load all bt_summary files that have a job label, sorted by PF descending."""
    rows = []
    for p in sorted(LOGS.glob("bt_summary_bt_*.json")):
        if "latest" in p.name:
            continue
        try:
            with open(p) as f:
                d = json.load(f)
            # Get label from run_header
            rh_path = LOGS / f"run_header_{d.get('run_id', '')}.json"
            label = ""
            if rh_path.exists():
                with open(rh_path) as f:
                    hdr = json.load(f)
                label = hdr.get("label", "")
            d["_label"] = label
            trades = int(d.get("trades_closed", 0) or 0)
            if min_trades > 0 and trades < min_trades:
                continue
            rows.append(d)
        except Exception:
            continue
    rows.sort(key=lambda r: float(r.get("profit_factor", "0") or "0"), reverse=True)
    return rows


def format_leaderboard(rows: list[dict]) -> str:
    if not rows:
        return "No completed runs found."

    lines = []
    lines.append("BACKTEST LEADERBOARD")
    lines.append("=" * 40)

    for i, r in enumerate(rows):
        rid = r.get("run_id", "?")[-16:]
        label = r.get("_label", "") or "unlabeled"
        trades = r.get("trades_closed", 0)
        wr = r.get("win_rate_pct", "0")
        pf = r.get("profit_factor", "0")
        pnl = r.get("pnl_usd", "0")
        dd = r.get("max_drawdown_pct", "0")

        medal = ""
        if i == 0:
            medal = " [BEST]"

        lines.append(f"#{i+1}{medal} {label}")
        lines.append(f"   {rid}  Trades={trades}  WR={wr}%  PF={pf}  PnL=${pnl}  DD={dd}%")

    # Check exit criteria on best run
    lines.append("")
    best = rows[0]
    wr_val = float(best.get("win_rate_pct", "0") or "0")
    pf_val = float(best.get("profit_factor", "0") or "0")
    if wr_val >= PHASE_165_EXIT["win_rate_pct"] and pf_val >= PHASE_165_EXIT["profit_factor"]:
        lines.append(f"EXIT CRITERIA MET: WR={wr_val}% >= {PHASE_165_EXIT['win_rate_pct']}%, PF={pf_val} >= {PHASE_165_EXIT['profit_factor']}")
        lines.append("Phase 16.5 PASS — lock this config and proceed to Phase 17.")
    else:
        gaps = []
        if wr_val < PHASE_165_EXIT["win_rate_pct"]:
            gaps.append(f"WR={wr_val}% (need {PHASE_165_EXIT['win_rate_pct']}%)")
        if pf_val < PHASE_165_EXIT["profit_factor"]:
            gaps.append(f"PF={pf_val} (need {PHASE_165_EXIT['profit_factor']})")
        lines.append(f"EXIT CRITERIA NOT MET: {', '.join(gaps)}")

    return "\n".join(lines)


def main():
    min_trades = 0
    no_discord = "--no-discord" in sys.argv
    for i, a in enumerate(sys.argv):
        if a == "--min-trades" and i + 1 < len(sys.argv):
            min_trades = int(sys.argv[i + 1])

    rows = load_labeled_runs(min_trades)
    report = format_leaderboard(rows)
    print(report)

    if not no_discord:
        try:
            import sys as _sys
            from pathlib import Path as _Path
            _repo = str(_Path(__file__).resolve().parent.parent)
            if _repo not in _sys.path:
                _sys.path.insert(0, _repo)
            from ops.notify import send_discord
            # Discord has 2000 char limit; truncate if needed
            msg = report
            if len(msg) > 1900:
                msg = msg[:1900] + "\n... (truncated)"
            send_discord(f"```\n{msg}\n```")
            print("\nSent to Discord.")
        except Exception as e:
            print(f"\nDiscord send failed: {e}")


if __name__ == "__main__":
    main()