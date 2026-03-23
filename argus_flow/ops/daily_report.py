"""Daily Report — summary of all IBKR runners for the day.

Usage:
    python -m argus_flow.ops.daily_report
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

RUNNERS = [
    {"name": "EUR/USD", "symbol": "EURUSD", "log_dir": "argus_flow/logs/eurusd", "config": "argus_flow/configs/eurusd_t4_paper_v1.json", "unit": "pips"},
    {"name": "MNQ", "symbol": "MNQ", "log_dir": "argus_flow/logs/mnq", "config": "argus_flow/configs/mnq_vol_burst_paper_v1.json", "unit": "pts"},
    {"name": "GBP/USD", "symbol": "GBPUSD", "log_dir": "argus_flow/logs/gbpusd", "config": "argus_flow/configs/gbpusd_range_paper_v1.json", "unit": "pips"},
]


def _load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, "r") as f:
        return list(csv.DictReader(f))


def generate_report() -> dict:
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    report = {"date": today, "timestamp": now.isoformat(), "runners": []}

    for runner in RUNNERS:
        log_dir = REPO / runner["log_dir"]
        cfg_path = REPO / runner["config"]

        r = {"name": runner["name"], "symbol": runner["symbol"], "unit": runner["unit"]}

        # Config
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text())
            r["strategy"] = cfg.get("strategy", "")
            r["replay_exp"] = cfg.get("replay_expectations", {})

        # Signals
        signals = _load_csv(log_dir / "signals.csv")
        today_signals = [s for s in signals if s.get("ts", "").startswith(today)]
        r["signals_total"] = len(signals)
        r["signals_today"] = len(today_signals)
        r["entries_today"] = sum(1 for s in today_signals if s.get("action") == "ENTRY")

        # Trades
        trades = _load_csv(log_dir / "trades.csv")
        pnl_field = "pnl_pips" if trades and "pnl_pips" in trades[0] else "pnl_pts"
        today_trades = [t for t in trades if t.get("ts", "").startswith(today)]

        r["trades_total"] = len(trades)
        r["trades_today"] = len(today_trades)

        # All-time metrics
        if trades:
            pnls = [float(t.get(pnl_field, 0)) for t in trades]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            r["total_pnl"] = round(sum(pnls), 2)
            r["win_rate"] = round(len(wins) / len(pnls), 3) if pnls else 0
            r["profit_factor"] = round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else 0
            r["avg_win"] = round(sum(wins) / len(wins), 2) if wins else 0
            r["avg_loss"] = round(sum(losses) / len(losses), 2) if losses else 0

            # Max drawdown
            cum = 0
            peak = 0
            max_dd = 0
            for p in pnls:
                cum += p
                peak = max(peak, cum)
                dd = peak - cum
                max_dd = max(max_dd, dd)
            r["max_drawdown"] = round(max_dd, 2)
        else:
            r["total_pnl"] = 0
            r["win_rate"] = 0
            r["profit_factor"] = 0

        # Today metrics
        if today_trades:
            today_pnls = [float(t.get(pnl_field, 0)) for t in today_trades]
            r["today_pnl"] = round(sum(today_pnls), 2)
            r["today_wins"] = sum(1 for p in today_pnls if p > 0)
            r["today_losses"] = sum(1 for p in today_pnls if p <= 0)
        else:
            r["today_pnl"] = 0

        # Divergence check
        replay_spd = r.get("replay_exp", {}).get("signals_per_day", 0)
        if signals and len(signals) > 10:
            try:
                first = datetime.fromisoformat(signals[0]["ts"].replace("Z", "+00:00"))
                last = datetime.fromisoformat(signals[-1]["ts"].replace("Z", "+00:00"))
                days = max((last - first).total_seconds() / 86400, 0.001)
                entries = sum(1 for s in signals if s.get("action") == "ENTRY")
                live_spd = entries / days
                r["live_signals_per_day"] = round(live_spd, 1)
                if replay_spd > 0:
                    r["signal_freq_ratio"] = round(live_spd / replay_spd, 2)
            except Exception:
                pass

        report["runners"].append(r)

    return report


def print_report(report: dict):
    print(f"\n{'='*65}")
    print(f"  IBKR Fleet Daily Report — {report['date']}")
    print(f"{'='*65}")

    for r in report["runners"]:
        print(f"\n  {r['name']:>10s} ({r.get('strategy', '')})")
        print(f"    Today:  signals={r['signals_today']}  entries={r.get('entries_today', 0)}  trades={r['trades_today']}  PnL={r.get('today_pnl', 0):+.1f} {r['unit']}")
        print(f"    Total:  signals={r['signals_total']}  trades={r['trades_total']}  PnL={r.get('total_pnl', 0):+.1f} {r['unit']}")
        if r["trades_total"] > 0:
            print(f"    Perf:   WR={r['win_rate']:.1%}  PF={r['profit_factor']}  avg_W={r['avg_win']:+.1f}  avg_L={r['avg_loss']:+.1f}  maxDD={r.get('max_drawdown', 0):.1f}")
        if r.get("signal_freq_ratio"):
            ratio = r["signal_freq_ratio"]
            flag = " WATCH" if ratio < 0.5 or ratio > 2.0 else ""
            print(f"    Replay: {r.get('live_signals_per_day', 0):.1f}/day vs {r.get('replay_exp', {}).get('signals_per_day', '?')}/day (ratio={ratio:.2f}){flag}")

    # Fleet totals
    total_trades = sum(r["trades_total"] for r in report["runners"])
    total_pnl = sum(r.get("total_pnl", 0) for r in report["runners"])
    today_pnl = sum(r.get("today_pnl", 0) for r in report["runners"])
    print(f"\n  {'FLEET':>10s}:  trades={total_trades}  total_pnl={total_pnl:+.1f}  today_pnl={today_pnl:+.1f}")
    print()


def main():
    report = generate_report()
    print_report(report)

    # Save JSON
    date_str = report["date"].replace("-", "")
    out_json = REPO / "argus_flow" / "logs" / f"daily_report_{date_str}.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2, default=str))
    print(f"  Saved: {out_json}")


if __name__ == "__main__":
    main()