"""Weekly FX Fleet Digest — comprehensive Discord report across all Class A pairs.

Reads trades from all cohort pairs, computes per-pair and fleet-wide metrics
for the last 7 days, and sends a rich Discord embed.

Usage:
    python -m argus_flow.ops.weekly_digest
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from argus_flow.ops.discord_alerts import send_discord

REPO = Path(__file__).resolve().parents[2]

RUNNERS = [
    {"name": "EUR/USD", "symbol": "EURUSD", "log_dir": "argus_flow/logs/eurusd", "unit": "pips"},
    {"name": "GBP/USD", "symbol": "GBPUSD", "log_dir": "argus_flow/logs/gbpusd", "unit": "pips"},
    {"name": "EUR/JPY", "symbol": "EURJPY", "log_dir": "argus_flow/logs/eurjpy", "unit": "pips"},
]

COHORT_TARGET = 30  # valid trades needed for promotion


def _load_trades(log_dir: Path) -> list[dict]:
    trade_file = log_dir / "trades.csv"
    if not trade_file.exists():
        return []
    with open(trade_file, "r") as f:
        return list(csv.DictReader(f))


def _filter_last_7_days(trades: list[dict]) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    result = []
    for t in trades:
        try:
            ts = datetime.fromisoformat(t["ts"].replace("Z", "+00:00"))
            if ts >= cutoff:
                result.append(t)
        except (KeyError, ValueError):
            continue
    return result


def _compute_metrics(trades: list[dict], pnl_field: str) -> dict:
    if not trades:
        return {
            "count": 0, "valid": 0, "win_rate": 0, "profit_factor": 0,
            "total_pnl": 0, "avg_win": 0, "avg_loss": 0, "max_consec_losses": 0,
        }

    valid_trades = [t for t in trades if t.get("experiment_valid", "true").lower() == "true"]
    pnls = [float(t.get(pnl_field, 0)) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    wr = len(wins) / len(pnls) if pnls else 0
    pf = round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else 0

    # Max consecutive losses
    max_consec = 0
    current_consec = 0
    for p in pnls:
        if p <= 0:
            current_consec += 1
            max_consec = max(max_consec, current_consec)
        else:
            current_consec = 0

    return {
        "count": len(trades),
        "valid": len(valid_trades),
        "win_rate": round(wr, 3),
        "profit_factor": pf,
        "total_pnl": round(sum(pnls), 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
        "max_consec_losses": max_consec,
    }


def _progress_bar(current: int, target: int, width: int = 15) -> str:
    ratio = min(current / target, 1.0) if target > 0 else 0
    filled = int(ratio * width)
    empty = width - filled
    pct = int(ratio * 100)
    return f"[{'█' * filled}{'░' * empty}] {current}/{target} ({pct}%)"


def _load_divergence_report() -> dict:
    path = REPO / "argus_flow" / "logs" / "divergence_report.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def generate_digest() -> dict:
    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    week_end = now.strftime("%Y-%m-%d")

    div_report = _load_divergence_report()
    div_runners = {r["symbol"]: r for r in div_report.get("runners", [])}

    pair_results = []
    fleet_pnl = 0
    fleet_trades = 0
    best_pair = None
    worst_pair = None

    for runner in RUNNERS:
        log_dir = REPO / runner["log_dir"]
        pnl_field = "pnl_pips" if runner["unit"] == "pips" else "pnl_pts"

        all_trades = _load_trades(log_dir)
        week_trades = _filter_last_7_days(all_trades)
        metrics = _compute_metrics(week_trades, pnl_field)

        # Cohort progress (all valid trades, not just this week)
        all_valid = len([t for t in all_trades if t.get("experiment_valid", "true").lower() == "true"])

        # Divergence status
        div_info = div_runners.get(runner["symbol"], {})
        div_status = div_info.get("status", "UNKNOWN")

        pair_data = {
            "name": runner["name"],
            "symbol": runner["symbol"],
            "unit": runner["unit"],
            "metrics": metrics,
            "cohort_valid": all_valid,
            "cohort_target": COHORT_TARGET,
            "divergence_status": div_status,
        }
        pair_results.append(pair_data)

        fleet_pnl += metrics["total_pnl"]
        fleet_trades += metrics["count"]

        if best_pair is None or metrics["total_pnl"] > best_pair["metrics"]["total_pnl"]:
            best_pair = pair_data
        if worst_pair is None or metrics["total_pnl"] < worst_pair["metrics"]["total_pnl"]:
            worst_pair = pair_data

    return {
        "week_start": week_start,
        "week_end": week_end,
        "pairs": pair_results,
        "fleet_trades": fleet_trades,
        "fleet_pnl": round(fleet_pnl, 2),
        "best_pair": best_pair["name"] if best_pair else "N/A",
        "worst_pair": worst_pair["name"] if worst_pair else "N/A",
    }


def send_weekly_digest():
    digest = generate_digest()

    # Build per-pair fields
    fields = []
    for p in digest["pairs"]:
        m = p["metrics"]
        sign = "+" if m["total_pnl"] >= 0 else ""
        div_emoji = {"PASS": "OK", "WATCH": "WATCH", "KILL": "KILL", "COLLECTING": "..."}.get(p["divergence_status"], "?")

        value_lines = [
            f"Trades: {m['count']} ({m['valid']} valid)",
            f"WR: {m['win_rate']:.0%} | PF: {m['profit_factor']}",
            f"PnL: {sign}{m['total_pnl']:.1f} {p['unit']}",
            f"Avg W: {m['avg_win']:+.1f} | Avg L: {m['avg_loss']:+.1f}",
            f"Max Consec L: {m['max_consec_losses']}",
            f"Divergence: {div_emoji}",
        ]
        fields.append({
            "name": p["name"],
            "value": "\n".join(value_lines),
            "inline": False,
        })

    # Cohort progress section
    progress_lines = []
    for p in digest["pairs"]:
        bar = _progress_bar(p["cohort_valid"], p["cohort_target"])
        progress_lines.append(f"{p['name']}: {bar}")

    fields.append({
        "name": "Cohort Progress (valid trades)",
        "value": "```\n" + "\n".join(progress_lines) + "\n```",
        "inline": False,
    })

    # Fleet summary
    fleet_sign = "+" if digest["fleet_pnl"] >= 0 else ""
    fields.append({
        "name": "Fleet Summary",
        "value": (
            f"Total trades: {digest['fleet_trades']}\n"
            f"Fleet PnL: {fleet_sign}{digest['fleet_pnl']:.1f} pips\n"
            f"Best pair: {digest['best_pair']}\n"
            f"Worst pair: {digest['worst_pair']}"
        ),
        "inline": False,
    })

    color = 0x00FF88 if digest["fleet_pnl"] >= 0 else 0xFF4444

    embed = {
        "title": f"Weekly FX Digest — {digest['week_start']} to {digest['week_end']}",
        "color": color,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Argus IBKR Fleet"},
    }

    ok = send_discord(embeds=[embed])
    if ok:
        print(f"Weekly digest sent for {digest['week_start']} to {digest['week_end']}")
    else:
        print("Failed to send weekly digest")

    return digest


def main():
    digest = send_weekly_digest()

    # Print summary to stdout as well
    print(f"\n{'='*60}")
    print(f"  WEEKLY FX DIGEST — {digest['week_start']} to {digest['week_end']}")
    print(f"{'='*60}")

    for p in digest["pairs"]:
        m = p["metrics"]
        sign = "+" if m["total_pnl"] >= 0 else ""
        bar = _progress_bar(p["cohort_valid"], p["cohort_target"])
        print(f"\n  {p['name']}:")
        print(f"    Trades: {m['count']} | WR: {m['win_rate']:.0%} | PF: {m['profit_factor']} | PnL: {sign}{m['total_pnl']:.1f} {p['unit']}")
        print(f"    Avg W: {m['avg_win']:+.1f} | Avg L: {m['avg_loss']:+.1f} | Max Consec L: {m['max_consec_losses']}")
        print(f"    Divergence: {p['divergence_status']}")
        print(f"    Cohort: {bar}")

    fleet_sign = "+" if digest["fleet_pnl"] >= 0 else ""
    print(f"\n  Fleet: {digest['fleet_trades']} trades | {fleet_sign}{digest['fleet_pnl']:.1f} pips")
    print(f"  Best: {digest['best_pair']} | Worst: {digest['worst_pair']}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
