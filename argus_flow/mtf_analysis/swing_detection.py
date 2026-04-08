"""Swing high/low detection and Fibonacci retracement levels."""
from __future__ import annotations

import numpy as np
import pandas as pd


def find_swings(
    df: pd.DataFrame, order: int = 5
) -> tuple[list[dict], list[dict]]:
    """Detect swing highs and swing lows from OHLC data.

    A swing high is a bar whose High is the highest High within *order* bars
    on each side.  A swing low is a bar whose Low is the lowest Low within
    *order* bars on each side.

    Confirmation is **causal** (no look-ahead): the swing at bar ``i`` is only
    confirmed once bar ``i + order`` is available -- i.e. after *order* bars of
    subsequent data confirm that bar ``i`` was indeed the local extremum.
    At any bar ``j``, only data from bars ``<= j`` is used.

    Parameters
    ----------
    df : pd.DataFrame
        OHLC dataframe with datetime index and Open/High/Low/Close columns.
    order : int
        Number of bars on each side required to confirm a swing point.

    Returns
    -------
    (swing_highs, swing_lows) where each element is a list of
    ``{"idx": datetime, "price": float, "bar_index": int}``.
    """
    highs = df["High"].values
    lows = df["Low"].values
    n = len(highs)

    swing_highs: list[dict] = []
    swing_lows: list[dict] = []

    # Iterate j = "current bar" from 2*order onward.  The candidate swing
    # point is at (j - order), the centre of the window [j - 2*order .. j].
    # All bars in the window are <= j, so no future data is used.
    for j in range(2 * order, n):
        candidate = j - order
        window_h = highs[j - 2 * order : j + 1]
        if highs[candidate] == window_h.max() and np.sum(window_h == highs[candidate]) == 1:
            swing_highs.append(
                {"idx": df.index[candidate], "price": float(highs[candidate]), "bar_index": candidate}
            )

        window_l = lows[j - 2 * order : j + 1]
        if lows[candidate] == window_l.min() and np.sum(window_l == lows[candidate]) == 1:
            swing_lows.append(
                {"idx": df.index[candidate], "price": float(lows[candidate]), "bar_index": candidate}
            )

    return swing_highs, swing_lows


# -- Fibonacci ------------------------------------------------------------

_FIB_RATIOS: dict[str, float] = {
    "0.0": 0.0,
    "23.6": 0.236,
    "38.2": 0.382,
    "50.0": 0.500,
    "61.8": 0.618,
    "78.6": 0.786,
    "100.0": 1.0,
}


def compute_fibonacci(swing_high: float, swing_low: float) -> dict[str, float]:
    """Compute Fibonacci retracement levels between a swing high and low.

    Levels are retracements *from high toward low*.  ``"0.0"`` corresponds to
    ``swing_low`` and ``"100.0"`` to ``swing_high``.

    Returns
    -------
    dict mapping level name (e.g. ``"38.2"``) to the price at that level.
    """
    diff = swing_high - swing_low
    return {name: swing_low + ratio * diff for name, ratio in _FIB_RATIOS.items()}


def nearest_fib_level(
    price: float,
    fib_levels: dict[str, float],
    tolerance_pct: float = 0.001,
) -> tuple[str, float, float]:
    """Find the Fibonacci level nearest to *price*.

    Parameters
    ----------
    price : float
        Current market price.
    fib_levels : dict
        Output of :func:`compute_fibonacci`.
    tolerance_pct : float
        Not used for filtering — included for caller convenience.

    Returns
    -------
    (level_name, level_price, distance) where *distance* is the absolute
    difference between *price* and *level_price*.
    """
    best_name = ""
    best_price = 0.0
    best_dist = float("inf")

    for name, lvl in fib_levels.items():
        dist = abs(price - lvl)
        if dist < best_dist:
            best_name, best_price, best_dist = name, lvl, dist

    return best_name, best_price, best_dist
