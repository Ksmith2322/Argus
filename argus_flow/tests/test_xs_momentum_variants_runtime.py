"""Runtime tests for the xs_momentum --variant flag wiring.

The variant flag mutates module-level globals (STRATEGY_LABEL, LOG_DIR,
IBKR_CLIENT_ID, PARAMS["universe"]). These tests verify the wiring is
correct + that the variant registry stays in sync with the universe
registry and the data-feed contract.
"""
from __future__ import annotations

import pytest

from forge.xs_momentum import runner as xs_runner


def _reset_baseline():
    """Restore baseline globals after a test mutates them."""
    xs_runner.configure_variant("baseline")


@pytest.fixture(autouse=True)
def restore_baseline():
    yield
    _reset_baseline()


# ─── Registry contents ──────────────────────────────────────────────

def test_variant_registry_contains_four_variants():
    """baseline + 3 disciplined-gate survivors from the 20y sweep."""
    expected = {"baseline", "sectors", "style", "legacy15"}
    assert set(xs_runner.list_variants()) == expected


def test_each_variant_has_unique_client_id():
    """IBKR client IDs must not collide across variants."""
    ids = []
    for name in xs_runner.list_variants():
        cfg = xs_runner._VARIANT_REGISTRY[name]
        ids.append(cfg["client_id"])
    assert len(set(ids)) == len(ids), \
        f"client_id collision: {ids}"


def test_each_variant_has_unique_label_and_log_dir():
    labels = set()
    dirs = set()
    for name in xs_runner.list_variants():
        cfg = xs_runner._VARIANT_REGISTRY[name]
        labels.add(cfg["label"])
        dirs.add(cfg["log_dir_name"])
    assert len(labels) == len(xs_runner.list_variants())
    assert len(dirs) == len(xs_runner.list_variants())


# ─── Variant configuration ──────────────────────────────────────────

def test_baseline_variant_keeps_broad8_universe():
    xs_runner.configure_variant("baseline")
    assert xs_runner.STRATEGY_LABEL == "forge_xs_momentum"
    assert xs_runner.IBKR_CLIENT_ID == 121
    assert "SPY" in xs_runner.PARAMS["universe"]
    assert "QQQ" in xs_runner.PARAMS["universe"]
    assert len(xs_runner.PARAMS["universe"]) == 8


def test_sectors_variant_switches_to_spdr_universe():
    xs_runner.configure_variant("sectors")
    assert xs_runner.STRATEGY_LABEL == "forge_xs_momentum_sectors"
    assert xs_runner.IBKR_CLIENT_ID == 122
    assert "XLK" in xs_runner.PARAMS["universe"]
    assert "XLF" in xs_runner.PARAMS["universe"]
    # Should NOT contain non-sector tickers
    assert "EWJ" not in xs_runner.PARAMS["universe"]


def test_style_variant_switches_to_style_factor_universe():
    xs_runner.configure_variant("style")
    assert xs_runner.STRATEGY_LABEL == "forge_xs_momentum_style"
    assert xs_runner.IBKR_CLIENT_ID == 123
    assert "MTUM" in xs_runner.PARAMS["universe"]
    assert "QUAL" in xs_runner.PARAMS["universe"]


def test_legacy15_variant_switches_to_15_ticker_universe():
    xs_runner.configure_variant("legacy15")
    assert xs_runner.STRATEGY_LABEL == "forge_xs_momentum_legacy15"
    assert xs_runner.IBKR_CLIENT_ID == 124
    assert len(xs_runner.PARAMS["universe"]) == 15
    # Mix of US sectors and international ETFs
    assert "XLK" in xs_runner.PARAMS["universe"]
    assert "EWJ" in xs_runner.PARAMS["universe"]


def test_log_dir_paths_change_per_variant():
    xs_runner.configure_variant("sectors")
    assert "xs_momentum_sectors" in str(xs_runner.LOG_DIR)
    assert "xs_momentum_sectors" in str(xs_runner.STATE_PATH)
    assert "xs_momentum_sectors" in str(xs_runner.HEARTBEAT_PATH)
    assert "xs_momentum_sectors" in str(xs_runner.TRADES_PATH)


def test_unknown_variant_raises():
    with pytest.raises(ValueError) as exc_info:
        xs_runner.configure_variant("does_not_exist")
    assert "unknown" in str(exc_info.value).lower()
    assert "does_not_exist" in str(exc_info.value)


def test_configure_variant_is_idempotent():
    """Same variant called twice should not error or shift state."""
    xs_runner.configure_variant("style")
    label1 = xs_runner.STRATEGY_LABEL
    xs_runner.configure_variant("style")
    assert xs_runner.STRATEGY_LABEL == label1


# ─── Cross-registry consistency ─────────────────────────────────────

def test_every_variant_universe_resolves_via_xs_momentum_universes():
    """Each non-baseline variant must reference a key that exists in
    helio.xs_momentum_universes."""
    from helio.xs_momentum_universes import CANDIDATE_UNIVERSES
    for name, cfg in xs_runner._VARIANT_REGISTRY.items():
        if cfg["universe_key"] is None:
            continue
        assert cfg["universe_key"] in CANDIDATE_UNIVERSES, (
            f"variant {name!r} references unknown universe "
            f"{cfg['universe_key']!r}"
        )


def test_every_variant_in_data_feed_contract_registry():
    """Each variant strategy_label must appear in
    helio.data_feed_contract.CONTRACTS so the pre-market check can
    verify its data feed."""
    from helio.data_feed_contract import CONTRACTS
    for name, cfg in xs_runner._VARIANT_REGISTRY.items():
        assert cfg["label"] in CONTRACTS, (
            f"variant {name!r} (label={cfg['label']!r}) missing from "
            f"helio.data_feed_contract.CONTRACTS"
        )


def test_every_variant_in_allocation_factors():
    """Each variant strategy_label must appear in
    allocation_factors.json — otherwise it wouldn't deploy capital.
    Variants are set to 0.5x for paper-evidence accumulation."""
    import json
    from pathlib import Path
    repo = Path(__file__).resolve().parents[2]
    factors = json.loads(
        (repo / "argus_flow" / "configs" / "allocation_factors.json")
        .read_text(encoding="utf-8")
    )["factors"]
    for name, cfg in xs_runner._VARIANT_REGISTRY.items():
        if name == "baseline":
            continue  # baseline allocation already covered elsewhere
        assert cfg["label"] in factors, (
            f"variant {name!r} (label={cfg['label']!r}) missing from "
            f"allocation_factors.json"
        )
        assert factors[cfg["label"]] > 0, (
            f"variant {cfg['label']!r} has allocation 0 — won't paper-"
            f"trade for evidence"
        )


def test_every_variant_in_active_roster():
    """Each variant strategy_label must appear in ACTIVE_ROSTER so the
    sunset-roster test pin passes."""
    from argus_flow.tests.test_sunset_roster import ACTIVE_ROSTER
    for name, cfg in xs_runner._VARIANT_REGISTRY.items():
        if name == "baseline":
            continue  # baseline already in ACTIVE_ROSTER
        assert cfg["label"] in ACTIVE_ROSTER
