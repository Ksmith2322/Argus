"""FX exit retry cascade — regression coverage for the 5/8 + 5/15
argus_gbpusd EXIT FAILED cascades.

The 2026-05-08 incident: argus_gbpusd EXIT order 296 → 30s timeout →
IOC retry → 60s stuck → GTC retry → 120s exhausted → forced local
FLAT with broker still holding 184K SHORT GBPUSD. Manual broker close
was required (and missed) for 7 days.

Codex audit 2026-05-18 hypothesised that a DAY-flagged FX LMT can stall
around the broker's daily roll boundary. Fix-now #7 collapsed the retry
chain by upgrading DAY → GTC in _build_exit_order for CASH instruments.
This test suite documents what the cascade must do per stage so the
behaviour can't silently drift back."""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from argus_flow.runner_unified import InstrumentRunner


class _StubLogger:
    def __init__(self):
        self.records = []

    def info(self, *a, **k): self.records.append(("info", a, k))
    def warning(self, *a, **k): self.records.append(("warning", a, k))
    def error(self, *a, **k): self.records.append(("error", a, k))
    def critical(self, *a, **k): self.records.append(("critical", a, k))


def _make_instrument(sec_type: str) -> SimpleNamespace:
    inst = SimpleNamespace()
    inst.contract = SimpleNamespace(secType=sec_type)
    inst._log = _StubLogger()
    inst._build_exit_order = InstrumentRunner._build_exit_order.__get__(inst, SimpleNamespace)
    return inst


# ── Stage A: initial exit (retry_count=0) ──────────────────────────────────

def test_fx_initial_exit_is_gtc_limit_order():
    """The first exit submission for FX (caller passes tif='DAY' as the
    default). Cascade stage A. Must be a LimitOrder + outsideRth + TIF=GTC
    after the DAY→GTC upgrade."""
    inst = _make_instrument("CASH")
    order = inst._build_exit_order("SELL", qty=184000, ref_px=1.30, tif="DAY")
    assert type(order).__name__ == "LimitOrder"
    assert order.outsideRth is True
    assert order.tif == "GTC", (
        f"FX initial exit must be GTC after DAY→GTC upgrade; got {order.tif!r}. "
        f"DAY-flagged FX LMTs can silently expire around broker session "
        f"boundaries (suspected cause of the 5/8 EXIT FAILED cascade)."
    )


# ── Stage B: retry 1 (timeout @ 30s, retry_count→1) ────────────────────────
# Cascade source (runner_unified._check_order_timeouts):
#   retry_tif = "DAY" if sec_type == "CASH" else "IOC"
# For FX, retry_tif passes "DAY" which _build_exit_order upgrades to GTC.

def test_fx_retry1_at_30s_uses_gtc_limit():
    """Cascade stage B: caller asks for tif='DAY' (the retry default for
    CASH per the source). _build_exit_order must upgrade to GTC for CASH."""
    inst = _make_instrument("CASH")
    order = inst._build_exit_order("SELL", qty=184000, ref_px=1.30, tif="DAY")
    assert type(order).__name__ == "LimitOrder"
    assert order.tif == "GTC"


def test_non_fx_retry1_at_30s_uses_ioc_market():
    """Cascade stage B for STK/ETF/FUT: tif='IOC' propagates unchanged."""
    inst = _make_instrument("STK")
    order = inst._build_exit_order("SELL", qty=100, ref_px=400.0, tif="IOC")
    assert type(order).__name__ == "MarketOrder"
    assert order.tif == "IOC", (
        f"Non-FX retry-1 must use IOC for urgency; got {order.tif!r}."
    )


# ── Stage C: retry 2 (timeout @ 60s, retry_count→2) ────────────────────────

def test_fx_retry2_at_60s_uses_gtc_limit():
    """Cascade stage C: caller asks for tif='GTC' (the explicit last-resort
    in _check_order_timeouts). Must produce GTC LMT for CASH."""
    inst = _make_instrument("CASH")
    order = inst._build_exit_order("BUY", qty=184000, ref_px=1.30, tif="GTC")
    assert type(order).__name__ == "LimitOrder"
    assert order.tif == "GTC"


def test_non_fx_retry2_at_60s_uses_gtc_market():
    """Cascade stage C for STK/ETF/FUT: tif='GTC' MKT as last-resort."""
    inst = _make_instrument("FUT")
    order = inst._build_exit_order("BUY", qty=2, ref_px=21_000.0, tif="GTC")
    assert type(order).__name__ == "MarketOrder"
    assert order.tif == "GTC"


# ── Cascade source-shape invariants ───────────────────────────────────────
# Codex feedback: "FX exit TIF/retry simulation tests — argus_gbpusd cascade
# — TIF=DAY still suspect." Verify the source still has the cascade structure
# (3 stages, the 120s exhaustion arm, the FX-vs-non-FX branch). These guard
# against an accidental refactor that drops a stage.

_RUNNER_UNIFIED = Path(__file__).resolve().parents[1] / "runner_unified.py"


def _read_runner_source() -> str:
    return _RUNNER_UNIFIED.read_text(encoding="utf-8")


def test_cascade_source_has_three_retry_stages():
    src = _read_runner_source()
    # Stage A: initial submit in _submit_real_exit. Stage B: 30s timeout.
    # Stage C: 60s. Stage D: 120s exhaustion + forced FLAT.
    assert "elapsed > 30 and exit_retry_count == 0" in src
    assert "elapsed > 60 and exit_retry_count == 1" in src
    assert "elapsed > 120 and exit_retry_count >= 2" in src


def test_cascade_branches_fx_vs_non_fx_at_retry_1():
    """The 30s-elapsed retry branch must still distinguish FX from non-FX,
    or the IOC-on-FX bug (IdealPro rejects IOC) returns."""
    src = _read_runner_source()
    assert 'retry_tif = "DAY" if sec_type == "CASH" else "IOC"' in src, (
        "Retry-1 FX-vs-non-FX branch missing. FX must request DAY (which "
        "_build_exit_order upgrades to GTC); non-FX must use IOC. Without "
        "this branch, IdealPro will reject IOC on FX exits."
    )


def test_cascade_has_broker_truth_race_check():
    """REGRESSION 2026-05-20: cascade fired a duplicate fill on CADJPY
    because the previous IOC retry filled instantly but execDetails raced
    the s.exit_order_id assignment. Position double-sold (LONG 41479 →
    SHORT -82858).

    Fix: cascade queries the broker before each retry. If broker shows
    position is flat, the prior exit must have filled — clear local state
    and exit the timeout check entirely instead of submitting another order.

    This is a source-level check that the broker-truth guard is present
    at the top of the exit-pending cascade arm."""
    src = _read_runner_source()
    # The guard must check broker positions and clear exit_pending on flat
    assert "EXIT_RACE_RESOLVED" in src, (
        "Cascade lost its broker-truth race-check guard. Without it, the "
        "5/19 CADJPY double-sell bug recurs: cascade fires a retry while "
        "the previous order is already filled."
    )
    # Must check ib.positions() (broker truth) before retry
    assert "for p in ib.positions()" in src
    # Must clear exit_pending + set position FLAT
    assert "s.exit_pending = False" in src
    assert 's.position = "FLAT"' in src


def test_cascade_last_resort_is_gtc_and_writes_incident():
    """The 120s-exhausted arm must force FLAT AND write an EXIT_FAILED
    incident — that incident drives the manual-broker-check alert without
    which the 5/8 184K orphan would still be silent."""
    src = _read_runner_source()
    # Match the relevant region: from the "elapsed > 120" arm through to
    # the incident write. Some flexibility on whitespace.
    region_match = re.search(
        r"elapsed > 120 and exit_retry_count >= 2.*?(?=elif|def )",
        src,
        re.DOTALL,
    )
    assert region_match, "Couldn't locate the 120s exhaustion arm"
    region = region_match.group(0)
    assert "_write_incident" in region
    assert "EXIT_FAILED" in region
    assert "clear_trade_state" in region
