"""SPY benchmark over strategy-active timestamps only.

The naive "did strategy X beat SPY this year?" comparison is misleading
when X only fires in narrow windows. A strategy that runs only during
high-VIX regimes shouldn't be benchmarked against SPY's full-year
trend — only against SPY's return *during the windows the strategy was
actually positioned*.

This module reads SPY price data and produces a per-trade benchmark
return that the ROI proof engine can compare against the strategy's own
realized return. The match is timestamp-aligned: for each strategy
trade with entry_ts and exit_ts, look up SPY's return between those two
timestamps and treat that as the "would have happened if I'd bought SPY
instead and held for the same window" baseline.

The comparison is **directional-naive** (always treats SPY as long).
For short strategies, callers should either flip the sign or treat the
benchmark as "buy-and-hold opportunity cost."

Data path: ``helio/data/SPY_1h.csv`` (preferred, finer granularity) or
``helio/data/SPY_daily.csv`` (fallback). Both are committed snapshots —
refresh via the existing ingestion path before running ROI proofs on
new windows.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

REPO = Path(__file__).resolve().parents[1]
SPY_1H = REPO / "helio" / "data" / "SPY_1h.csv"
SPY_DAILY = REPO / "helio" / "data" / "SPY_daily.csv"


@dataclass(frozen=True)
class SpyBar:
    """One row of SPY price history."""

    ts: datetime
    close: float


def _parse_ts(raw: str) -> datetime | None:
    """Best-effort parse of a timestamp cell. Returns None when unparseable
    so the caller can skip the row rather than crash on a header line."""
    s = (raw or "").strip()
    if not s or s in ("Date", "Datetime", "Price", "Ticker", "SPY"):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            d = datetime.strptime(s, fmt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(timezone.utc)
        except ValueError:
            continue
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except ValueError:
        pass
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
        return d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def load_spy_bars(path: Path | str | None = None) -> list[SpyBar]:
    """Load SPY bars sorted by timestamp ascending. Best-available data
    path is used: 1h data is preferred for intraday strategies, daily as
    fallback. Empty list if neither file exists."""
    if path is not None:
        candidates = [Path(path)]
    else:
        candidates = [SPY_1H, SPY_DAILY]
    for fp in candidates:
        if fp.exists():
            bars = _read_spy_csv(fp)
            if bars:
                return sorted(bars, key=lambda b: b.ts)
    return []


def _read_spy_csv(fp: Path) -> list[SpyBar]:
    """Read either schema we ship: ``Date,Open,High,Low,Close,Volume`` (daily)
    or ``Price,Close,High,Low,Open,Volume`` with a 'Ticker,SPY,...' second
    line then ``ts,close,...`` rows (1h yfinance dump)."""
    rows: list[SpyBar] = []
    try:
        with fp.open(encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if header is None:
                return rows
            close_idx: int | None = None
            ts_idx: int = 0
            cleaned_header = [h.strip() for h in header]
            for i, name in enumerate(cleaned_header):
                if name.lower() == "close":
                    close_idx = i
            if close_idx is None:
                close_idx = 1
            for row in reader:
                if not row:
                    continue
                ts = _parse_ts(row[ts_idx])
                if ts is None:
                    continue
                try:
                    close = float(row[close_idx])
                except (ValueError, IndexError):
                    continue
                rows.append(SpyBar(ts=ts, close=close))
    except OSError:
        return []
    return rows


def spy_return_between(
    bars: list[SpyBar],
    start: datetime,
    end: datetime,
) -> Optional[float]:
    """Fractional SPY return between two timestamps. Uses the closest bar
    AT-OR-BEFORE each timestamp. Returns None if either side is missing
    (i.e., the timestamp is outside the available data window)."""
    if not bars or end <= start:
        return None
    a = _bar_at_or_before(bars, start)
    b = _bar_at_or_before(bars, end)
    if a is None or b is None or a.close <= 0:
        return None
    return (b.close / a.close) - 1.0


def _bar_at_or_before(bars: list[SpyBar], when: datetime) -> SpyBar | None:
    """Return the most recent bar whose ts is <= ``when``. Linear scan from
    the end since the typical lookup is recent. Returns None if all bars
    are after ``when``."""
    for bar in reversed(bars):
        if bar.ts <= when:
            return bar
    return None


@dataclass
class TradeBenchmark:
    """Result of benchmarking one strategy trade against SPY over the
    same window."""

    entry_ts: str
    exit_ts: str
    strategy_return_frac: float
    spy_return_frac: Optional[float]
    excess_return_frac: Optional[float]
    spy_data_available: bool


def benchmark_trade(
    bars: list[SpyBar],
    entry_ts: str,
    exit_ts: str,
    strategy_pnl: float,
    notional: float,
) -> TradeBenchmark:
    """Compare one trade to SPY over the same window. Excess return is
    strategy - SPY when both are available; None when SPY data doesn't
    cover the window."""
    e = _parse_ts(entry_ts)
    x = _parse_ts(exit_ts)
    strat_return = (strategy_pnl / notional) if notional > 0 else 0.0
    if e is None or x is None:
        return TradeBenchmark(
            entry_ts=entry_ts, exit_ts=exit_ts,
            strategy_return_frac=strat_return,
            spy_return_frac=None, excess_return_frac=None,
            spy_data_available=False,
        )
    spy_ret = spy_return_between(bars, e, x)
    excess = (strat_return - spy_ret) if spy_ret is not None else None
    return TradeBenchmark(
        entry_ts=entry_ts, exit_ts=exit_ts,
        strategy_return_frac=strat_return,
        spy_return_frac=spy_ret,
        excess_return_frac=excess,
        spy_data_available=spy_ret is not None,
    )


def aggregate_benchmark(
    benchmarks: Iterable[TradeBenchmark],
) -> dict:
    """Roll up per-trade benchmarks into a strategy-level summary. Reports
    coverage (how many trades had SPY data) so callers can avoid drawing
    conclusions from partially-benchmarked series."""
    items = list(benchmarks)
    n = len(items)
    covered = [b for b in items if b.spy_data_available and b.excess_return_frac is not None]
    n_covered = len(covered)
    if not covered:
        return {
            "n_trades": n,
            "n_with_spy_data": 0,
            "coverage_pct": 0.0,
            "mean_strategy_return": 0.0,
            "mean_spy_return": 0.0,
            "mean_excess_return": 0.0,
            "wins_vs_spy": 0,
            "ties_vs_spy": 0,
            "losses_vs_spy": 0,
        }
    mean_strat = sum(b.strategy_return_frac for b in covered) / n_covered
    mean_spy = sum(b.spy_return_frac or 0.0 for b in covered) / n_covered
    mean_excess = sum(b.excess_return_frac or 0.0 for b in covered) / n_covered
    wins = sum(1 for b in covered if (b.excess_return_frac or 0.0) > 0)
    ties = sum(1 for b in covered if (b.excess_return_frac or 0.0) == 0)
    losses = sum(1 for b in covered if (b.excess_return_frac or 0.0) < 0)
    return {
        "n_trades": n,
        "n_with_spy_data": n_covered,
        "coverage_pct": round(n_covered / n, 4) if n else 0.0,
        "mean_strategy_return": round(mean_strat, 6),
        "mean_spy_return": round(mean_spy, 6),
        "mean_excess_return": round(mean_excess, 6),
        "wins_vs_spy": wins,
        "ties_vs_spy": ties,
        "losses_vs_spy": losses,
    }


__all__ = [
    "SpyBar",
    "TradeBenchmark",
    "load_spy_bars",
    "spy_return_between",
    "benchmark_trade",
    "aggregate_benchmark",
]
