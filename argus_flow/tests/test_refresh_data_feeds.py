"""Tests for ops.maintenance.refresh_data_feeds (Codex gap #2).

Focused on the orchestration + dedup logic. The actual yfinance pull
is monkeypatched out — we don't hit the network in CI.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _stub_fetch_universe_factory(call_log):
    """Build a stub for helio.yfinance_data.fetch_universe that
    records the tickers passed and returns synthetic YFFetchResults."""
    from helio.yfinance_data import YFFetchResult

    def _stub(tickers, refresh=False, **kwargs):
        call_log.append({
            "tickers": list(tickers),
            "refresh": refresh,
        })
        return [
            YFFetchResult(
                ticker=t,
                rows_fetched=10,
                rows_total=2500,
                cache_path=Path(f"stub/{t}.csv"),
                incremental=not refresh,
                elapsed_s=0.01,
            )
            for t in tickers
        ]
    return _stub


def test_refresh_dedupes_tickers_across_contracts(monkeypatch, tmp_path):
    """forge_gld_pm_long has GLD in its universe; if forge_xs_momentum
    is also configured to use GLD (it is, in the broad-8 universe), the
    refresh script must NOT pull GLD twice."""
    call_log = []
    import helio.yfinance_data as yfd
    monkeypatch.setattr(yfd, "fetch_universe", _stub_fetch_universe_factory(call_log))

    from helio.data_feed_contract import CONTRACTS
    assert "GLD" in CONTRACTS["forge_xs_momentum"].universe
    assert CONTRACTS["forge_gld_pm_long"].universe == ("GLD",)

    # Suppress verification so we don't read real cache
    from ops.maintenance import refresh_data_feeds as r
    monkeypatch.setattr(
        "helio.data_feed_contract.verify_all_contracts",
        lambda: {},
    )

    rc = r.main(["--no-verify"])
    assert rc == 0

    all_tickers_pulled = [t for call in call_log for t in call["tickers"]]
    # Each unique ticker appears exactly once
    assert len(all_tickers_pulled) == len(set(all_tickers_pulled))
    # GLD specifically appears exactly once
    assert all_tickers_pulled.count("GLD") == 1


def test_refresh_only_filters_to_one_contract(monkeypatch, tmp_path):
    """--only forge_xs_momentum should refresh only that contract's
    universe."""
    call_log = []
    import helio.yfinance_data as yfd
    monkeypatch.setattr(yfd, "fetch_universe", _stub_fetch_universe_factory(call_log))
    monkeypatch.setattr(
        "helio.data_feed_contract.verify_all_contracts",
        lambda: {},
    )

    from ops.maintenance import refresh_data_feeds as r
    rc = r.main(["--only", "forge_xs_momentum", "--no-verify"])
    assert rc == 0

    from helio.data_feed_contract import CONTRACTS
    expected = set(CONTRACTS["forge_xs_momentum"].universe)
    actually_pulled = {t for call in call_log for t in call["tickers"]}
    assert actually_pulled == expected


def test_refresh_only_rejects_unknown_contract(monkeypatch, capsys):
    monkeypatch.setattr(
        "helio.data_feed_contract.verify_all_contracts",
        lambda: {},
    )
    from ops.maintenance import refresh_data_feeds as r
    rc = r.main(["--only", "forge_does_not_exist"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "unknown contract" in captured.out.lower()


def test_refresh_returns_2_when_post_verify_red(monkeypatch):
    """If post-refresh verification is RED, exit code must be 2 so the
    scheduled task can surface the failure."""
    call_log = []
    import helio.yfinance_data as yfd
    monkeypatch.setattr(yfd, "fetch_universe", _stub_fetch_universe_factory(call_log))
    monkeypatch.setattr(
        "helio.data_feed_contract.verify_all_contracts",
        lambda: {"forge_xs_momentum": {"verdict": "RED", "reasons": ["stub"]}},
    )

    from ops.maintenance import refresh_data_feeds as r
    rc = r.main([])
    assert rc == 2


def test_refresh_returns_1_when_post_verify_yellow(monkeypatch):
    call_log = []
    import helio.yfinance_data as yfd
    monkeypatch.setattr(yfd, "fetch_universe", _stub_fetch_universe_factory(call_log))
    monkeypatch.setattr(
        "helio.data_feed_contract.verify_all_contracts",
        lambda: {"forge_xs_momentum": {"verdict": "YELLOW", "reasons": ["stub"]}},
    )

    from ops.maintenance import refresh_data_feeds as r
    rc = r.main([])
    assert rc == 1


def test_refresh_writes_report_artifact(monkeypatch, tmp_path):
    call_log = []
    import helio.yfinance_data as yfd
    monkeypatch.setattr(yfd, "fetch_universe", _stub_fetch_universe_factory(call_log))
    monkeypatch.setattr(
        "helio.data_feed_contract.verify_all_contracts",
        lambda: {"forge_xs_momentum": {"verdict": "GREEN", "reasons": []}},
    )

    from ops.maintenance import refresh_data_feeds as r
    out_dir = tmp_path / "reports"
    monkeypatch.setattr(r, "OUT_DIR", out_dir)
    rc = r.main([])
    assert rc == 0
    report_path = out_dir / "data_feed_refresh.json"
    assert report_path.exists()
    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert "refresh_log" in data
    assert "post_refresh_verdicts" in data
    assert data["refresh_mode"] == "incremental"


def test_runbook_exists_and_documents_scheduled_task():
    """Sanity-pin that the runbook exists and covers the scheduled
    task setup — that's the operator's main reference."""
    runbook = (
        Path(__file__).resolve().parents[2] / "ops" / "DATA_FEED_RUNBOOK.md"
    )
    assert runbook.exists()
    text = runbook.read_text(encoding="utf-8")
    assert "Task Scheduler" in text
    assert "refresh_data_feeds" in text
    assert "exit `2`" in text  # the RED exit-code path must be documented
