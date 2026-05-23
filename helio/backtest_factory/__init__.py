"""Backtest factory v1 — screen many strategy specs against many tickers,
return ranked metrics quickly.

Design constraints (locked 2026-05-22 post-reset session):
    - pandas-only (no polars/duckdb/boto3/pyarrow installs in prod venv)
    - CSV bar input (existing helio/data/*.csv layout); parquet via
      helio.bar_store works once a parquet engine is installed
    - Long-only, single-position, daily-or-coarser bars for v1
    - Strategy spec is a JSON-serializable dict, not Python code
    - Deterministic: same spec + same bars => same trades

Public API:
    from helio.backtest_factory import run_spec, screen, walk_forward

Goal: 10 candidate specs screened against 5+ tickers in well under a
minute. The factory is the bridge between "I have an edge idea" and
"the survivor cohort should consider it."
"""
from __future__ import annotations

VERSION = "1.0.0"

from helio.backtest_factory.engine import run_spec, BacktestResult, StrategySpec
from helio.backtest_factory.metrics import compute_metrics
from helio.backtest_factory.walk_forward import walk_forward

__all__ = [
    "VERSION",
    "run_spec",
    "BacktestResult",
    "StrategySpec",
    "compute_metrics",
    "walk_forward",
]
