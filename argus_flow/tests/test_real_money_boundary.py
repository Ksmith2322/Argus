"""Tests for the real-money boundary module.

The boundary's contract is "default off, fail closed" — every test here
exercises a way the boundary should refuse to let a real-money order
through, plus the narrow positive paths where everything is correctly
configured.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from helio import real_money as rm


@pytest.fixture
def real_money_module_enabled(monkeypatch):
    """Flip the module-level REAL_MONEY_ENABLED kill-switch for the duration of
    a test. The boundary has two kill-switches by design (allowlist.global_enabled
    AND the module constant); tests that exercise the post-switch paths must
    enable both. test_module_default_is_off intentionally does NOT use this."""
    monkeypatch.setattr(rm, "REAL_MONEY_ENABLED", True)
    yield


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeClient:
    def __init__(self, port: int) -> None:
        self.port = port


class _FakeIB:
    """Minimal stand-in for ib_insync.IB used by is_real_money_connection."""

    def __init__(self, port: int = rm.PAPER_PORT, accounts=()) -> None:
        self.client = _FakeClient(port)
        self._accounts = list(accounts)

    def managedAccounts(self):
        return list(self._accounts)


# ---------------------------------------------------------------------------
# Allowlist loader
# ---------------------------------------------------------------------------

def test_missing_allowlist_file_yields_empty_allowlist(tmp_path):
    al = rm.load_allowlist(tmp_path / "does_not_exist.json")
    assert al.global_enabled is False
    assert al.strategies == ()
    assert al.is_real_money_strategy("forge_vix_intraday") is False


def test_corrupt_allowlist_file_falls_back_to_empty(tmp_path):
    p = tmp_path / "allowlist.json"
    p.write_text("{this is not json")
    al = rm.load_allowlist(p)
    assert al.global_enabled is False
    assert al.strategies == ()


def test_well_formed_allowlist_loads(tmp_path):
    p = tmp_path / "allowlist.json"
    p.write_text(json.dumps({
        "global_enabled": True,
        "strategies": ["forge_vix_intraday"],
        "max_strategies_real": 1,
        "ledger_entry_id": "abc-1",
        "approver": "ksmith2322",
        "signed_at": "2026-05-30T19:30:00Z",
    }))
    al = rm.load_allowlist(p)
    assert al.global_enabled is True
    assert al.strategies == ("forge_vix_intraday",)
    assert al.is_real_money_strategy("forge_vix_intraday") is True
    assert al.is_real_money_strategy("forge_multi_orb") is False
    assert al.validate_self() == []


def test_allowlist_with_global_enabled_but_empty_strategies_is_invalid(tmp_path):
    p = tmp_path / "allowlist.json"
    p.write_text(json.dumps({
        "global_enabled": True,
        "strategies": [],
        "max_strategies_real": 1,
        "ledger_entry_id": "abc-1",
        "approver": "ksmith2322",
    }))
    al = rm.load_allowlist(p)
    problems = al.validate_self()
    assert any("strategies list is empty" in p for p in problems)


def test_allowlist_exceeding_cap_is_invalid(tmp_path):
    p = tmp_path / "allowlist.json"
    p.write_text(json.dumps({
        "global_enabled": True,
        "strategies": ["a", "b", "c"],
        "max_strategies_real": 1,
        "ledger_entry_id": "abc-1",
        "approver": "ksmith2322",
    }))
    al = rm.load_allowlist(p)
    problems = al.validate_self()
    assert any("max_strategies_real" in p for p in problems)


def test_allowlist_global_enabled_requires_signature(tmp_path):
    p = tmp_path / "allowlist.json"
    p.write_text(json.dumps({
        "global_enabled": True,
        "strategies": ["forge_vix_intraday"],
        "max_strategies_real": 1,
        "ledger_entry_id": "",
        "approver": "",
    }))
    al = rm.load_allowlist(p)
    problems = al.validate_self()
    assert any("ledger_entry_id" in p for p in problems)
    assert any("approver" in p for p in problems)


# ---------------------------------------------------------------------------
# is_real_money_connection
# ---------------------------------------------------------------------------

def test_paper_port_is_not_real():
    assert rm.is_real_money_connection(_FakeIB(port=rm.PAPER_PORT)) is False


def test_real_port_is_real():
    assert rm.is_real_money_connection(_FakeIB(port=rm.REAL_PORT)) is True


def test_none_client_is_not_real():
    assert rm.is_real_money_connection(None) is False


def test_real_account_id_in_managed_accounts_is_real(monkeypatch):
    monkeypatch.setattr(rm, "REAL_ACCOUNT_ID", "U1234567")
    ib = _FakeIB(port=rm.PAPER_PORT, accounts=("U1234567",))
    assert rm.is_real_money_connection(ib) is True


def test_paper_account_only_is_not_real(monkeypatch):
    monkeypatch.setattr(rm, "REAL_ACCOUNT_ID", "U1234567")
    ib = _FakeIB(port=rm.PAPER_PORT, accounts=("DUP472829",))
    assert rm.is_real_money_connection(ib) is False


# ---------------------------------------------------------------------------
# enforce_real_money_boundary
# ---------------------------------------------------------------------------

def test_paper_passthrough_is_noop():
    ib = _FakeIB(port=rm.PAPER_PORT)
    al = rm.Allowlist(global_enabled=False, strategies=())
    # Should not raise even with absurd notional and unknown strategy:
    rm.enforce_real_money_boundary(
        ib, strategy_label="unknown", notional_usd=1_000_000.0, allowlist=al,
    )


def test_real_with_allowlist_disabled_rejects():
    ib = _FakeIB(port=rm.REAL_PORT)
    al = rm.Allowlist(global_enabled=False, strategies=())
    with pytest.raises(rm.AccountBoundaryViolationError, match="real_money_disabled"):
        rm.enforce_real_money_boundary(
            ib, strategy_label="forge_vix_intraday", allowlist=al,
        )


def test_real_with_strategy_not_allowlisted_rejects(real_money_module_enabled):
    ib = _FakeIB(port=rm.REAL_PORT)
    al = rm.Allowlist(
        global_enabled=True,
        strategies=("forge_vix_intraday",),
        max_strategies_real=1,
        ledger_entry_id="abc-1",
        approver="ksmith2322",
    )
    with pytest.raises(rm.AccountBoundaryViolationError, match="strategy_not_allowlisted"):
        rm.enforce_real_money_boundary(
            ib, strategy_label="forge_multi_orb", allowlist=al,
        )


def test_real_with_oversized_notional_rejects(monkeypatch, real_money_module_enabled):
    ib = _FakeIB(port=rm.REAL_PORT)
    al = rm.Allowlist(
        global_enabled=True,
        strategies=("forge_vix_intraday",),
        max_strategies_real=1,
        ledger_entry_id="abc-1",
        approver="ksmith2322",
    )
    monkeypatch.setattr(rm, "_capital_ladder_approved_capital_usd", lambda: 100_000.0)
    too_big = rm.REAL_MONEY_MAX_NOTIONAL_PER_ORDER * 2
    with pytest.raises(rm.AccountBoundaryViolationError, match="oversize_real_order"):
        rm.enforce_real_money_boundary(
            ib,
            strategy_label="forge_vix_intraday",
            notional_usd=too_big,
            allowlist=al,
        )


def test_real_without_capital_ladder_approval_rejects(real_money_module_enabled):
    ib = _FakeIB(port=rm.REAL_PORT)
    al = rm.Allowlist(
        global_enabled=True,
        strategies=("forge_vix_intraday",),
        max_strategies_real=1,
        ledger_entry_id="abc-1",
        approver="ksmith2322",
    )
    with pytest.raises(rm.AccountBoundaryViolationError, match="capital_ladder_blocked"):
        rm.enforce_real_money_boundary(
            ib,
            strategy_label="forge_vix_intraday",
            notional_usd=100.0,
            allowlist=al,
        )


def test_real_within_cap_with_allowlisted_strategy_passes(monkeypatch, real_money_module_enabled):
    ib = _FakeIB(port=rm.REAL_PORT)
    al = rm.Allowlist(
        global_enabled=True,
        strategies=("forge_vix_intraday",),
        max_strategies_real=1,
        ledger_entry_id="abc-1",
        approver="ksmith2322",
    )
    monkeypatch.setattr(rm, "_capital_ladder_approved_capital_usd", lambda: 10_000.0)
    monkeypatch.setattr(rm, "_existing_real_money_notional_usd", lambda allowlist: 0.0)
    # No exception:
    rm.enforce_real_money_boundary(
        ib,
        strategy_label="forge_vix_intraday",
        notional_usd=rm.REAL_MONEY_MAX_NOTIONAL_PER_ORDER - 1,
        allowlist=al,
    )


def test_real_gross_above_ladder_cap_rejects(monkeypatch, real_money_module_enabled):
    ib = _FakeIB(port=rm.REAL_PORT)
    al = rm.Allowlist(
        global_enabled=True,
        strategies=("forge_vix_intraday",),
        max_strategies_real=1,
        ledger_entry_id="abc-1",
        approver="ksmith2322",
    )
    monkeypatch.setattr(rm, "_capital_ladder_approved_capital_usd", lambda: 5_000.0)
    monkeypatch.setattr(rm, "_existing_real_money_notional_usd", lambda allowlist: 4_500.0)
    with pytest.raises(rm.AccountBoundaryViolationError, match="capital_ladder_gross_oversize"):
        rm.enforce_real_money_boundary(
            ib,
            strategy_label="forge_vix_intraday",
            notional_usd=600.0,
            allowlist=al,
        )


def test_real_gross_under_ladder_cap_passes(monkeypatch, real_money_module_enabled):
    ib = _FakeIB(port=rm.REAL_PORT)
    al = rm.Allowlist(
        global_enabled=True,
        strategies=("forge_vix_intraday",),
        max_strategies_real=1,
        ledger_entry_id="abc-1",
        approver="ksmith2322",
    )
    monkeypatch.setattr(rm, "_capital_ladder_approved_capital_usd", lambda: 5_000.0)
    monkeypatch.setattr(rm, "_existing_real_money_notional_usd", lambda allowlist: 1_000.0)
    rm.enforce_real_money_boundary(
        ib,
        strategy_label="forge_vix_intraday",
        notional_usd=500.0,
        allowlist=al,
    )


def test_real_above_ladder_cap_rejects(monkeypatch, real_money_module_enabled):
    ib = _FakeIB(port=rm.REAL_PORT)
    al = rm.Allowlist(
        global_enabled=True,
        strategies=("forge_vix_intraday",),
        max_strategies_real=1,
        ledger_entry_id="abc-1",
        approver="ksmith2322",
    )
    monkeypatch.setattr(rm, "_capital_ladder_approved_capital_usd", lambda: 1_000.0)
    monkeypatch.setattr(rm, "_existing_real_money_notional_usd", lambda allowlist: 0.0)
    with pytest.raises(rm.AccountBoundaryViolationError, match="capital_ladder_oversize"):
        rm.enforce_real_money_boundary(
            ib,
            strategy_label="forge_vix_intraday",
            notional_usd=1_001.0,
            allowlist=al,
        )


def test_real_invalid_allowlist_rejects(real_money_module_enabled):
    ib = _FakeIB(port=rm.REAL_PORT)
    # global_enabled=True but missing approver/ledger — internally invalid.
    al = rm.Allowlist(
        global_enabled=True,
        strategies=("forge_vix_intraday",),
        max_strategies_real=1,
        ledger_entry_id="",
        approver="",
    )
    with pytest.raises(rm.AccountBoundaryViolationError, match="allowlist_invalid"):
        rm.enforce_real_money_boundary(
            ib, strategy_label="forge_vix_intraday", allowlist=al,
        )


# ---------------------------------------------------------------------------
# real_money_order_tag
# ---------------------------------------------------------------------------

def test_order_tag_includes_ledger_id():
    al = rm.Allowlist(
        global_enabled=True,
        strategies=("forge_vix_intraday",),
        ledger_entry_id="ledger-2026-05-30",
    )
    tag = rm.real_money_order_tag("forge_vix_intraday", allowlist=al)
    assert tag == "argus-real-forge_vix_intraday-ledger-2026-05-30"


def test_order_tag_marks_unsigned_when_ledger_missing():
    al = rm.Allowlist(global_enabled=False, strategies=(), ledger_entry_id="")
    tag = rm.real_money_order_tag("forge_vix_intraday", allowlist=al)
    assert "UNSIGNED" in tag


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

def test_module_default_is_off():
    assert rm.REAL_MONEY_ENABLED is False


def test_default_allowlist_file_is_paper_only():
    """The shipped allowlist scaffold must be paper-only by default."""
    al = rm.load_allowlist()  # default path
    assert al.global_enabled is False
    assert al.strategies == ()


# ---------------------------------------------------------------------------
# real-money mismatch detector
# ---------------------------------------------------------------------------

def test_mismatch_detector_flags_untagged_position():
    from helio.real_money_mismatch_daemon import scan_ibkr

    class _Contract:
        symbol = "SPY"

    class _Position:
        account = "U1234567"
        contract = _Contract()
        position = 3
        avgCost = 500.0

    class _IB:
        def positions(self):
            return [_Position()]

        def trades(self):
            return []

    positions, violations = scan_ibkr(_IB(), rm.Allowlist(global_enabled=True, strategies=("forge_vix_intraday",)))
    assert positions[0].symbol == "SPY"
    assert violations[0].code == "REAL_POSITION_TAG_MISSING"


def test_mismatch_detector_accepts_allowlisted_argus_real_tag():
    from helio.real_money_mismatch_daemon import scan_ibkr

    class _Contract:
        symbol = "UVXY"

    class _Position:
        account = "U1234567"
        contract = _Contract()
        position = 10
        avgCost = 20.0

    class _Order:
        orderRef = "argus-real-forge_vix_intraday-ledger-1"

    class _Trade:
        contract = _Contract()
        order = _Order()

    class _IB:
        def positions(self):
            return [_Position()]

        def trades(self):
            return [_Trade()]

    al = rm.Allowlist(global_enabled=True, strategies=("forge_vix_intraday",), ledger_entry_id="ledger-1")
    _, violations = scan_ibkr(_IB(), al)
    assert violations == []
