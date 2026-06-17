"""Tests for helio.yfinance_cache fallback storage."""
from __future__ import annotations

import pandas as pd

from helio import yfinance_cache as yfc


def test_cache_write_falls_back_to_pickle_without_parquet(monkeypatch, tmp_path):
    path = tmp_path / "SPY_1d_7d.parquet"
    idx = pd.to_datetime(["2026-05-22"], utc=True)
    df = pd.DataFrame({"Close": [100.0]}, index=idx)

    def _raise_parquet(self, *args, **kwargs):
        raise ImportError("no parquet engine")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", _raise_parquet)

    yfc._write_cache(path, df)

    fallback = path.with_suffix(".pkl")
    assert fallback.exists()
    loaded = yfc._read_cache(path)
    assert loaded is not None
    assert float(loaded["Close"].iloc[0]) == 100.0
    assert yfc._cache_age_seconds(path) < float("inf")
