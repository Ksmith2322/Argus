"""Long-only single-position backtest engine for the factory.

A strategy is a JSON-serializable dict — a "spec" — that the engine
compiles into a sequence of entry/exit signals on a bar DataFrame.

## Spec format (v1)

```json
{
    "name": "sma_cross_50_200",
    "timeframe": "1d",
    "entry": {
        "kind": "sma_cross",
        "fast": 20,
        "slow": 50
    },
    "exit": {
        "kind": "atr_stop_target",
        "atr_n": 14,
        "stop_mult": 2.0,
        "target_mult": 4.0,
        "max_hold_bars": 60
    }
}
```

## Supported entry kinds

- `sma_cross` — fast SMA crosses above slow SMA (golden cross)
- `ema_cross` — fast EMA crosses above slow EMA
- `rsi_oversold` — RSI(n) crosses up through `threshold` (default 30)
- `donchian_breakout` — Close > shift(1) of donchian upper(n)
- `bollinger_revert` — Close was below lower band, now above mid
- `macd_cross` — MACD histogram crosses above 0 (bullish signal cross)
- `zscore_revert` — close was at z<=lower_thresh, now above z=mid_thresh

## Regime filter

An optional `regime_filter` block on the spec gates EVERY entry with an
additional bar-level boolean (same schema as `entry`). The entry signal
fires only on bars where the filter is also True. Use this to add a
trend gate ("only long when close > 200-SMA") or a volatility gate.

Example:
    {"entry": {"kind": "donchian_breakout", "n": 20},
     "regime_filter": {"kind": "above_sma", "n": 200}}

## Supported exit kinds

- `atr_stop_target` — fixed ATR-based stop + target, plus `max_hold_bars`
- `hold_period` — exit after fixed N bars
- `signal_exit` — exit when an "exit signal" fires (e.g. opposite cross);
  the exit dict re-uses the entry-kind schema but with `inverse=true`

Position sizing is 1 unit per signal — the factory screens edge, not
risk-adjusted equity. Sizing is the auto-allocator's job downstream.

No look-ahead: entry decision uses bar i's close (assumes signal fires
on close, fill happens at next bar's open). For v1 simplicity we model
entry AT bar i's close — same convention as the existing per-strategy
backtesters in this repo (poc_backtest, strategies_backtest).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd

from helio.backtest_factory import primitives as P
from helio.backtest_factory.metrics import compute_metrics


REQUIRED_BAR_COLS = ("Open", "High", "Low", "Close")


@dataclass
class StrategySpec:
    """Lightweight typed wrapper around the spec dict. Use `from_dict` to
    validate; the engine accepts either StrategySpec or raw dict."""
    name: str
    entry: Dict[str, Any]
    exit: Dict[str, Any]
    timeframe: str = "1d"
    extras: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StrategySpec":
        if "name" not in d or "entry" not in d or "exit" not in d:
            raise ValueError(f"spec missing required keys (name/entry/exit): keys={list(d.keys())}")
        extras = {k: v for k, v in d.items() if k not in ("name", "entry", "exit", "timeframe")}
        return cls(
            name=str(d["name"]),
            entry=dict(d["entry"]),
            exit=dict(d["exit"]),
            timeframe=str(d.get("timeframe", "1d")),
            extras=extras,
        )

    def to_dict(self) -> Dict[str, Any]:
        out = {"name": self.name, "timeframe": self.timeframe,
               "entry": dict(self.entry), "exit": dict(self.exit)}
        out.update(self.extras)
        return out


@dataclass
class BacktestResult:
    spec_name: str
    ticker: str
    trades: List[Dict]
    metrics: Dict
    bars_used: int
    elapsed_ms: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "spec_name": self.spec_name,
            "ticker": self.ticker,
            "n_trades": len(self.trades),
            "metrics": self.metrics,
            "bars_used": self.bars_used,
            "elapsed_ms": round(self.elapsed_ms, 2),
        }


# ─── entry signal compilers ───────────────────────────────────────────

def _entry_signal(df: pd.DataFrame, cfg: Dict[str, Any]) -> pd.Series:
    """Return a bool Series. True = enter at THIS bar's close (long).

    Also handles two non-entry "kinds" used for regime filtering:
        - `above_sma`: close > SMA(n) — trend regime gate
        - `above_ema`: close > EMA(n) — same idea, EMA flavor
    """
    kind = cfg.get("kind")
    if kind == "sma_cross":
        fast = P.sma(df["Close"], int(cfg["fast"]))
        slow = P.sma(df["Close"], int(cfg["slow"]))
        return P.cross_above(fast, slow)
    if kind == "ema_cross":
        fast = P.ema(df["Close"], int(cfg["fast"]))
        slow = P.ema(df["Close"], int(cfg["slow"]))
        return P.cross_above(fast, slow)
    if kind == "rsi_oversold":
        n = int(cfg.get("n", 14))
        thresh = float(cfg.get("threshold", 30.0))
        r = P.rsi(df["Close"], n)
        return P.cross_above(r, thresh)
    if kind == "donchian_breakout":
        n = int(cfg["n"])
        upper, _ = P.donchian(df["High"], df["Low"], n)
        # No look-ahead: compare close to PRIOR bar's donchian upper
        upper_prev = upper.shift(1)
        return (df["Close"] > upper_prev).fillna(False)
    if kind == "bollinger_revert":
        n = int(cfg.get("n", 20))
        k = float(cfg.get("k", 2.0))
        mid, _, lower = P.bollinger(df["Close"], n, k)
        # Was below lower band on prior bar AND now above mid
        was_low = (df["Close"].shift(1) < lower.shift(1)).fillna(False)
        above_mid = (df["Close"] > mid).fillna(False)
        return was_low & above_mid
    if kind == "macd_cross":
        fast = int(cfg.get("fast", 12))
        slow = int(cfg.get("slow", 26))
        signal_n = int(cfg.get("signal", 9))
        _, _, hist = P.macd(df["Close"], fast, slow, signal_n)
        # Bullish cross: histogram crosses up through 0
        return P.cross_above(hist, 0.0)
    if kind == "zscore_revert":
        n = int(cfg.get("n", 20))
        lower_thresh = float(cfg.get("lower_thresh", -2.0))
        mid_thresh = float(cfg.get("mid_thresh", 0.0))
        z = P.zscore(df["Close"], n)
        was_low = (z.shift(1) <= lower_thresh).fillna(False)
        above_mid = (z > mid_thresh).fillna(False)
        return was_low & above_mid
    if kind == "above_sma":
        # Regime gate — not an entry trigger by itself, but a filter.
        n = int(cfg["n"])
        return (df["Close"] > P.sma(df["Close"], n)).fillna(False)
    if kind == "above_ema":
        n = int(cfg["n"])
        return (df["Close"] > P.ema(df["Close"], n)).fillna(False)
    if kind == "volume_z_above":
        # Filter: volume's rolling z-score > threshold (volume surge)
        if "Volume" not in df.columns:
            raise ValueError("volume_z_above filter requires Volume column")
        n = int(cfg.get("n", 20))
        thresh = float(cfg.get("threshold", 1.5))
        z = P.volume_zscore(df["Volume"], n)
        return (z > thresh).fillna(False)
    if kind == "obv_above_sma":
        # Filter: OBV above its own N-period SMA (volume-trend confirmation)
        if "Volume" not in df.columns:
            raise ValueError("obv_above_sma filter requires Volume column")
        n = int(cfg.get("n", 20))
        o = P.obv(df["Close"], df["Volume"])
        sma_o = o.rolling(window=n, min_periods=n).mean()
        return (o > sma_o).fillna(False)
    if kind == "before_event":
        # Calendar entry: fires when the next named-event date is between
        # `n_min` and `n_max` trading bars away. Used for "buy N days
        # before FOMC" type strategies.
        from helio.backtest_factory.calendar_data import (
            bars_until_next_event, get_event_dates,
        )
        event_set = str(cfg["event_set"])
        n_min = int(cfg.get("n_min", 1))
        n_max = int(cfg.get("n_max", 5))
        dates = get_event_dates(event_set)
        bars_until = bars_until_next_event(df.index, dates)
        return ((bars_until >= n_min) & (bars_until <= n_max)).fillna(False)
    if kind == "after_event":
        # Calendar entry: fires N trading bars AFTER an event. We compute
        # this by checking if the most-recent event was n_min..n_max bars
        # ago. Implementation: for each bar, look at "bars since last
        # event" — derived by inverting bars_until_next_event over the
        # event list shifted by 1 day.
        from helio.backtest_factory.calendar_data import get_event_dates
        event_set = str(cfg["event_set"])
        n_min = int(cfg.get("n_min", 1))
        n_max = int(cfg.get("n_max", 5))
        dates = sorted(pd.Timestamp(d) for d in get_event_dates(event_set))
        # For each bar, find days since most-recent event date
        idx = df.index
        if isinstance(idx, pd.DatetimeIndex) and idx.tz is not None:
            idx = idx.tz_localize(None)
        out = []
        # event_positions: position in idx of each event (or just-after)
        event_positions = [int(idx.searchsorted(ev, side="left")) for ev in dates]
        event_positions = [p for p in event_positions if p < len(idx)]
        for i in range(len(idx)):
            prior = [p for p in event_positions if p < i]
            if not prior:
                out.append(False)
                continue
            bars_since = i - prior[-1]
            out.append(n_min <= bars_since <= n_max)
        return pd.Series(out, index=df.index, dtype=bool)
    raise ValueError(f"unknown entry kind: {kind!r}")


# ─── exit logic ───────────────────────────────────────────────────────

def _resolve_exit(
    pos: Dict[str, Any],
    row: pd.Series,
    bar_idx: int,
    cfg: Dict[str, Any],
    df: pd.DataFrame,
) -> Optional[Dict[str, Any]]:
    """Return {exit_reason, exit_price} or None if position survives bar.

    For atr_stop_target we use intra-bar prices: stop is checked against
    Low, target against High. Conservative ordering: stop wins if BOTH
    are touched in the same bar (worst-case assumption).
    """
    kind = cfg.get("kind")

    if kind == "atr_stop_target":
        if row["Low"] <= pos["stop"]:
            return {"exit_reason": "stop", "exit_price": pos["stop"]}
        if row["High"] >= pos["target"]:
            return {"exit_reason": "target", "exit_price": pos["target"]}
        max_hold = int(cfg.get("max_hold_bars", 60))
        if pos["bars_held"] >= max_hold:
            return {"exit_reason": "timeout", "exit_price": float(row["Close"])}
        return None

    if kind == "hold_period":
        n_hold = int(cfg["bars"])
        if pos["bars_held"] >= n_hold:
            return {"exit_reason": "timeout", "exit_price": float(row["Close"])}
        return None

    if kind == "signal_exit":
        # Exit when the (precomputed) exit_signal Series is True
        sig: pd.Series = cfg["_compiled_exit_signal"]
        if bool(sig.iat[bar_idx]):
            return {"exit_reason": "signal", "exit_price": float(row["Close"])}
        max_hold = int(cfg.get("max_hold_bars", 250))
        if pos["bars_held"] >= max_hold:
            return {"exit_reason": "timeout", "exit_price": float(row["Close"])}
        return None

    raise ValueError(f"unknown exit kind: {kind!r}")


# ─── main entrypoint ──────────────────────────────────────────────────

def run_spec(
    spec: Any,
    df: pd.DataFrame,
    *,
    ticker: str = "",
    bars_per_year: float = 252.0,
    slippage_bps: float = 0.0,
) -> BacktestResult:
    """Run one spec against one bar DataFrame.

    df must have columns Open/High/Low/Close and a DatetimeIndex. The
    engine does NOT enforce tz-awareness — it accepts naive or aware.

    slippage_bps: round-trip cost subtracted from each trade's pnl_pct.
        10 bps = 0.10% per round trip. Approximates ETF spread + 1tick
        slip per side. Applied uniformly regardless of trade direction
        or size. Set 0.0 for the perfect-fill baseline.
    """
    t0 = time.perf_counter()
    if isinstance(spec, dict):
        spec = StrategySpec.from_dict(spec)
    elif not isinstance(spec, StrategySpec):
        raise TypeError(f"spec must be dict or StrategySpec, got {type(spec).__name__}")

    _validate_df(df)
    entry_sig = _entry_signal(df, spec.entry)
    # Optional regime filter AND-gates the entry signal at the bar level.
    regime_cfg = spec.extras.get("regime_filter") or None
    if regime_cfg is not None:
        regime_sig = _entry_signal(df, regime_cfg)
        entry_sig = entry_sig & regime_sig

    # Pre-compile exit signal if it's signal_exit kind
    exit_cfg = dict(spec.exit)
    if exit_cfg.get("kind") == "signal_exit":
        exit_cfg["_compiled_exit_signal"] = _entry_signal(df, exit_cfg["signal"])

    # ATR for atr_stop_target
    atr_series: Optional[pd.Series] = None
    if exit_cfg.get("kind") == "atr_stop_target":
        atr_series = P.atr(df["High"], df["Low"], df["Close"], int(exit_cfg.get("atr_n", 14)))

    trades: List[Dict] = []
    position: Optional[Dict] = None
    bars_in_market = 0
    n = len(df)
    index = df.index

    for i in range(n):
        row = df.iloc[i]

        if position is None:
            if not bool(entry_sig.iat[i]):
                continue
            entry_price = float(row["Close"])
            position = {
                "direction": "LONG",
                "entry_price": entry_price,
                "entry_dt": index[i],
                "bars_held": 0,
            }
            if exit_cfg.get("kind") == "atr_stop_target":
                a = atr_series.iat[i] if atr_series is not None else float("nan")
                if not (a == a):  # NaN guard — skip warm-up
                    position = None
                    continue
                stop_mult = float(exit_cfg.get("stop_mult", 2.0))
                tgt_mult = float(exit_cfg.get("target_mult", 4.0))
                position["stop"] = entry_price - a * stop_mult
                position["target"] = entry_price + a * tgt_mult
                position["atr_at_entry"] = float(a)
            continue

        # Already in position
        position["bars_held"] += 1
        bars_in_market += 1
        outcome = _resolve_exit(position, row, i, exit_cfg, df)
        if outcome is None:
            continue
        exit_price = float(outcome["exit_price"])
        gross_pnl_pct = (exit_price - position["entry_price"]) / position["entry_price"] * 100.0
        # Slippage: bps are 1/100ths of 1% — convert to percent and subtract
        pnl_pct = gross_pnl_pct - (slippage_bps / 100.0)
        trades.append({
            "pnl_pct": round(pnl_pct, 4),
            "gross_pnl_pct": round(gross_pnl_pct, 4),
            "bars_held": int(position["bars_held"]),
            "direction": "LONG",
            "exit_reason": outcome["exit_reason"],
            "entry_dt": position["entry_dt"],
            "exit_dt": index[i],
            "entry_price": round(position["entry_price"], 4),
            "exit_price": round(exit_price, 4),
        })
        position = None

    metrics = compute_metrics(trades, bars_per_year=bars_per_year)
    if n > 0:
        metrics["exposure_pct"] = round(bars_in_market / n * 100.0, 2)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    return BacktestResult(
        spec_name=spec.name,
        ticker=ticker,
        trades=trades,
        metrics=metrics,
        bars_used=n,
        elapsed_ms=elapsed_ms,
    )


def _validate_df(df: pd.DataFrame) -> None:
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"df must be pd.DataFrame, got {type(df).__name__}")
    missing = [c for c in REQUIRED_BAR_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"df missing required columns: {missing}")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError(f"df.index must be DatetimeIndex, got {type(df.index).__name__}")
    if len(df) < 30:
        raise ValueError(f"df too short for backtest: len={len(df)} < 30")
