"""Tests for helio.data_feed_contract (Codex gap #1)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from helio.data_feed_contract import (
    CONTRACTS,
    DataFeedContract,
    verify_contract,
    verify_all_contracts,
)


def _write_csv(path: Path, latest_date: datetime, columns=("Date", "Close")) -> None:
    """Write a minimal CSV with one row at `latest_date`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [",".join(columns)]
    vals = []
    for col in columns:
        if col == "Date":
            vals.append(latest_date.strftime("%Y-%m-%d"))
        else:
            vals.append("100.0")
    rows.append(",".join(vals))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


# ─── Registry sanity ─────────────────────────────────────────────────

def test_xs_momentum_contract_universe_matches_runner():
    """Pin contract universe to the live runner's universe so they
    can't drift."""
    from forge.xs_momentum.runner import PARAMS
    contract = CONTRACTS["forge_xs_momentum"]
    assert tuple(PARAMS["universe"]) == contract.universe


def test_gld_pm_long_contract_present():
    c = CONTRACTS["forge_gld_pm_long"]
    assert c.universe == ("GLD",)
    assert c.primary_source == "yfinance"


def test_every_contract_has_nonempty_universe():
    for name, c in CONTRACTS.items():
        assert c.universe, f"{name} has empty universe"


def test_every_contract_documents_auto_adjust():
    """auto_adjust setting must be explicit — silent drift between
    baseline and live runner has bitten us before."""
    for name, c in CONTRACTS.items():
        assert isinstance(c.auto_adjust, bool), f"{name} missing auto_adjust"


# ─── verify_contract behavior ────────────────────────────────────────

def _make_contract(tmp_path: Path, tickers, max_age_days=7) -> DataFeedContract:
    return DataFeedContract(
        strategy="forge_test",
        primary_source="yfinance",
        fallback_cache_root=tmp_path,
        universe=tuple(tickers),
        max_age_days=max_age_days,
    )


def test_verify_green_when_all_fresh(tmp_path):
    contract = _make_contract(tmp_path, ["AAA", "BBB"])
    now = datetime(2026, 5, 24, tzinfo=timezone.utc)
    _write_csv(tmp_path / "AAA_daily.csv", now - timedelta(days=1))
    _write_csv(tmp_path / "BBB_daily.csv", now - timedelta(days=2))

    v = verify_contract(contract, now=now)
    assert v.verdict == "GREEN"
    assert sorted(v.universe_present) == ["AAA", "BBB"]
    assert not v.universe_missing
    assert not v.stale_tickers


def test_verify_red_when_ticker_missing(tmp_path):
    contract = _make_contract(tmp_path, ["AAA", "BBB"])
    now = datetime(2026, 5, 24, tzinfo=timezone.utc)
    _write_csv(tmp_path / "AAA_daily.csv", now - timedelta(days=1))
    # BBB cache file deliberately not written

    v = verify_contract(contract, now=now)
    assert v.verdict == "RED"
    assert v.universe_missing == ["BBB"]
    assert any("BBB" in r for r in v.reasons)


def test_verify_yellow_when_some_stale(tmp_path):
    contract = _make_contract(tmp_path, ["AAA", "BBB"], max_age_days=7)
    now = datetime(2026, 5, 24, tzinfo=timezone.utc)
    _write_csv(tmp_path / "AAA_daily.csv", now - timedelta(days=1))   # fresh
    _write_csv(tmp_path / "BBB_daily.csv", now - timedelta(days=30))  # stale

    v = verify_contract(contract, now=now)
    assert v.verdict == "YELLOW"
    assert len(v.stale_tickers) == 1
    assert v.stale_tickers[0]["ticker"] == "BBB"
    assert v.stale_tickers[0]["age_days"] >= 29


def test_verify_red_when_all_stale(tmp_path):
    """All present but uniformly stale = no insurance against live
    outage = RED, not YELLOW."""
    contract = _make_contract(tmp_path, ["AAA", "BBB"], max_age_days=7)
    now = datetime(2026, 5, 24, tzinfo=timezone.utc)
    _write_csv(tmp_path / "AAA_daily.csv", now - timedelta(days=30))
    _write_csv(tmp_path / "BBB_daily.csv", now - timedelta(days=30))

    v = verify_contract(contract, now=now)
    assert v.verdict == "RED"
    assert len(v.stale_tickers) == 2


def test_verify_red_when_required_column_missing(tmp_path):
    contract = DataFeedContract(
        strategy="forge_test",
        primary_source="yfinance",
        fallback_cache_root=tmp_path,
        universe=("AAA",),
        required_columns=("Close", "Volume"),
    )
    now = datetime(2026, 5, 24, tzinfo=timezone.utc)
    # Write a CSV missing the Volume column
    _write_csv(tmp_path / "AAA_daily.csv", now, columns=("Date", "Close"))

    v = verify_contract(contract, now=now)
    assert v.verdict == "RED"
    assert "AAA" in v.universe_missing


def test_cache_path_for_normalizes_ticker(tmp_path):
    contract = _make_contract(tmp_path, ["BRK.B"])  # ticker w/ symbols
    path = contract.cache_path_for("EUR=X")
    assert "EUR_X_daily.csv" in str(path)


# ─── verify_all_contracts ────────────────────────────────────────────

def test_verify_all_contracts_returns_one_entry_per_registry():
    results = verify_all_contracts()
    assert set(results.keys()) == set(CONTRACTS.keys())
    for name, r in results.items():
        assert "verdict" in r
        assert r["verdict"] in {"GREEN", "YELLOW", "RED"}
