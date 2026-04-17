"""Label generators for label-first research.

For each bar B, label as LONG_ENTRY (or SHORT_ENTRY) if a forward window
exhibits the targeted move profile WITHOUT exceeding the adverse-excursion
budget. ATR-normalized so labels are comparable across timeframes/instruments.

NO LOOKAHEAD beyond the explicit forward window — features are computed only
from data at or before bar B; labels are the answer key, not features.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# (forward_bars, min_gain_atr, max_dd_atr)
LABEL_VARIANTS = {
    "L1_quick":      (4,  0.5, 0.20),
    "L2_modest":     (8,  1.0, 0.40),
    "L3_big":        (20, 2.0, 0.50),
    "L4_asym_tight": (12, 1.5, 0.30),
}


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def label_entries(df: pd.DataFrame, variant: str) -> pd.DataFrame:
    """Returns DataFrame with columns: long_label, short_label (booleans).

    Bar B is labeled LONG if max(High[B+1..B+N]) - Close[B] >= min_gain*ATR
    AND min(Low[B+1..B+N]) - Close[B] >= -max_dd*ATR (drawdown stayed within budget).
    """
    fwd_n, min_gain, max_dd = LABEL_VARIANTS[variant]
    atr = _atr(df).values
    high = df["High"].values
    low = df["Low"].values
    close = df["Close"].values
    n_bars = len(df)

    long_label = np.zeros(n_bars, dtype=bool)
    short_label = np.zeros(n_bars, dtype=bool)

    for i in range(n_bars - fwd_n):
        a = atr[i]
        if not np.isfinite(a) or a <= 0:
            continue
        c = close[i]
        fwd_h = high[i + 1 : i + 1 + fwd_n]
        fwd_l = low[i + 1 : i + 1 + fwd_n]
        # LONG side
        max_up = fwd_h.max() - c
        max_dn = c - fwd_l.min()  # adverse excursion for a long
        if max_up >= min_gain * a and max_dn <= max_dd * a:
            long_label[i] = True
        # SHORT side (mirror)
        max_dn_short = c - fwd_l.min()  # gain for a short
        max_up_short = fwd_h.max() - c  # adverse excursion for a short
        if max_dn_short >= min_gain * a and max_up_short <= max_dd * a:
            short_label[i] = True

    return pd.DataFrame(
        {"long_label": long_label, "short_label": short_label},
        index=df.index,
    )


def label_all_variants(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    return {v: label_entries(df, v) for v in LABEL_VARIANTS}
