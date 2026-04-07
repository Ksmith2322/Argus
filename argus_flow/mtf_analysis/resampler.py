"""Incremental OHLCV resampler: 1m bars -> 5m, 30m, 1H, 4H.

Supports bulk seeding from historical DataFrames and incremental
single-bar updates for live streaming.
"""
from __future__ import annotations

import collections
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

# ── Constants ────────────────────────────────────────────────────────
TIMEFRAMES = ("5m", "30m", "1h", "4h")
_TF_MINUTES = {"5m": 5, "30m": 30, "1h": 60, "4h": 240}
_MAX_BARS = 250

_COLS = ["Open", "High", "Low", "Close", "Volume"]


def _floor_ts(dt: datetime, minutes: int) -> datetime:
    """Floor a datetime to the nearest *minutes* boundary (UTC)."""
    ts = int(dt.timestamp())
    floored = ts - (ts % (minutes * 60))
    return datetime.fromtimestamp(floored, tz=timezone.utc)


class _CurrentBar:
    """Mutable accumulator for an in-progress bar."""
    __slots__ = ("ts", "o", "h", "l", "c", "v")

    def __init__(self, ts: datetime, o: float, h: float, l: float, c: float, v: float):
        self.ts = ts
        self.o = o
        self.h = h
        self.l = l
        self.c = c
        self.v = v

    def update(self, h: float, l: float, c: float, v: float) -> None:
        if h > self.h:
            self.h = h
        if l < self.l:
            self.l = l
        self.c = c
        self.v += v

    def as_tuple(self) -> tuple:
        return (self.o, self.h, self.l, self.c, self.v)


class Resampler:
    """Incremental multi-timeframe OHLCV resampler.

    Usage::

        rs = Resampler()
        rs.on_seed(df_1m)              # bulk historical load
        rs.on_bar({"t": dt, "o": ..})  # live 1m bar
        df_5m = rs.get_df("5m")        # completed bars
    """

    def __init__(self) -> None:
        self._bars: dict[str, collections.deque] = {
            tf: collections.deque(maxlen=_MAX_BARS) for tf in TIMEFRAMES
        }
        self._current: dict[str, Optional[_CurrentBar]] = {tf: None for tf in TIMEFRAMES}

    # ── Bulk seed ────────────────────────────────────────────────────

    def on_seed(self, df_1m: pd.DataFrame) -> None:
        """Bulk-load from a historical 1m DataFrame.

        Parameters
        ----------
        df_1m : pd.DataFrame
            Must have a DatetimeIndex (UTC) and columns that map to
            open/high/low/close/volume (case-insensitive first letter).
        """
        df = self._normalise_columns(df_1m)
        if df.empty:
            return

        # Ensure UTC-aware index for proper resampling
        if df.index.tz is None:
            df = df.tz_localize("UTC")

        for tf in TIMEFRAMES:
            rule = tf.upper().replace("M", "min").replace("H", "h")
            # pandas resample rule: 5min, 30min, 1h, 4h
            rule = {"5m": "5min", "30m": "30min", "1h": "1h", "4h": "4h"}[tf]
            resampled = (
                df.resample(rule, label="left", closed="left")
                .agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
                .dropna(subset=["Open"])
            )
            if resampled.empty:
                continue

            # All but the last bar are "completed"; last may be partial
            completed = resampled.iloc[:-1]
            partial = resampled.iloc[-1]

            dq = self._bars[tf]
            for ts, row in completed.iterrows():
                dq.append((ts, row["Open"], row["High"], row["Low"], row["Close"], row["Volume"]))

            # Set the current (incomplete) bar from the last resampled row
            self._current[tf] = _CurrentBar(
                ts=resampled.index[-1],
                o=partial["Open"],
                h=partial["High"],
                l=partial["Low"],
                c=partial["Close"],
                v=partial["Volume"],
            )

    # ── Incremental update ───────────────────────────────────────────

    def on_bar(self, bar: dict) -> None:
        """Process a single 1m bar.

        Parameters
        ----------
        bar : dict
            Keys: t (datetime), o, h, l, c, v.
        """
        t: datetime = bar["t"]
        o, h, l, c, v = bar["o"], bar["h"], bar["l"], bar["c"], bar["v"]

        for tf in TIMEFRAMES:
            minutes = _TF_MINUTES[tf]
            bar_ts = _floor_ts(t, minutes)
            cur = self._current[tf]

            if cur is None or bar_ts > cur.ts:
                # Boundary crossed — finalize previous bar (if any)
                if cur is not None:
                    self._bars[tf].append((cur.ts, *cur.as_tuple()))
                self._current[tf] = _CurrentBar(ts=bar_ts, o=o, h=h, l=l, c=c, v=v)
            else:
                cur.update(h, l, c, v)

    # ── Query ────────────────────────────────────────────────────────

    def get_df(self, timeframe: str) -> pd.DataFrame:
        """Return completed bars for *timeframe* as a DataFrame.

        Columns: Open, High, Low, Close, Volume.  Index: DatetimeIndex (UTC).
        """
        if timeframe not in self._bars:
            raise ValueError(f"Unknown timeframe {timeframe!r}; choose from {TIMEFRAMES}")
        dq = self._bars[timeframe]
        if not dq:
            return pd.DataFrame(columns=_COLS)
        rows = list(dq)
        idx = pd.DatetimeIndex([r[0] for r in rows], name="timestamp")
        return pd.DataFrame(
            [(r[1], r[2], r[3], r[4], r[5]) for r in rows],
            index=idx,
            columns=_COLS,
        )

    def get_current(self, timeframe: str) -> Optional[dict]:
        """Return the current (incomplete) bar for *timeframe*, or None."""
        cur = self._current.get(timeframe)
        if cur is None:
            return None
        return {"ts": cur.ts, "Open": cur.o, "High": cur.h, "Low": cur.l, "Close": cur.c, "Volume": cur.v}

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
        """Map common column names to Open/High/Low/Close/Volume."""
        rename = {}
        lower_map = {c: c.lower() for c in df.columns}
        for target, aliases in [
            ("Open", ("open", "o")),
            ("High", ("high", "h")),
            ("Low", ("low", "l")),
            ("Close", ("close", "c")),
            ("Volume", ("volume", "vol", "v")),
        ]:
            for col, lc in lower_map.items():
                if lc in aliases:
                    rename[col] = target
                    break
        df = df.rename(columns=rename)
        missing = [c for c in _COLS if c not in df.columns]
        if missing:
            raise ValueError(f"Cannot resolve columns: {missing}. Available: {list(df.columns)}")
        return df[_COLS].copy()
