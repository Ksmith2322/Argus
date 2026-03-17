#!/usr/bin/env python3
"""
compile_queue_results.py — Parse all bt_summary_*.json files and produce a
ranked leaderboard CSV + console summary with coin tier classification.

Usage:
    python ops/compile_queue_results.py              # full leaderboard
    python ops/compile_queue_results.py --top 20     # top 20 only
    python ops/compile_queue_results.py --coin ETH   # filter by coin
    python ops/compile_queue_results.py --tiers      # show tier report + deploy recommendations
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

LOGS_DIR = Path("C:/Argus/repo/ops/logs")
OUT_CSV = LOGS_DIR / "queue_leaderboard.csv"
OUT_TIERS = LOGS_DIR / "coin_tiers.json"


def load_all_summaries() -> list[dict]:
    """Load every bt_summary_bt_*.json in the logs dir."""
    results = []
    for f in LOGS_DIR.glob("bt_summary_bt_*.json"):
        if "latest" in f.name:
            continue
        try:
            data = json.loads(f.read_text())
            data["_file"] = f.name
            results.append(data)
        except Exception:
            pass
    return results


def extract_label(run_id: str) -> str:
    """Try to match run_id back to a queue label via queue_results.log."""
    log_path = LOGS_DIR / "queue_results.log"
    if not log_path.exists():
        return ""
    text = log_path.read_text()
    for line in text.splitlines():
        if run_id in line and "DONE:" in line:
            m = re.search(r"DONE:\s*(\S+)", line)
            if m:
                return m.group(1)
    return ""


def compute_tier_score(pf: float, wr: float, trades: int, dd: float) -> float:
    """Composite score: weighted blend of PF, WR, trade count, and drawdown.

    Score range: 0-100.  Higher = better candidate for live trading.
    """
    # PF component (0-40 pts): PF 1.0 = 20, PF 2.0 = 40
    pf_score = min(40, max(0, (pf - 0.5) * 26.67))

    # Win rate component (0-25 pts): 50% = 25
    wr_score = min(25, max(0, wr * 0.5))

    # Trade count component (0-20 pts): 50+ trades = 20
    trade_score = min(20, max(0, trades * 0.4))

    # Drawdown penalty (0 to -15 pts): DD 5% = -15
    dd_penalty = min(15, max(0, dd * 3))

    return round(pf_score + wr_score + trade_score - dd_penalty, 1)


def classify_tier(best_pf: float, best_wr: float, best_trades: int,
                  profitable_pct: float, tier_score: float) -> str:
    """Classify coin into TRADE / WATCH / SKIP.

    TRADE: Proven profitable with sufficient trades
    WATCH: Shows potential but needs more validation
    SKIP: Consistently unprofitable
    """
    if best_pf >= 1.1 and best_trades >= 10 and tier_score >= 35:
        return "TRADE"
    if best_pf >= 1.0 and best_trades >= 5:
        return "TRADE"
    if best_pf >= 0.85 or profitable_pct >= 0.2:
        return "WATCH"
    return "SKIP"


def extract_best_config(summaries: list[dict], coin: str) -> dict:
    """Find the best performing config for a given coin."""
    coin_runs = [s for s in summaries if s.get("_coin", "") == coin]
    if not coin_runs:
        return {}
    best = max(coin_runs, key=lambda s: float(s.get("profit_factor", 0)))
    return best


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=0, help="Show only top N results")
    parser.add_argument("--coin", type=str, default="", help="Filter by coin symbol")
    parser.add_argument("--min-trades", type=int, default=5, help="Min trades to include")
    parser.add_argument("--tiers", action="store_true", help="Show detailed tier report")
    args = parser.parse_args()

    summaries = load_all_summaries()
    if not summaries:
        print("No summaries found in", LOGS_DIR)
        return

    # Enrich with label
    for s in summaries:
        run_id = s.get("run_id", "")
        s["_label"] = extract_label(run_id)
        s["_coin"] = s.get("symbol", "").replace("-USD", "")

    all_summaries = list(summaries)  # keep unfiltered for tier analysis

    # Filter
    if args.coin:
        summaries = [s for s in summaries if s["_coin"].upper() == args.coin.upper()]

    if args.min_trades:
        summaries = [s for s in summaries if s.get("trades_closed", 0) >= args.min_trades]

    # Sort by profit_factor descending
    summaries.sort(key=lambda s: float(s.get("profit_factor", 0)), reverse=True)

    if args.top:
        summaries = summaries[:args.top]

    # Print leaderboard
    print(f"\n{'='*110}")
    print(f"  BACKTEST LEADERBOARD -- {len(summaries)} runs")
    print(f"{'='*110}")
    print(f"{'Rank':>4} {'Label':<40} {'Coin':<6} {'PF':>6} {'WR%':>6} {'Trades':>7} {'PnL':>10} {'DD%':>6} {'AvgWin':>8} {'AvgLoss':>8}")
    print(f"{'-'*110}")

    for i, s in enumerate(summaries, 1):
        label = s.get("_label", "") or s.get("run_id", "")[:30]
        coin = s.get("_coin", "?")
        pf = float(s.get("profit_factor", 0))
        wr = float(s.get("win_rate_pct", 0))
        trades = int(s.get("trades_closed", 0))
        pnl = float(s.get("pnl_usd", 0))
        dd = float(s.get("max_drawdown_pct", 0))
        avg_win = float(s.get("avg_win_usd", 0))
        avg_loss = float(s.get("avg_loss_usd", 0))

        pf_mark = "**" if pf >= 1.0 else "  "
        print(f"{i:>4} {label:<40} {coin:<6} {pf_mark}{pf:>4.3f} {wr:>5.1f}% {trades:>7} ${pnl:>8.2f} {dd:>5.2f}% ${avg_win:>6.3f} ${avg_loss:>7.3f}")

    # Summary stats
    profitable = [s for s in summaries if float(s.get("profit_factor", 0)) >= 1.0]
    print(f"\n{'='*110}")
    print(f"  PROFITABLE: {len(profitable)}/{len(summaries)} runs (PF >= 1.0)")

    # Per-coin analysis (use ALL summaries, not filtered)
    coins: dict[str, dict] = {}
    for s in all_summaries:
        if s.get("trades_closed", 0) < 3:
            continue
        coin = s.get("_coin", "?")
        pf = float(s.get("profit_factor", 0))
        wr = float(s.get("win_rate_pct", 0))
        trades = int(s.get("trades_closed", 0))
        dd = float(s.get("max_drawdown_pct", 0))
        label = s.get("_label", "") or s.get("run_id", "")[:20]

        if coin not in coins:
            coins[coin] = {
                "count": 0, "profitable": 0,
                "best_pf": 0, "best_wr": 0, "best_trades": 0,
                "best_label": "", "best_run_id": "",
                "all_pf": [], "max_dd": 0,
                "best_summary": None,
            }
        c = coins[coin]
        c["count"] += 1
        c["all_pf"].append(pf)
        if pf >= 1.0:
            c["profitable"] += 1
        if dd > c["max_dd"]:
            c["max_dd"] = dd
        if pf > c["best_pf"]:
            c["best_pf"] = pf
            c["best_wr"] = wr
            c["best_trades"] = trades
            c["best_label"] = label
            c["best_run_id"] = s.get("run_id", "")
            c["best_summary"] = s

    # Compute tier scores and classify
    tier_data = {}
    for coin, c in coins.items():
        avg_pf = sum(c["all_pf"]) / len(c["all_pf"]) if c["all_pf"] else 0
        profitable_pct = c["profitable"] / c["count"] if c["count"] > 0 else 0
        tier_score = compute_tier_score(c["best_pf"], c["best_wr"], c["best_trades"], c["max_dd"])
        tier = classify_tier(c["best_pf"], c["best_wr"], c["best_trades"], profitable_pct, tier_score)

        tier_data[coin] = {
            "tier": tier,
            "tier_score": tier_score,
            "runs": c["count"],
            "profitable": c["profitable"],
            "profitable_pct": round(profitable_pct * 100, 1),
            "best_pf": c["best_pf"],
            "avg_pf": round(avg_pf, 3),
            "best_wr": c["best_wr"],
            "best_trades": c["best_trades"],
            "max_dd": c["max_dd"],
            "best_label": c["best_label"],
            "best_run_id": c["best_run_id"],
        }

    # Print per-coin summary
    print(f"\n  PER-COIN SUMMARY:")
    print(f"  {'Coin':<8} {'Tier':<6} {'Score':>5} {'Runs':>5} {'Win%':>5} {'BestPF':>7} {'AvgPF':>7} {'BestWR':>6} {'MaxDD':>6} {'Best Config':<30}")
    for coin in sorted(tier_data.keys(), key=lambda c: tier_data[c]["tier_score"], reverse=True):
        t = tier_data[coin]
        tier_mark = {"TRADE": "++", "WATCH": " ~", "SKIP": " -"}[t["tier"]]
        print(f"  {coin:<8} {tier_mark}{t['tier']:<4} {t['tier_score']:>5.1f} {t['runs']:>5} {t['profitable_pct']:>4.0f}% {t['best_pf']:>7.3f} {t['avg_pf']:>7.3f} {t['best_wr']:>5.1f}% {t['max_dd']:>5.2f}% {t['best_label']:<30}")

    # Tier buckets
    trade_coins = sorted([c for c, t in tier_data.items() if t["tier"] == "TRADE"],
                         key=lambda c: tier_data[c]["tier_score"], reverse=True)
    watch_coins = sorted([c for c, t in tier_data.items() if t["tier"] == "WATCH"],
                         key=lambda c: tier_data[c]["tier_score"], reverse=True)
    skip_coins = sorted([c for c, t in tier_data.items() if t["tier"] == "SKIP"],
                        key=lambda c: tier_data[c]["tier_score"], reverse=True)

    print(f"\n  COIN TIERS:")
    print(f"  TRADE: {', '.join(trade_coins) if trade_coins else 'none'}")
    print(f"  WATCH: {', '.join(watch_coins) if watch_coins else 'none'}")
    print(f"  SKIP:  {', '.join(skip_coins) if skip_coins else 'none'}")

    if args.tiers:
        # Detailed tier report with deploy recommendations
        print(f"\n{'='*110}")
        print(f"  DEPLOYMENT RECOMMENDATIONS")
        print(f"{'='*110}")

        for coin in trade_coins:
            t = tier_data[coin]
            best = coins[coin].get("best_summary", {})
            print(f"\n  {coin} [TRADE] -- Score: {t['tier_score']}")
            print(f"    Best config: {t['best_label']}")
            print(f"    PF={t['best_pf']:.3f}  WR={t['best_wr']:.1f}%  Trades={t['best_trades']}  DD={t['max_dd']:.2f}%")
            if best:
                print(f"    Recommended .env overrides:")
                # Extract key config differences from label
                label = t["best_label"]
                if "be" in label.lower():
                    print(f"      EXIT_BREAKEVEN_TRIGGER_PCT=0.008")
                if "nogov" in label.lower():
                    print(f"      USE_ML_GOVERNOR=false")
                if "loose" in label.lower():
                    print(f"      CONFLUENCE_MIN_SCORE=75")
                if "scalp" in label.lower():
                    print(f"      TAKE_PROFIT_PCT=0.008  STOP_LOSS_PCT=0.012  MAX_HOLD_SECONDS=1800")

        for coin in watch_coins[:5]:
            t = tier_data[coin]
            print(f"\n  {coin} [WATCH] -- Score: {t['tier_score']}")
            print(f"    Best config: {t['best_label']}  PF={t['best_pf']:.3f}")
            print(f"    Action: Run 45-day validation, test with no-governor + wider exits")

        if skip_coins:
            print(f"\n  SKIP coins ({len(skip_coins)}): {', '.join(skip_coins)}")
            print(f"    Action: Do not trade. Re-test in 30 days with updated strategy.")

    # Save tier data as JSON for programmatic use
    with open(OUT_TIERS, "w") as f:
        json.dump(tier_data, f, indent=2)
    print(f"\n  Tier data saved: {OUT_TIERS}")

    # Write CSV
    fields = ["rank", "label", "coin", "tier", "tier_score", "profit_factor", "win_rate_pct",
              "trades_closed", "pnl_usd", "max_drawdown_pct", "avg_win_usd", "avg_loss_usd",
              "avg_trade_duration_s", "exposure_pct", "run_id"]
    with open(OUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for i, s in enumerate(summaries, 1):
            coin = s.get("_coin", "")
            td = tier_data.get(coin, {})
            writer.writerow({
                "rank": i,
                "label": s.get("_label", "") or s.get("run_id", ""),
                "coin": coin,
                "tier": td.get("tier", ""),
                "tier_score": td.get("tier_score", ""),
                "profit_factor": s.get("profit_factor", ""),
                "win_rate_pct": s.get("win_rate_pct", ""),
                "trades_closed": s.get("trades_closed", ""),
                "pnl_usd": s.get("pnl_usd", ""),
                "max_drawdown_pct": s.get("max_drawdown_pct", ""),
                "avg_win_usd": s.get("avg_win_usd", ""),
                "avg_loss_usd": s.get("avg_loss_usd", ""),
                "avg_trade_duration_s": s.get("avg_trade_duration_s", ""),
                "exposure_pct": s.get("exposure_pct", ""),
                "run_id": s.get("run_id", ""),
            })

    print(f"  Leaderboard CSV saved: {OUT_CSV}")
    print(f"{'='*110}\n")


if __name__ == "__main__":
    main()