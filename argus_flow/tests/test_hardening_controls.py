from __future__ import annotations

import json
import os
import sys
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from argus_flow.ops.promotion_gate_v2 import _filter_rows_since
from argus_flow.runner_unified import State, _sync_entry_block_state
from helio.fleet_monitor import build_restart_args
from helio.portfolio_guard import _load_legacy_positions
from helio.runner import _build_contract as _build_helio_swing_contract
from helio.runner_hermes import _build_contract as _build_helio_hermes_contract

TEST_TMP_ROOT = Path(__file__).resolve().parents[2] / ".tmp_sandbox_tests"


def test_state_clear_trade_state_preserves_cumulative_fields() -> None:
    TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="hardening_", dir=TEST_TMP_ROOT))
    try:
        state = State(tmp / "state.json")
        state.position = "LONG"
        state.entry_price = 1.2345
        state.stop_price = 1.2300
        state.target_price = 1.2400
        state.position_size = 25000
        state.entry_risk_usd = 125.0
        state.trade_count = 7
        state.pnl_pips = 12.5
        state.pnl_usd = 31.4

        state.clear_trade_state()

        assert state.position == "FLAT"
        assert state.entry_price == 0.0
        assert state.stop_price == 0.0
        assert state.target_price == 0.0
        assert state.position_size == 0.0
        assert state.entry_risk_usd == 0.0
        assert state.trade_count == 7
        assert state.pnl_pips == 12.5
        assert state.pnl_usd == 31.4
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sync_entry_block_state_prefers_control_reason() -> None:
    inst = SimpleNamespace(
        _control_blocked=False,
        _control_block_reason="",
        _recon_blocked=False,
        _recon_block_reason="",
    )

    _sync_entry_block_state(inst)
    assert inst._entries_blocked is False
    assert inst._entries_block_reason == ""

    inst._recon_blocked = True
    inst._recon_block_reason = "RECON_DRIFT"
    _sync_entry_block_state(inst)
    assert inst._entries_blocked is True
    assert inst._entries_block_reason == "RECON_DRIFT"

    inst._control_blocked = True
    inst._control_block_reason = "PAUSE_ENTRIES"
    _sync_entry_block_state(inst)
    assert inst._entries_blocked is True
    assert inst._entries_block_reason == "PAUSE_ENTRIES"


def test_build_restart_args_is_paper_safe_by_default() -> None:
    cfg = {
        "restart_args": ["-m", "titan.runner", "--loop"],
        "supports_live_flag": True,
    }

    assert build_restart_args(cfg, legacy_execution_mode="paper") == ["-m", "titan.runner", "--loop"]
    assert build_restart_args(cfg, legacy_execution_mode="live") == ["-m", "titan.runner", "--loop", "--live"]


def test_filter_rows_since_keeps_only_current_stage_rows() -> None:
    stage_start = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
    rows = [
        {"ts": "2026-04-10T11:59:59+00:00", "value": "old"},
        {"ts": "2026-04-10T12:00:00+00:00", "value": "edge"},
        {"ts": "2026-04-10T12:05:00+00:00", "value": "new"},
    ]

    filtered = _filter_rows_since(rows, stage_start)

    assert [row["value"] for row in filtered] == ["edge", "new"]


def test_load_legacy_positions_reads_stock_runner_shape() -> None:
    positions_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            prefix="legacy_positions_",
            delete=False,
        ) as fh:
            fh.write(json.dumps({
                "AAPL": {"direction": "LONG", "entry_price": 150.0, "status": "OPEN"},
                "MSFT": {"direction": "SHORT", "entry_price": 300.0, "status": "CLOSED"},
            }))
            positions_path = Path(fh.name)

        positions = _load_legacy_positions(positions_path, "titan")

        assert len(positions) == 1
        assert positions[0].family == "titan"
        assert positions[0].symbol == "AAPL"
        assert positions[0].direction == "LONG"
    finally:
        if positions_path is not None:
            try:
                os.unlink(positions_path)
            except OSError:
                pass


def test_load_legacy_positions_reads_rotation_holdings_shape() -> None:
    positions_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            prefix="rotation_positions_",
            delete=False,
        ) as fh:
            fh.write(json.dumps({
                "holdings": {
                    "XLE": {"entry_price": 92.5},
                    "GLD": {"entry_price": 215.1},
                }
            }))
            positions_path = Path(fh.name)

        positions = _load_legacy_positions(positions_path, "ares_rotation")

        assert len(positions) == 2
        assert {pos.symbol for pos in positions} == {"XLE", "GLD"}
        assert {pos.direction for pos in positions} == {"LONG"}
    finally:
        if positions_path is not None:
            try:
                os.unlink(positions_path)
            except OSError:
                pass


def test_helio_swing_uses_continuous_future_contracts_for_futures() -> None:
    contract = _build_helio_swing_contract({
        "ibkr_symbol": "MES",
        "ibkr_sec_type": "FUT",
        "ibkr_exchange": "CME",
    })

    assert contract.secType == "CONTFUT"
    assert contract.symbol == "MES"
    assert contract.exchange == "CME"


def test_helio_hermes_uses_continuous_future_contracts_for_futures() -> None:
    contract = _build_helio_hermes_contract({
        "ibkr_symbol": "MGC",
        "ibkr_sec_type": "FUT",
        "ibkr_exchange": "COMEX",
    })

    assert contract.secType == "CONTFUT"
    assert contract.symbol == "MGC"
    assert contract.exchange == "COMEX"
