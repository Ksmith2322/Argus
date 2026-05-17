"""Tests for helio.ibkr_execution.make_contract exchange routing.

Background (2026-05-16): CME Group splits futures products across CME and
CBOT exchanges. NQ/MNQ (Nasdaq) and ES/MES (S&P) route to CME; YM/MYM (Dow)
and RTY/M2K (Russell 2000) route to CBOT. Sending a Dow/Russell contract
with exchange="CME" causes TWS to SILENTLY cancel orders — no errorCode,
no whyHeld, no trade.log entry. This bug kept tori/cuebanks/mamba at zero
fills across 22+ days of MYM signals.
"""
from __future__ import annotations

from helio import ibkr_execution as ie


def test_mnq_routes_to_cme():
    c = ie.make_contract("MNQ", "micro_future")
    assert c.symbol == "MNQ"
    assert c.exchange == "CME"


def test_mes_routes_to_cme():
    c = ie.make_contract("MES", "micro_future")
    assert c.exchange == "CME"


def test_nq_routes_to_cme():
    c = ie.make_contract("NQ", "future")
    assert c.exchange == "CME"


def test_es_routes_to_cme():
    c = ie.make_contract("ES", "future")
    assert c.exchange == "CME"


def test_mym_routes_to_cbot():
    """The bug fix: MYM was silently failing with exchange=CME. Must be CBOT."""
    c = ie.make_contract("MYM", "micro_future")
    assert c.symbol == "MYM"
    assert c.exchange == "CBOT", f"MYM must route to CBOT, got {c.exchange}"


def test_m2k_routes_to_cbot():
    c = ie.make_contract("M2K", "micro_future")
    assert c.exchange == "CBOT", f"M2K must route to CBOT, got {c.exchange}"


def test_ym_routes_to_cbot():
    """Full-size Dow contract also CBOT-listed."""
    c = ie.make_contract("YM", "future")
    assert c.exchange == "CBOT"


def test_rty_routes_to_cbot():
    """Full-size Russell 2000 contract CBOT-listed."""
    c = ie.make_contract("RTY", "future")
    assert c.exchange == "CBOT"


def test_lowercase_symbol_normalized():
    """Defensive: case-insensitive symbol matching."""
    c = ie.make_contract("mym", "micro_future")
    assert c.exchange == "CBOT"


def test_stock_routing_unchanged():
    c = ie.make_contract("GLD", "etf")
    assert c.exchange == "SMART"


def test_fx_routing_unchanged():
    c = ie.make_contract("EURUSD", "fx")
    # ib_insync Forex contract has secType="CASH"
    assert c.symbol == "EUR"  # Forex splits the pair


def test_unknown_instrument_type_raises():
    import pytest
    with pytest.raises(ie.IBKRExecutionError):
        ie.make_contract("FOO", "crypto")
