#!/usr/bin/env python3
"""ops/weekly_digest.py -- Weekly performance digest, optionally sent to Discord.

Usage:
    python ops/weekly_digest.py              # last 7 days, send to Discord
    python ops/weekly_digest.py --days 14    # last 14 days
    python ops/weekly_digest.py --no-discord # print only
"""
import csv
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from ops.notify import send_discord

LOGS = REPO / "ops" / "logs"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ts(s: str) -> datetime:
    """Parse an ISO-8601 timestamp to an aware UTC datetime."""
    s = s.strip()
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Trade journal aggregation
# ---------------------------------------------------------------------------

def _load_trades(days: int) -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    trades = []
    for p in sorted(LOGS.glob("trade_journal_*.csv")):
        with open(p, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                exit_ts_raw = row.get("exit_time", "").strip()
                if not exit_ts_raw:
                    continue
                try:
                    exit_ts = _parse_ts(exit_ts_raw)
                except Exception:
                    continue
                if exit_ts >= cutoff:
                    trades.append(row)
    return trades


def _compute_trade_stats(trades: list) -> dict:
    if not trades:
        return {}
    pnls = [_safe_float(r.get("pnl")) for r in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total_pnl = sum(pnls)
    gross_win = sum(wins) if wins else 0.0
    gross_loss = abs(sum(losses)) if losses else 0.0
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0

    exit_reasons = Counter(r.get("exit_reason", "?") for r in trades)
    regimes = Counter(r.get("regime_at_entry", "?") for r in trades)

    best = max(pnls)
    worst = min(pnls)

    return {
        "total": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100,
        "total_pnl": total_pnl,
        "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
        "profit_factor": pf,
        "best": best,
        "worst": worst,
        "top_exit_reason": exit_reasons.most_common(1)[0][0] if exit_reasons else "N/A",
        "top_regime": regimes.most_common(1)[0][0] if regimes else "N/A",
    }


# ---------------------------------------------------------------------------
# Backtest summary aggregation
# ---------------------------------------------------------------------------

def _load_bt_summaries(days: int) -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    results = []
    for p in sorted(LOGS.glob("bt_summary_bt_*.json")):
        name = p.stem
        parts = name.split("_")
        if len(parts) < 4:
            continue
        ts_str = parts[3]
        try:
            dt = datetime.strptime(ts_str, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if dt >= cutoff:
            try:
                with open(p) as f:
                    data = json.load(f)
                data["_file_ts"] = dt
                results.append(data)
            except Exception:
                continue
    return results


def _compute_bt_stats(summaries: list) -> dict:
    if not summaries:
        return {"count": 0}
    pfs = [_safe_float(s.get("profit_factor")) for s in summaries]
    best_pf = max(pfs) if pfs else 0.0
    best_run = None
    for s in summaries:
        if _safe_float(s.get("profit_factor")) == best_pf:
            best_run = s.get("run_id", "?")
            break
    return {
        "count": len(summaries),
        "best_pf": best_pf,
        "best_run": best_run,
    }


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _build_embed(days: int, ts: dict, bs: dict) -> dict:
    pnl = ts.get("total_pnl", 0.0)
    color = 0x00E676 if pnl > 0 else 0xFF5252 if pnl < 0 else 0xBDBDBD

    fields = []
    if ts:
        pf_str = f"{ts['profit_factor']:.2f}" if ts["profit_factor"] != float("inf") else "inf"
        fields += [
            {"name": "Trades", "value": str(ts["total"]), "inline": True},
            {"name": "Win Rate", "value": f"{ts['win_rate']:.1f}%", "inline": True},
            {"name": "PnL", "value": f"${ts['total_pnl']:+.4f}", "inline": True},
            {"name": "Avg Win", "value": f"${ts['avg_win']:.4f}", "inline": True},
            {"name": "Avg Loss", "value": f"${ts['avg_loss']:.4f}", "inline": True},
            {"name": "PF", "value": pf_str, "inline": True},
            {"name": "Best Trade", "value": f"${ts['best']:+.4f}", "inline": True},
            {"name": "Worst Trade", "value": f"${ts['worst']:+.4f}", "inline": True},
            {"name": "Top Exit", "value": ts["top_exit_reason"], "inline": True},
            {"name": "Top Regime", "value": ts["top_regime"], "inline": True},
        ]
    else:
        fields.append({"name": "Live Trades", "value": "No closed trades in window", "inline": False})

    if bs.get("count", 0) > 0:
        fields.append({"name": "Backtests Run", "value": str(bs["count"]), "inline": True})
        fields.append({"name": "Best BT PF", "value": f"{bs['best_pf']:.4f}", "inline": True})
        if bs.get("best_run"):
            fields.append({"name": "Best BT Run", "value": bs["best_run"][-20:], "inline": True})
    else:
        fields.append({"name": "Backtests", "value": "None in window", "inline": False})

    embed = {
        "title": f"Argus {days}-Day Digest",
        "color": color,
        "fields": fields,
        "footer": {"text": f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"},
    }
    return embed


def _format_plain(days: int, ts: dict, bs: dict) -> str:
    lines = [f"=== Argus {days}-Day Digest ===", ""]
    if ts:
        pf_str = f"{ts['profit_factor']:.2f}" if ts["profit_factor"] != float("inf") else "inf"
        lines += [
            "-- Live Trades --",
            f"  Trades:      {ts['total']}",
            f"  Win Rate:    {ts['win_rate']:.1f}%",
            f"  PnL:         ${ts['total_pnl']:+.4f}",
            f"  Avg Win:     ${ts['avg_win']:.4f}",
            f"  Avg Loss:    ${ts['avg_loss']:.4f}",
            f"  PF:          {pf_str}",
            f"  Best Trade:  ${ts['best']:+.4f}",
            f"  Worst Trade: ${ts['worst']:+.4f}",
            f"  Top Exit:    {ts['top_exit_reason']}",
            f"  Top Regime:  {ts['top_regime']}",
            "",
        ]
    else:
        lines += ["-- Live Trades --", "  No closed trades in window", ""]

    if bs.get("count", 0) > 0:
        lines += [
            "-- Backtests --",
            f"  Runs:        {bs['count']}",
            f"  Best PF:     {bs['best_pf']:.4f}",
            f"  Best Run:    {bs.get('best_run', '?')}",
        ]
    else:
        lines += ["-- Backtests --", "  None in window"]

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    days = 7
    no_discord = False

    i = 0
    while i < len(args):
        if args[i] == "--days" and i + 1 < len(args):
            days = int(args[i + 1])
            i += 2
        elif args[i] == "--no-discord":
            no_discord = True
            i += 1
        else:
            print(f"Unknown arg: {args[i]}")
            sys.exit(1)

    trades = _load_trades(days)
    ts = _compute_trade_stats(trades)
    bt_summaries = _load_bt_summaries(days)
    bs = _compute_bt_stats(bt_summaries)

    plain = _format_plain(days, ts, bs)
    print(plain)

    if no_discord:
        return

    embed = _build_embed(days, ts, bs)
    ok = send_discord(embed=embed)
    if ok:
        print("[digest] Sent to Discord.")
    else:
        print("[digest] Discord send failed or webhook not configured.", file=sys.stderr)


if __name__ == "__main__":
    main()