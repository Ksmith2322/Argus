#!/usr/bin/env python3
"""Replay breakout + aggressive-flow hypothesis on prebuilt bars.

Part of Argus Cascade — Phase B (Replay / Edge Proof).
This is research infrastructure. It answers ONE question:

    Does breakout + aggressive delta produce asymmetric post-break moves?

No ML. No book logic. No execution logic. Just measurement.

Usage:
    python -m argus_flow.analytics.replay_breakout_flow \
        --bars data/derived/btc_bars_1s.csv \
        --outdir data/replay/btc_run_001

    # With realistic friction and custom thresholds:
    python -m argus_flow.analytics.replay_breakout_flow \
        --bars data/derived/btc_bars_1s.csv \
        --outdir data/replay/btc_run_002 \
        --compression-threshold 0.0035 \
        --delta-z 1.0 \
        --fee-bps 6 \
        --slippage-bps 8

Outputs:
    bars_with_features.csv  — all bars with computed features
    breakout_events.csv     — detected breakout events with MFE/MAE + session tags
    summary.json            — aggregate stats, expectancy, session breakdown

Kill conditions (from summary.json):
    - false_breakout_rate > 0.65-0.70  -> edge likely dead
    - avg_mae_pct >= avg_mfe_pct       -> no asymmetry
    - expectancy.expectancy_pct <= 0   -> does not survive friction
    - event_count too low              -> not enough signal
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd


Direction = Literal["long", "short"]


# ---------------------------------------------------------------------------
# Config — sane research starting point, NOT sacred values
# ---------------------------------------------------------------------------

@dataclass
class ReplayConfig:
    # Breakout detection
    compression_lookback_bars: int = 30      # bars to measure compression
    compression_threshold_pct: float = 0.0035  # 0.35% range = compressed
    breakout_buffer_pct: float = 0.0005      # 5 bps buffer to avoid touch-breaks

    # Expansion filters
    expansion_lookback_bars: int = 20        # median baseline window
    range_multiplier: float = 1.5            # bar must expand 1.5x median
    volume_multiplier: float = 1.25          # volume must exceed 1.25x median

    # Delta / flow
    delta_norm_lookback_bars: int = 120      # z-score normalization window
    flow_confirm_delta_z: float = 1.0        # min delta z-score for confirmation
    flow_confirm_buy_ratio: float = 0.55     # min buy ratio for long confirmation
    flow_confirm_sell_ratio: float = 0.55    # min sell ratio for short confirmation

    # Replay evaluation
    forward_look_bars: int = 30              # bars to measure MFE/MAE after entry
    lockout_bars: int = 10                   # suppress repeat signals


# ---------------------------------------------------------------------------
# Bar loading
# ---------------------------------------------------------------------------

REQUIRED_BAR_COLUMNS = {
    "ts", "open", "high", "low", "close",
    "volume", "buy_volume", "sell_volume",
    "delta", "trade_count", "bar_range", "delta_ratio",
}


def load_bars(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)

    missing = REQUIRED_BAR_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(f"Missing required bar columns: {sorted(missing)}")

    df = df.copy()
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")

    numeric_cols = [
        "open", "high", "low", "close", "volume",
        "buy_volume", "sell_volume", "delta",
        "trade_count", "bar_range", "delta_ratio",
    ]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["ts", "open", "high", "low", "close"])
    df = df.sort_values("ts").reset_index(drop=True)

    if df.empty:
        raise ValueError("No valid bars remain after cleaning.")

    return df


# ---------------------------------------------------------------------------
# Feature computation
# ---------------------------------------------------------------------------

def add_features(df: pd.DataFrame, cfg: ReplayConfig) -> pd.DataFrame:
    out = df.copy()

    n = cfg.compression_lookback_bars
    m = cfg.expansion_lookback_bars
    k = cfg.delta_norm_lookback_bars

    # --- Structure ---
    # Compression window: rolling high/low EXCLUDING current bar (shift 1)
    out["rolling_high"] = out["high"].shift(1).rolling(n, min_periods=n).max()
    out["rolling_low"] = out["low"].shift(1).rolling(n, min_periods=n).min()
    out["compression_range"] = out["rolling_high"] - out["rolling_low"]
    out["compression_pct"] = np.where(
        out["close"] > 0,
        out["compression_range"] / out["close"],
        np.nan,
    )

    # Expansion baselines
    out["range_median"] = out["bar_range"].shift(1).rolling(m, min_periods=m).median()
    out["volume_median"] = out["volume"].shift(1).rolling(m, min_periods=m).median()

    # --- Flow ---
    # Rolling deltas at multiple horizons
    out["delta_5s"] = out["delta"].rolling(5, min_periods=5).sum()
    out["delta_15s"] = out["delta"].rolling(15, min_periods=15).sum()
    out["buy_volume_5s"] = out["buy_volume"].rolling(5, min_periods=5).sum()
    out["sell_volume_5s"] = out["sell_volume"].rolling(5, min_periods=5).sum()
    out["volume_5s"] = out["volume"].rolling(5, min_periods=5).sum()

    # Buy/sell ratios
    out["buy_ratio_5s"] = np.where(
        out["volume_5s"] > 0,
        out["buy_volume_5s"] / out["volume_5s"],
        0.0,
    )
    out["sell_ratio_5s"] = np.where(
        out["volume_5s"] > 0,
        out["sell_volume_5s"] / out["volume_5s"],
        0.0,
    )

    # Delta z-score normalization (rolling, excludes current bar)
    delta_mean = out["delta_5s"].shift(1).rolling(k, min_periods=k).mean()
    delta_std = out["delta_5s"].shift(1).rolling(k, min_periods=k).std(ddof=0)
    out["delta_z_5s"] = np.where(
        (delta_std > 0) & (~delta_std.isna()),
        (out["delta_5s"] - delta_mean) / delta_std,
        np.nan,
    )

    # Volume z-score
    vol_mean = out["volume_5s"].shift(1).rolling(k, min_periods=k).mean()
    vol_std = out["volume_5s"].shift(1).rolling(k, min_periods=k).std(ddof=0)
    out["volume_z_5s"] = np.where(
        (vol_std > 0) & (~vol_std.isna()),
        (out["volume_5s"] - vol_mean) / vol_std,
        np.nan,
    )

    # --- Composite filters ---
    out["compression_ok"] = out["compression_pct"] <= cfg.compression_threshold_pct
    out["range_ok"] = out["bar_range"] >= (out["range_median"] * cfg.range_multiplier)
    out["volume_ok"] = out["volume"] >= (out["volume_median"] * cfg.volume_multiplier)

    out["breakout_buffer"] = out["close"] * cfg.breakout_buffer_pct
    out["long_break"] = out["close"] > (out["rolling_high"] + out["breakout_buffer"])
    out["short_break"] = out["close"] < (out["rolling_low"] - out["breakout_buffer"])

    # Flow confirmation (both z-score AND ratio must pass)
    out["long_flow_ok"] = (
        (out["delta_z_5s"] >= cfg.flow_confirm_delta_z)
        & (out["buy_ratio_5s"] >= cfg.flow_confirm_buy_ratio)
    )
    out["short_flow_ok"] = (
        (out["delta_z_5s"] <= -cfg.flow_confirm_delta_z)
        & (out["sell_ratio_5s"] >= cfg.flow_confirm_sell_ratio)
    )

    return out


# ---------------------------------------------------------------------------
# Forward excursion measurement
# ---------------------------------------------------------------------------

def compute_forward_excursions(
    df: pd.DataFrame,
    entry_idx: int,
    direction: Direction,
    forward_look_bars: int,
) -> dict:
    entry_px = float(df.at[entry_idx, "close"])
    end_idx = min(entry_idx + forward_look_bars, len(df) - 1)
    window = df.iloc[entry_idx + 1 : end_idx + 1]

    if window.empty:
        return {
            "entry_px": entry_px,
            "mfe_abs": 0.0,
            "mae_abs": 0.0,
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "time_to_mfe_bars": 0,
            "time_to_mae_bars": 0,
        }

    if direction == "long":
        favorable = window["high"] - entry_px
        adverse = entry_px - window["low"]
    else:
        favorable = entry_px - window["low"]
        adverse = window["high"] - entry_px

    mfe_abs = float(favorable.max())
    mae_abs = float(adverse.max())

    mfe_idx = favorable.idxmax()
    mae_idx = adverse.idxmax()

    return {
        "entry_px": entry_px,
        "mfe_abs": mfe_abs,
        "mae_abs": mae_abs,
        "mfe_pct": mfe_abs / entry_px if entry_px else 0.0,
        "mae_pct": mae_abs / entry_px if entry_px else 0.0,
        "time_to_mfe_bars": int(mfe_idx - entry_idx),
        "time_to_mae_bars": int(mae_idx - entry_idx),
    }


# ---------------------------------------------------------------------------
# Breakout detection (one-shot event rule with lockout)
# ---------------------------------------------------------------------------

def detect_events(df: pd.DataFrame, cfg: ReplayConfig) -> pd.DataFrame:
    records: list[dict] = []
    next_allowed_long = -1
    next_allowed_short = -1

    for i in range(len(df)):
        row = df.iloc[i]

        # All common filters must pass
        common_ok = bool(
            row["compression_ok"]
            and row["range_ok"]
            and row["volume_ok"]
            and not pd.isna(row["rolling_high"])
            and not pd.isna(row["rolling_low"])
        )
        if not common_ok:
            continue

        # Long breakout
        if i >= next_allowed_long and bool(row["long_break"] and row["long_flow_ok"]):
            forward = compute_forward_excursions(
                df=df,
                entry_idx=i,
                direction="long",
                forward_look_bars=cfg.forward_look_bars,
            )
            records.append(
                {
                    "ts": row["ts"],
                    "direction": "long",
                    "entry_idx": i,
                    "level_px": float(row["rolling_high"]),
                    "compression_pct": float(row["compression_pct"]),
                    "delta_5s": float(row["delta_5s"]),
                    "delta_z_5s": float(row["delta_z_5s"]),
                    "buy_ratio_5s": float(row["buy_ratio_5s"]),
                    "volume_z_5s": float(row["volume_z_5s"])
                    if not pd.isna(row["volume_z_5s"])
                    else np.nan,
                    **forward,
                }
            )
            next_allowed_long = i + cfg.lockout_bars

        # Short breakout
        if i >= next_allowed_short and bool(row["short_break"] and row["short_flow_ok"]):
            forward = compute_forward_excursions(
                df=df,
                entry_idx=i,
                direction="short",
                forward_look_bars=cfg.forward_look_bars,
            )
            records.append(
                {
                    "ts": row["ts"],
                    "direction": "short",
                    "entry_idx": i,
                    "level_px": float(row["rolling_low"]),
                    "compression_pct": float(row["compression_pct"]),
                    "delta_5s": float(row["delta_5s"]),
                    "delta_z_5s": float(row["delta_z_5s"]),
                    "sell_ratio_5s": float(row["sell_ratio_5s"]),
                    "volume_z_5s": float(row["volume_z_5s"])
                    if not pd.isna(row["volume_z_5s"])
                    else np.nan,
                    **forward,
                }
            )
            next_allowed_short = i + cfg.lockout_bars

    events = pd.DataFrame(records)
    if events.empty:
        return events

    # Derived metrics
    events["mfe_mae_ratio"] = np.where(
        events["mae_pct"] > 0,
        events["mfe_pct"] / events["mae_pct"],
        np.nan,
    )
    events["false_breakout"] = events["mfe_pct"] < events["mae_pct"]

    return events


# ---------------------------------------------------------------------------
# Time / session features
# ---------------------------------------------------------------------------

def add_time_features(events: pd.DataFrame) -> pd.DataFrame:
    df = events.copy()
    df["hour"] = df["ts"].dt.hour

    # Rough session buckets (UTC)
    df["session"] = np.select(
        [
            df["hour"].between(0, 7),    # Asia
            df["hour"].between(8, 15),   # Europe
            df["hour"].between(16, 23),  # US
        ],
        ["asia", "europe", "us"],
        default="unknown",
    )
    return df


# ---------------------------------------------------------------------------
# Expectancy with friction
# ---------------------------------------------------------------------------

def compute_expectancy(events: pd.DataFrame, fee_bps: float, slippage_bps: float) -> dict:
    if events.empty:
        return {}

    # Round-trip cost (entry + exit)
    total_cost = (fee_bps + slippage_bps) * 2 / 10000.0

    wins_mask = events["mfe_pct"] > events["mae_pct"]
    win_rate = float(wins_mask.mean())
    loss_rate = 1.0 - win_rate

    avg_win = float(events.loc[wins_mask, "mfe_pct"].mean()) if wins_mask.any() else 0.0
    avg_loss = float(events.loc[~wins_mask, "mae_pct"].mean()) if (~wins_mask).any() else 0.0

    expectancy = (avg_win * win_rate) - (avg_loss * loss_rate) - total_cost

    return {
        "fee_bps": fee_bps,
        "slippage_bps": slippage_bps,
        "total_round_trip_cost_pct": total_cost,
        "avg_win_pct": avg_win,
        "avg_loss_pct": avg_loss,
        "win_rate": win_rate,
        "loss_rate": loss_rate,
        "expectancy_pct": expectancy,
        "expectancy_positive": expectancy > 0,
    }


# ---------------------------------------------------------------------------
# Session breakdown
# ---------------------------------------------------------------------------

def session_breakdown(events: pd.DataFrame) -> dict:
    if events.empty or "session" not in events.columns:
        return {}

    result = {}
    for session, group in events.groupby("session"):
        result[session] = {
            "count": int(len(group)),
            "avg_mfe_pct": float(group["mfe_pct"].mean()),
            "avg_mae_pct": float(group["mae_pct"].mean()),
            "false_breakout_rate": float(
                (group["mfe_pct"] < group["mae_pct"]).mean()
            ),
            "avg_mfe_mae_ratio": float(
                group["mfe_mae_ratio"]
                .replace([np.inf, -np.inf], np.nan)
                .mean()
            ),
        }
    return result


# ---------------------------------------------------------------------------
# Summary report
# ---------------------------------------------------------------------------

def build_summary(
    events: pd.DataFrame,
    fee_bps: float,
    slippage_bps: float,
) -> dict:
    if events.empty:
        return {
            "event_count": 0,
            "long_count": 0,
            "short_count": 0,
        }

    summary = {
        "event_count": int(len(events)),
        "long_count": int((events["direction"] == "long").sum()),
        "short_count": int((events["direction"] == "short").sum()),
        "avg_mfe_pct": float(events["mfe_pct"].mean()),
        "avg_mae_pct": float(events["mae_pct"].mean()),
        "median_mfe_pct": float(events["mfe_pct"].median()),
        "median_mae_pct": float(events["mae_pct"].median()),
        "false_breakout_rate": float(events["false_breakout"].mean()),
        "avg_mfe_mae_ratio": float(
            events["mfe_mae_ratio"].replace([np.inf, -np.inf], np.nan).mean()
        ),
        "pct_mfe_gt_2x_mae": float(
            (events["mfe_pct"] > (2 * events["mae_pct"])).mean()
        ),
        "pct_mfe_gt_1pct": float((events["mfe_pct"] > 0.01).mean()),
    }

    # Per-direction breakdown
    for direction in ["long", "short"]:
        subset = events[events["direction"] == direction]
        if subset.empty:
            continue
        summary[f"{direction}_count"] = int(len(subset))
        summary[f"{direction}_avg_mfe_pct"] = float(subset["mfe_pct"].mean())
        summary[f"{direction}_avg_mae_pct"] = float(subset["mae_pct"].mean())
        summary[f"{direction}_false_breakout_rate"] = float(
            subset["false_breakout"].mean()
        )

    # Expectancy and session breakdown
    summary["expectancy"] = compute_expectancy(events, fee_bps, slippage_bps)
    summary["session_breakdown"] = session_breakdown(events)

    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay breakout + aggressive-flow hypothesis on prebuilt bars."
    )
    parser.add_argument(
        "--bars",
        required=True,
        help="Path to bars CSV (output of build_bars_from_trades).",
    )
    parser.add_argument(
        "--outdir",
        required=True,
        help="Directory for replay outputs.",
    )

    # Tunable thresholds (for robustness testing, not optimization)
    parser.add_argument(
        "--compression-threshold", type=float, default=None,
        help="Override compression_threshold_pct (default: 0.0035).",
    )
    parser.add_argument(
        "--breakout-buffer", type=float, default=None,
        help="Override breakout_buffer_pct (default: 0.0005).",
    )
    parser.add_argument(
        "--delta-z", type=float, default=None,
        help="Override flow_confirm_delta_z (default: 1.0).",
    )
    parser.add_argument(
        "--forward-bars", type=int, default=None,
        help="Override forward_look_bars (default: 30).",
    )

    # Friction
    parser.add_argument(
        "--fee-bps", type=float, default=5.0,
        help="One-way fee in basis points (default: 5).",
    )
    parser.add_argument(
        "--slippage-bps", type=float, default=5.0,
        help="One-way slippage in basis points (default: 5).",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    bars_path = Path(args.bars)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    cfg = ReplayConfig()

    # Apply CLI overrides
    if args.compression_threshold is not None:
        cfg.compression_threshold_pct = args.compression_threshold
    if args.breakout_buffer is not None:
        cfg.breakout_buffer_pct = args.breakout_buffer
    if args.delta_z is not None:
        cfg.flow_confirm_delta_z = args.delta_z
    if args.forward_bars is not None:
        cfg.forward_look_bars = args.forward_bars

    bars = load_bars(bars_path)
    bars_with_features = add_features(bars, cfg)
    events = detect_events(bars_with_features, cfg)

    # Add time/session tags before summary
    if not events.empty:
        events = add_time_features(events)

    summary = build_summary(events, args.fee_bps, args.slippage_bps)

    features_path = outdir / "bars_with_features.csv"
    events_path = outdir / "breakout_events.csv"
    summary_path = outdir / "summary.json"

    bars_with_features.to_csv(features_path, index=False)
    events.to_csv(events_path, index=False)
    summary_path.write_text(json.dumps(summary, indent=2))

    print(f"Loaded bars:   {len(bars):,}")
    print(f"Signal events: {len(events):,}")
    print(f"Saved: {features_path}")
    print(f"Saved: {events_path}")
    print(f"Saved: {summary_path}")
    print()
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()