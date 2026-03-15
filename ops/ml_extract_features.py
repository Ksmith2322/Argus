#!/usr/bin/env python3
"""ops/ml_extract_features.py -- Extract per-trade ML feature vectors from backtest artifacts.

For each completed backtest run, joins trade outcomes with the signal state at entry time
to produce a unified feature dataset for ML training.

Usage:
    python ops/ml_extract_features.py                   # extract all runs, save to data/ml_trades.csv
    python ops/ml_extract_features.py --min-trades 10   # skip runs with < 10 trades
    python ops/ml_extract_features.py --output data/ml_trades.parquet  # parquet output
    python ops/ml_extract_features.py --run-id bt_20260314T163058Z_b713d91e  # single run
"""
import csv
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOGS = REPO / "ops" / "logs"

# Features to extract from signals CSV at entry epoch
SIGNAL_FEATURES = [
    # Technical
    "ma_fast", "ma_slow", "ma50", "ma200",
    "signal", "trend_ok", "score",
    "score_5m", "score_1h",
    "dist_ma200_pct",
    # Confluence
    "confluence_score", "confluence_gate",
    "ac_base_score", "ac_adjusted_score", "ac_delta",
    # Volume & Liquidity
    "candle_volume_1m", "vol", "vol_used",
    "liq_ok", "liq_spread_bps", "liq_vol_1m", "liq_vol_baseline",
    "liq_atr_norm", "liq_penalty_points",
    # Regime & Session
    "regime", "trend_strength",
    "session", "session_bonus_points", "session_risk_mult",
    # Structure
    "nearest_support", "nearest_resistance",
    "dist_support", "dist_resistance",
    "near_support", "near_resistance",
    # Portfolio state at entry
    "cash_usd", "equity_usd", "realized_pnl_usd",
    # Risk
    "cooldown_remaining_s",
]

# Trade outcome fields from trades CSV
TRADE_OUTCOME_FIELDS = [
    "entry_epoch", "exit_epoch", "duration_s",
    "entry_px", "exit_px", "qty",
    "realized_usd", "realized_return_pct",
    "mfe_pct_points", "mae_pct_points",
]


def _safe_float(v, default=None):
    if v is None or v == "" or v == "None" or v == "N/A":
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _find_signal_at_epoch(signals_path: Path, target_epoch: int) -> dict:
    """Find the signal row closest to the target epoch."""
    if not signals_path.exists():
        return {}
    best_row = {}
    best_diff = float("inf")
    with open(signals_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                epoch = int(row.get("epoch", 0))
            except (ValueError, TypeError):
                continue
            diff = abs(epoch - target_epoch)
            if diff < best_diff:
                best_diff = diff
                best_row = dict(row)
            # Early exit if we've passed the target
            if epoch > target_epoch + 120:
                break
    return best_row


def _extract_run_features(run_id: str, min_trades: int = 0) -> list[dict]:
    """Extract feature vectors for all trades in a run."""
    # Find artifacts
    summary_path = LOGS / f"bt_summary_{run_id}.json"
    if not summary_path.exists():
        return []

    summary = json.load(open(summary_path))
    trades_closed = int(summary.get("trades_closed", 0) or 0)
    if trades_closed < min_trades:
        return []

    # Find trades CSV
    trades_path = LOGS / f"trades_{run_id}.csv"
    if not trades_path.exists():
        return []

    # Find signals CSV
    signals_path = LOGS / f"bt_signals_{run_id}.csv"

    # Find run header for label
    header_path = LOGS / f"run_header_{run_id}.json"
    label = ""
    config_hash = ""
    if header_path.exists():
        hdr = json.load(open(header_path))
        label = hdr.get("label", "")
        config_hash = hdr.get("config_hash", "")

    # Read all trades
    trades = []
    with open(trades_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("exit_epoch"):
                continue  # skip open trades
            trades.append(row)

    if not trades:
        return []

    # Pre-load signals index for faster lookup
    signal_index = {}
    if signals_path.exists():
        with open(signals_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    epoch = int(row.get("epoch", 0))
                    signal_index[epoch] = row
                except (ValueError, TypeError):
                    continue

    results = []
    for trade in trades:
        entry_epoch = int(trade.get("entry_epoch", 0))
        exit_epoch = int(trade.get("exit_epoch", 0))
        realized_usd = _safe_float(trade.get("realized_usd"), 0.0)

        # Build feature vector
        feat = {
            "run_id": run_id,
            "label": label,
            "config_hash": config_hash,
            "symbol": trade.get("symbol", "ETH-USD"),
        }

        # Trade outcomes
        for field in TRADE_OUTCOME_FIELDS:
            feat[f"trade_{field}"] = _safe_float(trade.get(field))

        # Derived outcome features
        feat["win"] = 1 if realized_usd > 0 else 0
        feat["trade_pnl_usd"] = realized_usd

        # Duration buckets
        dur_s = _safe_float(trade.get("duration_s"), 0)
        if dur_s is not None:
            if dur_s < 1800:
                feat["duration_bucket"] = "0-30m"
            elif dur_s < 5400:
                feat["duration_bucket"] = "30m-90m"
            elif dur_s < 10800:
                feat["duration_bucket"] = "90m-3h"
            else:
                feat["duration_bucket"] = "3h+"
        else:
            feat["duration_bucket"] = "unknown"

        # Signal features at entry
        sig = signal_index.get(entry_epoch, {})
        if not sig:
            # Try nearby epochs (within 60s)
            for offset in range(1, 61):
                sig = signal_index.get(entry_epoch + offset, {})
                if sig:
                    break
                sig = signal_index.get(entry_epoch - offset, {})
                if sig:
                    break

        for field in SIGNAL_FEATURES:
            val = sig.get(field, "")
            feat[f"sig_{field}"] = _safe_float(val) if field not in (
                "confluence_gate", "regime", "session", "liq_ok",
                "trend_ok", "near_support", "near_resistance"
            ) else val

        # Derived signal features
        if sig:
            ma_fast = _safe_float(sig.get("ma_fast"))
            ma_slow = _safe_float(sig.get("ma_slow"))
            ma200 = _safe_float(sig.get("ma200"))
            px = _safe_float(sig.get("px") or sig.get("price"))

            if ma_fast and ma_slow and ma_slow != 0:
                feat["sig_ma_cross_ratio"] = ma_fast / ma_slow
            if px and ma200 and ma200 != 0:
                feat["sig_px_vs_ma200_pct"] = (px - ma200) / ma200 * 100

            # Time features from epoch
            from datetime import datetime, timezone
            try:
                dt = datetime.fromtimestamp(entry_epoch, tz=timezone.utc)
                feat["entry_hour_utc"] = dt.hour
                feat["entry_dow"] = dt.weekday()  # 0=Mon, 6=Sun
            except Exception:
                pass

        results.append(feat)

    return results


def main():
    args = sys.argv[1:]
    min_trades = 0
    output_path = str(REPO / "data" / "ml_trades.csv")
    target_run = None

    i = 0
    while i < len(args):
        if args[i] == "--min-trades" and i + 1 < len(args):
            min_trades = int(args[i + 1])
            i += 2
        elif args[i] == "--output" and i + 1 < len(args):
            output_path = args[i + 1]
            i += 2
        elif args[i] == "--run-id" and i + 1 < len(args):
            target_run = args[i + 1]
            i += 2
        else:
            i += 1

    # Find all completed runs
    if target_run:
        run_ids = [target_run]
    else:
        run_ids = []
        for p in sorted(LOGS.glob("bt_summary_bt_*.json")):
            if "latest" in p.name:
                continue
            try:
                d = json.load(open(p))
                rid = d.get("run_id", "")
                if rid:
                    run_ids.append(rid)
            except Exception:
                continue

    print(f"Processing {len(run_ids)} runs (min_trades={min_trades})...")

    all_features = []
    for rid in run_ids:
        feats = _extract_run_features(rid, min_trades)
        if feats:
            all_features.extend(feats)
            print(f"  {rid}: {len(feats)} trades extracted")

    if not all_features:
        print("No trades extracted.")
        return

    # Determine all columns
    all_cols = []
    seen = set()
    for feat in all_features:
        for k in feat:
            if k not in seen:
                all_cols.append(k)
                seen.add(k)

    # Write CSV
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=all_cols)
        writer.writeheader()
        writer.writerows(all_features)

    print(f"\nExtracted {len(all_features)} trades from {len(run_ids)} runs")
    print(f"Features per trade: {len(all_cols)}")
    print(f"Output: {output_path}")

    # Quick stats
    wins = sum(1 for f in all_features if f.get("win") == 1)
    total = len(all_features)
    print(f"Win rate: {wins}/{total} ({wins/total*100:.1f}%)")

    # Feature completeness
    missing = {}
    for col in all_cols:
        n_missing = sum(1 for f in all_features if f.get(col) is None or f.get(col) == "")
        if n_missing > 0:
            missing[col] = n_missing
    if missing:
        print(f"\nFeatures with missing values ({len(missing)}):")
        for col, n in sorted(missing.items(), key=lambda x: -x[1])[:10]:
            print(f"  {col}: {n}/{total} missing ({n/total*100:.0f}%)")


if __name__ == "__main__":
    main()