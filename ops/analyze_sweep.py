#!/usr/bin/env python3
"""ops/analyze_sweep.py -- Analyze completed backtest sweep results.

Reads bt_summary_*.json + run_header_*.json from ops/logs/,
decodes params from run labels, and prints ranked results.

Usage:
    python ops/analyze_sweep.py
    python ops/analyze_sweep.py --coin ETH
    python ops/analyze_sweep.py --coin BTC --top 10
    python ops/analyze_sweep.py --min-trades 20 --min-pf 1.2
    python ops/analyze_sweep.py --pivot hold    # pivot table by param
    python ops/analyze_sweep.py --save          # write CSV to ops/logs/
"""
from __future__ import annotations
import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
LOG_DIR = REPO / "ops" / "logs"


# ──────────────────────────────────────────────────────────────────────────────
# Parse label → params dict
# Format: sweep_ETH_tp4_sl2_s88_g38_h3600
#         sweep_BTC_tp6_sl25_s82_g32_h3600
# ──────────────────────────────────────────────────────────────────────────────

def _parse_label(label: str) -> Dict[str, Any]:
    """Extract params from sweep label. Returns {} if not a sweep label."""
    params: Dict[str, Any] = {}
    if not label or not label.startswith("sweep_"):
        return params

    parts = label.split("_")
    if len(parts) < 3:
        return params

    # Coin is second part
    params["coin"] = parts[1].upper()

    for part in parts[2:]:
        # tp4 → take_profit_pct=0.04
        m = re.fullmatch(r"tp(\d+)", part)
        if m:
            params["tp_pct"] = int(m.group(1)) / 100
            continue
        # sl2 or sl25 → stop_loss_pct=0.02 or 0.025
        m = re.fullmatch(r"sl(\d+)", part)
        if m:
            v = int(m.group(1))
            # sl2→0.02, sl25→0.025, sl3→0.03
            params["sl_pct"] = v / 100 if v <= 9 else v / 1000
            continue
        # s88 → score=88
        m = re.fullmatch(r"s(\d+)", part)
        if m:
            params["score"] = int(m.group(1))
            continue
        # g38 → gate=0.38
        m = re.fullmatch(r"g(\d+)", part)
        if m:
            v = int(m.group(1))
            params["gate"] = v / 100
            continue
        # h3600 → hold_s=3600
        m = re.fullmatch(r"h(\d+)", part)
        if m:
            params["hold_s"] = int(m.group(1))
            continue

    return params


# ──────────────────────────────────────────────────────────────────────────────
# Load all sweep results
# ──────────────────────────────────────────────────────────────────────────────

def _breakeven_wr(tp_pct: Optional[float], sl_pct: Optional[float],
                  fee_bps: float = 60.0) -> Optional[float]:
    """Return the WR% needed to break even after round-trip fees.
    Uses: WR_be = net_loss / (net_win + net_loss)
    net_win  = TP_pct - 2 * fee_pct
    net_loss = SL_pct + 2 * fee_pct
    """
    if tp_pct is None or sl_pct is None:
        return None
    fee_pct = fee_bps / 10000.0
    net_win  = tp_pct - 2 * fee_pct
    net_loss = sl_pct + 2 * fee_pct
    if net_win <= 0 or (net_win + net_loss) <= 0:
        return None
    return net_loss / (net_win + net_loss) * 100.0


def load_results(log_dir: Path = LOG_DIR) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []

    # Build run_id → (label, fee_bps) map from run_headers
    label_map: Dict[str, str] = {}
    fee_map: Dict[str, float] = {}
    for hdr_file in log_dir.glob("run_header_*.json"):
        try:
            with open(hdr_file) as f:
                h = json.load(f)
            rid = h.get("run_id", "")
            lbl = h.get("label", "")
            if rid and lbl:
                label_map[rid] = lbl
            # Read FEE_BPS: check direct field first, then queue env dict
            if "fee_bps" in h:
                fee_map[rid] = float(h["fee_bps"])
            else:
                env = h.get("env") or {}
                if isinstance(env, dict) and "FEE_BPS" in env:
                    fee_map[rid] = float(env["FEE_BPS"])
        except Exception:
            pass

    for summary_file in log_dir.glob("bt_summary_*.json"):
        try:
            with open(summary_file) as f:
                d = json.load(f)
            if not isinstance(d, dict):
                continue

            run_id = d.get("run_id", "")
            symbol = d.get("symbol", "")
            label = label_map.get(run_id, "")

            # Determine coin
            if "ETH" in symbol:
                coin = "ETH"
            elif "BTC" in symbol:
                coin = "BTC"
            elif "SOL" in symbol:
                coin = "SOL"
            else:
                coin = symbol.split("-")[0] if "-" in symbol else "UNK"

            params = _parse_label(label)
            if not params:
                # Try to infer coin from symbol
                params["coin"] = coin

            fee_bps = fee_map.get(run_id, 60.0)
            tp_p = params.get("tp_pct")
            sl_p = params.get("sl_pct")
            r: Dict[str, Any] = {
                "run_id": run_id,
                "label": label,
                "coin": params.get("coin", coin),
                "tp_pct": tp_p,
                "sl_pct": sl_p,
                "score": params.get("score"),
                "gate": params.get("gate"),
                "hold_s": params.get("hold_s"),
                "fee_bps": fee_bps,
                "pf": float(d.get("profit_factor", 0) or 0),
                "wr_pct": float(d.get("win_rate_pct", 0) or 0),
                "pnl": float(d.get("pnl_usd", 0) or 0),
                "trades": int(d.get("trades_closed", 0) or 0),
                "max_dd_pct": float(d.get("max_drawdown_pct", 0) or 0),
                "expectancy": float(d.get("expectancy_usd", 0) or 0),
                "exposure_pct": float(d.get("exposure_pct", 0) or 0),
                "avg_duration_s": float(d.get("avg_trade_duration_s", 0) or 0),
                "breakeven_wr": _breakeven_wr(tp_p, sl_p, fee_bps),
            }
            results.append(r)

        except Exception:
            pass

    return results


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _fmt_hold(hold_s: Optional[float]) -> str:
    if hold_s is None:
        return "?"
    h = int(hold_s)
    if h < 3600:
        return f"{h//60}m"
    return f"{h//3600}h"


def _fmt_pct(v: Optional[float]) -> str:
    return f"{v*100:.0f}%" if v is not None else "?"


def _color(val: float, pos_color: str = "\033[92m", neg_color: str = "\033[91m") -> str:
    """ANSI color wrap."""
    c = pos_color if val >= 0 else neg_color
    return f"{c}{val:.4f}\033[0m"


def _pivot(results: List[Dict], param: str, metric: str = "pf") -> None:
    """Print mean metric grouped by param value."""
    from collections import defaultdict
    groups: Dict[Any, List[float]] = defaultdict(list)
    for r in results:
        v = r.get(param)
        if v is not None:
            groups[v].append(float(r.get(metric, 0) or 0))

    print(f"\n  Pivot: mean {metric} by {param}")
    print(f"  {'Value':<12}  {'Count':>5}  {'Mean ' + metric:>10}  {'% PF>=1.0':>10}")
    print("  " + "-" * 45)
    for key in sorted(groups):
        vals = groups[key]
        mean_v = sum(vals) / len(vals) if vals else 0
        pct_ok = sum(1 for v in vals if v >= 1.0) / len(vals) * 100 if vals else 0
        print(f"  {str(key):<12}  {len(vals):>5}  {mean_v:>10.3f}  {pct_ok:>9.0f}%")


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> None:
    p = argparse.ArgumentParser(description="Analyze backtest sweep results")
    p.add_argument("--coin", default=None, help="Filter by coin (ETH/BTC/etc)")
    p.add_argument("--top", type=int, default=10, help="Number of top configs to show (default 10)")
    p.add_argument("--min-trades", type=int, default=15, help="Minimum trades filter (default 15)")
    p.add_argument("--min-pf", type=float, default=0.0, help="Minimum PF filter")
    p.add_argument("--sort", default="pf", choices=["pf", "pnl", "wr", "expectancy", "trades"],
                   help="Sort metric (default: pf)")
    p.add_argument("--pivot", default=None,
                   choices=["tp_pct", "sl_pct", "score", "gate", "hold_s"],
                   help="Show pivot table for a parameter")
    p.add_argument("--save", action="store_true", help="Save results to CSV")
    p.add_argument("--fee-bps", type=float, default=None,
                   help="Override fee rate for breakeven WR calc (default: from run header or 60)")
    args = p.parse_args(argv)

    results = load_results()
    if not results:
        print("No sweep results found in", LOG_DIR)
        return

    # Filter
    if args.coin:
        results = [r for r in results if r["coin"].upper() == args.coin.upper()]
    if args.min_trades > 0:
        results = [r for r in results if r["trades"] >= args.min_trades]
    if args.min_pf > 0:
        results = [r for r in results if r["pf"] >= args.min_pf]

    # Summary stats
    all_results = load_results()
    total = len(all_results)
    by_coin: Dict[str, int] = {}
    for r in all_results:
        by_coin[r["coin"]] = by_coin.get(r["coin"], 0) + 1

    print(f"\n{'='*70}")
    print(f"  Argus Sweep Analysis  |  {total} total runs completed")
    print(f"  Breakdown: " + "  ".join(f"{c}:{n}" for c, n in sorted(by_coin.items())))
    print(f"{'='*70}")

    # Group by coin for summary
    coins_to_show = [args.coin.upper()] if args.coin else sorted(set(r["coin"] for r in all_results))
    for coin in coins_to_show:
        coin_all = [r for r in all_results if r["coin"] == coin]
        coin_filtered = [r for r in results if r["coin"] == coin]
        profitable = [r for r in coin_all if r["pf"] >= 1.0 and r["trades"] >= args.min_trades]

        print(f"\n  [{coin}]  {len(coin_all)} runs  |  "
              f"{len(profitable)}/{len(coin_all)} profitable (PF>=1.0, trades>={args.min_trades})")

        # Sort and take top N
        sort_key = {"pf": "pf", "pnl": "pnl", "wr": "wr_pct", "expectancy": "expectancy",
                    "trades": "trades"}[args.sort]
        top_n = sorted(coin_filtered, key=lambda x: x.get(sort_key, 0), reverse=True)[:args.top]

        if not top_n:
            print(f"    No results pass the filters (min_trades={args.min_trades})")
            continue

        # Header
        print(f"\n  {'#':>2}  {'TP':>4} {'SL':>5} {'Score':>5} {'Hold':>4} "
              f"{'PF':>5} {'WR%':>6} {'BE-WR':>6} {'PnL':>7} {'Trades':>6} {'E[$/t]':>7}")
        print("  " + "-" * 72)

        for i, r in enumerate(top_n, 1):
            tp = _fmt_pct(r.get("tp_pct"))
            sl = _fmt_pct(r.get("sl_pct"))
            score = str(r.get("score") or "?")
            hold = _fmt_hold(r.get("hold_s"))
            pf_v = r["pf"]
            pf_str = f"{pf_v:.3f}"
            pf_color = "\033[92m" if pf_v >= 1.2 else ("\033[93m" if pf_v >= 1.0 else "\033[91m")
            wr_v = r["wr_pct"]
            wr = f"{wr_v:.1f}"
            be_wr = r.get("breakeven_wr")
            if be_wr is not None:
                # Override fee if --fee-bps passed
                if args.fee_bps is not None:
                    be_wr = _breakeven_wr(r.get("tp_pct"), r.get("sl_pct"), args.fee_bps) or be_wr
                be_str = f"{be_wr:.0f}%"
                be_color = "\033[92m" if wr_v >= be_wr else "\033[91m"
                be_fmt = f"{be_color}{be_str}\033[0m"
            else:
                be_fmt = "  ?%"
            pnl_v = r["pnl"]
            pnl_str = f"{pnl_v:+.3f}"
            pnl_color = "\033[92m" if pnl_v > 0 else "\033[91m"
            trades = str(r["trades"])
            exp = f"{r['expectancy']:+.4f}"

            print(f"  {i:>2}  {tp:>4} {sl:>5} {score:>5} {hold:>4} "
                  f"{pf_color}{pf_str}\033[0m {wr:>6} "
                  f"{be_fmt:>6} "
                  f"{pnl_color}{pnl_str}\033[0m {trades:>6} {exp:>7}")

        # Pivot table
        if args.pivot:
            _pivot(coin_filtered, args.pivot)
        else:
            # Auto-pivot on the most discriminating params
            print()
            for param in ["hold_s", "score", "tp_pct", "sl_pct"]:
                _pivot([r for r in all_results if r["coin"] == coin], param)

    # Save CSV
    if args.save and results:
        out_path = LOG_DIR / "sweep_analysis.csv"
        fieldnames = ["coin", "tp_pct", "sl_pct", "score", "gate", "hold_s", "fee_bps",
                      "pf", "wr_pct", "breakeven_wr", "pnl", "trades", "max_dd_pct", "expectancy",
                      "run_id", "label"]
        with open(out_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            w.writerows(sorted(results, key=lambda x: x["pf"], reverse=True))
        print(f"\n  Saved: {out_path}")

    print()


if __name__ == "__main__":
    main()