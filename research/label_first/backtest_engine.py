"""Bar-walking backtest engine for label-first rules.

Three exit modes:
- 'oco': fixed target/stop at entry, first touch wins
- 'breakeven': OCO start, but stop moves to entry after price hits +0.5R
- 'trail': stop trails by trail_atr behind highest favorable price after first 0.3 ATR favorable move

All exits are bar-resolution (intraday touches assumed reachable).
Entry = next bar's open after signal bar (realistic).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def atr(df: pd.DataFrame, n: int = 14) -> np.ndarray:
    h, l, c = df["High"].values, df["Low"].values, df["Close"].values
    pc = np.concatenate([[np.nan], c[:-1]])
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    out = np.full_like(tr, np.nan, dtype=float)
    for i in range(n - 1, len(tr)):
        out[i] = np.nanmean(tr[i - n + 1 : i + 1])
    return out


def backtest_long(
    df: pd.DataFrame,
    signal: np.ndarray,
    target_atr: float,
    stop_atr: float,
    hold_bars: int,
    exit_mode: str = "oco",
    trail_atr: float = 0.5,
) -> pd.DataFrame:
    """Walk through bars, for each True signal entry at next bar's open."""
    a_arr = atr(df)
    H = df["High"].values
    L = df["Low"].values
    C = df["Close"].values
    O = df["Open"].values

    trades = []
    sig_idx = np.where(signal)[0]
    for i in sig_idx:
        if i + 1 >= len(df):
            continue
        a = a_arr[i]
        if not np.isfinite(a) or a <= 0:
            continue
        entry = O[i + 1]
        target = entry + target_atr * a
        stop = entry - stop_atr * a
        breakeven_armed = False
        trail_armed = False
        peak = entry
        exit_price = None
        exit_bar = None
        exit_reason = None
        for j in range(i + 1, min(i + 1 + hold_bars, len(df))):
            # Update favorable extreme
            if H[j] > peak:
                peak = H[j]
            # Apply exit-mode adjustments
            if exit_mode == "breakeven" and not breakeven_armed and H[j] >= entry + 0.5 * stop_atr * a + entry * 0:  # +0.5R
                if H[j] >= entry + 0.5 * (target - entry):  # halfway to target
                    stop = max(stop, entry)
                    breakeven_armed = True
            if exit_mode == "trail":
                if not trail_armed and (peak - entry) >= 0.3 * a:
                    trail_armed = True
                if trail_armed:
                    stop = max(stop, peak - trail_atr * a)

            # Check exits — stop first (conservative)
            if L[j] <= stop:
                exit_price = stop
                exit_bar = j
                exit_reason = "stop"
                break
            if H[j] >= target:
                exit_price = target
                exit_bar = j
                exit_reason = "target"
                break
        if exit_price is None:
            exit_bar = min(i + hold_bars, len(df) - 1)
            exit_price = C[exit_bar]
            exit_reason = "time"

        trades.append({
            "entry_idx": i + 1,
            "entry_date": df.index[i + 1],
            "exit_date": df.index[exit_bar],
            "entry": entry,
            "exit": exit_price,
            "atr": a,
            "pnl_atr": (exit_price - entry) / a,
            "exit_reason": exit_reason,
            "bars_held": exit_bar - i,
        })
    return pd.DataFrame(trades)


def stats(trades: pd.DataFrame) -> dict:
    if len(trades) == 0:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "exp": 0.0}
    pnl = trades["pnl_atr"].values
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    gross_w = wins.sum()
    gross_l = abs(losses.sum())
    return {
        "n": len(trades),
        "wr": float((pnl > 0).mean()),
        "pf": float(gross_w / gross_l) if gross_l > 0 else float("inf"),
        "exp": float(pnl.mean()),
        "gross_w": float(gross_w),
        "gross_l": float(gross_l),
    }
