"""Tests for helio.fama_french_data.

Network is not exercised — tests synthesize fake KF-format CSVs to
validate the parser's header detection, daily/monthly aggregation, and
caching. A single live-fetch smoke test is GUARDED behind an env var
so CI can opt in.
"""
from __future__ import annotations

import io
import os
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from helio.fama_french_data import (
    _parse_kf_csv,
    FamaFrenchError,
    load_factors_daily,
    load_factors_monthly,
)


# ─── _parse_kf_csv ───────────────────────────────────────────────────

def _factors_3_csv(rows: list[tuple[str, float, float, float, float]]) -> str:
    """Build a fake F-F_Research_Data_Factors_daily.csv string."""
    lines = [
        "This file was created by using the 202603 CRSP database.",
        "The Tbill return is the simple daily rate that ...",
        "",
        ",Mkt-RF,SMB,HML,RF",
    ]
    for date, mkt, smb, hml, rf in rows:
        lines.append(f"{date},{mkt:.2f},{smb:.2f},{hml:.2f},{rf:.2f}")
    lines.extend(["", "Copyright 2026 Eugene F. Fama and Kenneth R. French"])
    return "\n".join(lines)


def _momentum_csv(rows: list[tuple[str, float]]) -> str:
    """Build a fake F-F_Momentum_Factor_daily.csv string. Header text
    is long and contains 'momentum' to make sure the parser isn't
    fooled into picking a prose line as the column header."""
    lines = [
        "This file was created by using the 202603 CRSP database. It",
        "contains a momentum factor, constructed from six value-weight portfolios",
        "using independent sorts on size and prior return.",
        "Missing data are indicated by -99.99 or -999.",
        "",
        ",Mom",
    ]
    for date, mom in rows:
        lines.append(f"{date},{mom:.2f}")
    lines.extend(["", "Copyright 2026 Eugene F. Fama and Kenneth R. French"])
    return "\n".join(lines)


def test_parse_3factor_csv_basic():
    """Header should be found on the ',Mkt-RF,SMB,HML,RF' line, NOT the
    prose lines above it. Values must be divided by 100."""
    text = _factors_3_csv([
        ("20240101", 1.23, 0.45, -0.67, 0.01),
        ("20240102", -0.50, 0.10, 0.20, 0.01),
    ])
    df = _parse_kf_csv(text, expected_cols=["Mkt-RF", "SMB", "HML", "RF"])
    assert len(df) == 2
    assert list(df.columns) == ["Mkt-RF", "SMB", "HML", "RF"]
    assert df.iloc[0]["Mkt-RF"] == pytest.approx(0.0123)  # 1.23% -> 0.0123
    assert df.iloc[1]["HML"] == pytest.approx(0.002)


def test_parse_momentum_csv_does_not_match_prose_header():
    """REGRESSION GUARD: the parser used to match line 1 'contains a
    momentum factor...' as the header because 'mom' substring-matched.
    Header must start with ',' and have 'Mom' as a whole token."""
    text = _momentum_csv([
        ("19261103", 0.54),
        ("19261104", -0.51),
        ("19261105", 1.17),
    ])
    df = _parse_kf_csv(text, expected_cols=["Mom"])
    assert len(df) == 3
    assert df.iloc[0]["Mom"] == pytest.approx(0.0054)


def test_parse_stops_at_blank_line():
    """Daily section ends at the first blank line (before the copyright
    footer or any monthly/annual aggregated sections)."""
    text = _factors_3_csv([
        ("20240101", 1.0, 0.0, 0.0, 0.01),
        ("20240102", 1.0, 0.0, 0.0, 0.01),
    ])
    df = _parse_kf_csv(text, expected_cols=["Mkt-RF", "SMB", "HML", "RF"])
    assert len(df) == 2
    # Copyright line and any other footer must NOT be parsed
    assert "Copyright" not in df.index.astype(str).tolist()


def test_parse_raises_when_header_missing():
    text = "no header here\nrandom prose\n"
    with pytest.raises(FamaFrenchError, match="header"):
        _parse_kf_csv(text, expected_cols=["Mkt-RF", "SMB", "HML", "RF"])


def test_parse_index_is_datetime():
    text = _factors_3_csv([("20240115", 0.5, 0.0, 0.0, 0.01)])
    df = _parse_kf_csv(text, expected_cols=["Mkt-RF", "SMB", "HML", "RF"])
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index[0] == pd.Timestamp("2024-01-15")


# ─── load_factors_daily / load_factors_monthly with stubbed cache ───

def _write_cache_files(tmp_path: Path) -> None:
    """Plant fake KF CSVs in the cache so load_factors_daily skips download."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "ff_factors_daily.csv").write_text(
        _factors_3_csv([
            ("20240101", 0.50, 0.10, -0.20, 0.01),
            ("20240102", -0.30, 0.20, 0.10, 0.01),
            ("20240103", 1.00, -0.10, 0.30, 0.01),
            ("20240131", 0.40, 0.05, 0.05, 0.01),
            ("20240201", -0.20, 0.00, 0.10, 0.01),
            ("20240228", 0.10, 0.10, 0.00, 0.01),
        ]),
        encoding="utf-8",
    )
    (cache_dir / "ff_momentum_daily.csv").write_text(
        _momentum_csv([
            ("20240101", 0.20),
            ("20240102", -0.15),
            ("20240103", 0.50),
            ("20240131", 0.10),
            ("20240201", -0.05),
            ("20240228", 0.30),
        ]),
        encoding="utf-8",
    )


def test_load_factors_daily_returns_merged_frame(monkeypatch, tmp_path):
    _write_cache_files(tmp_path)
    import helio.fama_french_data as ff
    monkeypatch.setattr(ff, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(ff, "CACHE_FACTORS_3", tmp_path / "cache" / "ff_factors_daily.csv")
    monkeypatch.setattr(ff, "CACHE_MOMENTUM", tmp_path / "cache" / "ff_momentum_daily.csv")
    # Avoid hitting the network — assume cache is fresh
    monkeypatch.setattr(ff, "_is_cache_fresh", lambda *a, **kw: True)

    df = ff.load_factors_daily()
    assert list(df.columns) == ["MKT_RF", "SMB", "HML", "RF", "MOM"]
    assert len(df) == 6
    # Mkt-RF was renamed to MKT_RF
    assert df.iloc[0]["MKT_RF"] == pytest.approx(0.005)
    # MOM column came from the momentum CSV
    assert df.iloc[0]["MOM"] == pytest.approx(0.002)


def test_load_factors_monthly_compounds_daily_returns(monkeypatch, tmp_path):
    """Month-end aggregation must compound: monthly = prod(1+daily) - 1."""
    _write_cache_files(tmp_path)
    import helio.fama_french_data as ff
    monkeypatch.setattr(ff, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(ff, "CACHE_FACTORS_3", tmp_path / "cache" / "ff_factors_daily.csv")
    monkeypatch.setattr(ff, "CACHE_MOMENTUM", tmp_path / "cache" / "ff_momentum_daily.csv")
    monkeypatch.setattr(ff, "_is_cache_fresh", lambda *a, **kw: True)

    monthly = ff.load_factors_monthly()
    # Two month-end buckets in the synthetic data: 2024-01-31 and 2024-02-29
    assert len(monthly) == 2
    # Jan compound: (1.005)*(0.997)*(1.01)*(1.004) - 1 ≈ 0.0160
    jan = monthly.loc["2024-01-31"]
    expected = ((1.005) * (0.997) * (1.01) * (1.004)) - 1.0
    assert jan["MKT_RF"] == pytest.approx(expected, abs=1e-5)


def test_load_factors_monthly_index_is_month_end(monkeypatch, tmp_path):
    _write_cache_files(tmp_path)
    import helio.fama_french_data as ff
    monkeypatch.setattr(ff, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(ff, "CACHE_FACTORS_3", tmp_path / "cache" / "ff_factors_daily.csv")
    monkeypatch.setattr(ff, "CACHE_MOMENTUM", tmp_path / "cache" / "ff_momentum_daily.csv")
    monkeypatch.setattr(ff, "_is_cache_fresh", lambda *a, **kw: True)

    monthly = ff.load_factors_monthly()
    for ts in monthly.index:
        # All entries should land on month-end
        next_day = ts + pd.Timedelta(days=1)
        assert next_day.month != ts.month, (
            f"{ts} is not a month-end timestamp")


# ─── integration with factor_decomposition (KF mode) ─────────────────

def test_factor_decomposition_source_kf_uses_kf_data(monkeypatch, tmp_path):
    """When source='kf', build_factor_panel must consult fama_french_data."""
    _write_cache_files(tmp_path)
    import helio.fama_french_data as ff
    import helio.factor_decomposition as fd
    monkeypatch.setattr(ff, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(ff, "CACHE_FACTORS_3", tmp_path / "cache" / "ff_factors_daily.csv")
    monkeypatch.setattr(ff, "CACHE_MOMENTUM", tmp_path / "cache" / "ff_momentum_daily.csv")
    monkeypatch.setattr(ff, "_is_cache_fresh", lambda *a, **kw: True)

    panel = fd.build_factor_panel(
        start="2024-01-01", end="2024-03-01",
        rf_annual=0.045,
        include_cross_asset=False,
        source="kf",
    )
    # Should have MKT_RF, SMB, HML, MOM, _RF columns (KF mode includes
    # the raw RF for accurate per-month excess-return subtraction)
    for col in ("MKT_RF", "SMB", "HML", "MOM", "_RF"):
        assert col in panel.columns, f"missing {col}"
    # 2 month-end rows
    assert len(panel) == 2


# ─── live smoke test (opt-in) ────────────────────────────────────────

@pytest.mark.skipif(
    not os.environ.get("ARGUS_TEST_LIVE_FF"),
    reason="set ARGUS_TEST_LIVE_FF=1 to enable Ken French network fetch",
)
def test_live_fetch_smoke():
    """Live integration test — hits Dartmouth's data library. Skip by
    default; opt in by setting ARGUS_TEST_LIVE_FF=1."""
    df = load_factors_daily()
    assert len(df) > 10000, "expect tens of thousands of daily rows back to 1926"
    assert "MKT_RF" in df.columns
    assert "MOM" in df.columns
    # Data should extend at least to 2024
    assert df.index.max() >= pd.Timestamp("2024-12-31")
