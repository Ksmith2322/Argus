"""Tests for helio.xs_momentum_variants + xs_momentum_universes."""
from __future__ import annotations

import pytest

from helio.xs_momentum_universes import (
    CANDIDATE_UNIVERSES,
    SECTORS_SPDR_11,
    COMMODITIES_7,
    COUNTRIES_10,
    get_universe,
)
from helio.xs_momentum_variants import (
    MULTI_HORIZON_LOOKBACKS,
    SizedPick,
    VARIANTS,
    _trailing_volatility,
    rank_universe_multi_horizon,
    size_picks_equal_weight,
    size_picks_vol_scaled,
)
from helio.xs_momentum import MomentumScore


# ─── Universe registry ──────────────────────────────────────────────

def test_every_registered_universe_is_a_nonempty_tuple_of_strings():
    for name, tickers in CANDIDATE_UNIVERSES.items():
        assert isinstance(tickers, tuple), name
        assert len(tickers) > 0, name
        assert all(isinstance(t, str) and t for t in tickers), name


def test_get_universe_returns_registered_tuple():
    assert get_universe("sectors_spdr_11") == SECTORS_SPDR_11
    assert get_universe("commodities_7") == COMMODITIES_7
    assert get_universe("countries_10") == COUNTRIES_10


def test_get_universe_raises_for_unknown_name():
    with pytest.raises(KeyError):
        get_universe("does_not_exist")


def test_universe_tickers_unique_within_each_universe():
    for name, tickers in CANDIDATE_UNIVERSES.items():
        assert len(set(tickers)) == len(tickers), \
            f"{name} has duplicate tickers"


# ─── Multi-horizon ranker ───────────────────────────────────────────

def _synth_closes(slope: float, n: int = 300) -> list[float]:
    """Synthetic close series with constant log-slope (so 12-1 = slope)."""
    return [100.0 * (1.0 + slope) ** (i / 252.0) for i in range(n)]


def test_multi_horizon_ranks_higher_for_steeper_trend():
    fast = _synth_closes(0.30)   # 30% annualized
    flat = _synth_closes(0.02)
    down = _synth_closes(-0.10)
    ranked = rank_universe_multi_horizon({
        "FAST": fast,
        "FLAT": flat,
        "DOWN": down,
    })
    tickers = [r.ticker for r in ranked]
    assert tickers == ["FAST", "FLAT", "DOWN"]


def test_multi_horizon_drops_assets_with_insufficient_history():
    short = [100.0] * 100   # less than long_lookback for 12-1 horizon
    long_enough = _synth_closes(0.20)
    ranked = rank_universe_multi_horizon({
        "SHORT": short, "LONG": long_enough,
    })
    assert [r.ticker for r in ranked] == ["LONG"]


def test_multi_horizon_uses_three_lookbacks():
    assert len(MULTI_HORIZON_LOOKBACKS) == 3
    # First must be the canonical 12-1
    assert MULTI_HORIZON_LOOKBACKS[0] == (252, 21)


# ─── Trailing volatility ────────────────────────────────────────────

def test_trailing_volatility_returns_none_for_short_series():
    assert _trailing_volatility([100.0] * 10, window_days=60) is None


def test_trailing_volatility_zero_for_constant_series():
    vol = _trailing_volatility([100.0] * 100, window_days=60)
    assert vol is not None
    assert vol == pytest.approx(0.0, abs=1e-9)


def test_trailing_volatility_positive_for_noisy_series():
    import random
    random.seed(42)
    closes = [100.0]
    for _ in range(100):
        closes.append(closes[-1] * (1.0 + random.gauss(0, 0.01)))
    vol = _trailing_volatility(closes, window_days=60)
    assert vol is not None
    assert vol > 0


# ─── Equal-weight sizing ────────────────────────────────────────────

def test_equal_weight_sizing_distributes_evenly():
    picks = [
        MomentumScore("A", 0.5, 100, 80, 95),
        MomentumScore("B", 0.3, 100, 80, 95),
        MomentumScore("C", 0.1, 100, 80, 95),
    ]
    sized = size_picks_equal_weight(picks)
    assert len(sized) == 3
    assert all(s.weight == pytest.approx(1.0 / 3.0) for s in sized)
    # Weights sum to 1.0
    assert sum(s.weight for s in sized) == pytest.approx(1.0)


def test_equal_weight_sizing_handles_empty_input():
    assert size_picks_equal_weight([]) == []


# ─── Vol-scaled sizing ──────────────────────────────────────────────

def test_vol_scaled_overweights_low_vol_pick():
    """High-vol pick should get LESS weight than low-vol pick."""
    import random
    random.seed(0)
    # Two synthetic series: one quiet, one noisy
    quiet = [100.0]
    for _ in range(100):
        quiet.append(quiet[-1] * (1.0 + random.gauss(0, 0.002)))
    noisy = [100.0]
    for _ in range(100):
        noisy.append(noisy[-1] * (1.0 + random.gauss(0, 0.03)))

    picks = [
        MomentumScore("QUIET", 0.5, quiet[-1], 100, quiet[-21]),
        MomentumScore("NOISY", 0.5, noisy[-1], 100, noisy[-21]),
    ]
    closes_by = {"QUIET": quiet, "NOISY": noisy}
    sized = size_picks_vol_scaled(picks, closes_by, vol_window_days=60)
    by_ticker = {s.ticker: s.weight for s in sized}
    assert by_ticker["QUIET"] > by_ticker["NOISY"], (
        "vol-scaled sizing should overweight the lower-vol pick"
    )
    assert sum(by_ticker.values()) == pytest.approx(1.0)


def test_vol_scaled_falls_back_to_equal_when_missing_closes():
    picks = [MomentumScore("A", 0.5, 100, 80, 95)]
    sized = size_picks_vol_scaled(picks, {}, vol_window_days=60)
    assert len(sized) == 1
    assert sized[0].weight == pytest.approx(1.0)


def test_vol_scaled_floor_caps_extreme_weights():
    """If a pick has near-zero vol, the floor should cap its weight
    instead of letting it dominate."""
    import random
    random.seed(0)
    near_zero_vol = [100.0001 * (1 + 0.0000001 * i) for i in range(100)]
    normal = [100.0]
    for _ in range(100):
        normal.append(normal[-1] * (1.0 + random.gauss(0, 0.015)))
    picks = [
        MomentumScore("FLAT", 0.5, near_zero_vol[-1], 100, near_zero_vol[-21]),
        MomentumScore("NORM", 0.5, normal[-1], 100, normal[-21]),
    ]
    closes_by = {"FLAT": near_zero_vol, "NORM": normal}
    sized = size_picks_vol_scaled(picks, closes_by, floor_vol=0.05)
    weights = {s.ticker: s.weight for s in sized}
    # FLAT should still get the larger share, but not >95%
    assert weights["FLAT"] < 0.95, weights


def test_vol_scaled_handles_empty_picks():
    assert size_picks_vol_scaled([], {}) == []


# ─── Variant catalog ────────────────────────────────────────────────

def test_variant_catalog_has_baseline():
    assert "v1_baseline" in VARIANTS
    assert VARIANTS["v1_baseline"]["ranker"] == "single_12_1"
    assert VARIANTS["v1_baseline"]["sizer"] == "equal_weight"


def test_variant_catalog_has_four_variants():
    assert len(VARIANTS) == 4
    for name, cfg in VARIANTS.items():
        assert "ranker" in cfg
        assert "sizer" in cfg


def test_sized_pick_is_a_dataclass():
    p = SizedPick("X", 0.5, 0.25)
    assert p.ticker == "X"
    assert p.score == 0.5
    assert p.weight == 0.25
