"""Strategy registry — typed, validated reader for config/strategies.json.

Phase 1: MIRROR ONLY. The registry exists so we have one file to diff when an
edge threshold changes, and one place the dashboard/review code can read from
to show "what are our current strategy knobs". Runners are NOT yet wired to
consume from here — they keep their in-module PARAMS dicts. Phase 2 migrates
them, one at a time, after equivalence tests prove no drift.

Usage:
    from helio.strategy_registry import load_registry, get_strategy

    reg = load_registry()
    gld = reg.strategies["forge_gld_pm_long"]
    assert gld.signal_hours_utc == [18, 19, 20]

Validation catches: missing fields, type mismatches, invalid values. If the
registry file is malformed, `load_registry()` raises — do not catch and fall
back; a bad registry should fail loudly.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_REPO = Path(__file__).resolve().parents[1]
REGISTRY_PATH = _REPO / "config" / "strategies.json"


class _Base(BaseModel):
    """Shared config: ignore the underscore-prefixed comment fields in JSON."""
    model_config = ConfigDict(extra="allow")  # tolerate _comment / _source keys


class Gate(_Base):
    min_trades: int = Field(ge=0)
    min_pf: float = Field(ge=0)
    min_expectancy: float


class PromotionConfig(_Base):
    review_gate: Gate
    canonical_gate: Gate


class ShortlistEntry(_Base):
    label: str
    card: str | None = None


class GldPmLongParams(_Base):
    family: Literal["forge"]
    symbol: str
    timeframe: str
    signal_hours_utc: list[int]
    atr_period: int = Field(gt=0)
    stop_atr: float = Field(gt=0)
    target_atr: float = Field(gt=0)
    hold_bars: int = Field(gt=0)


class WickGbpusdParams(_Base):
    family: Literal["forge"]
    symbol: str
    timeframe: str
    uw_min: float = Field(ge=0, le=1)
    cp_max: float = Field(ge=0, le=1)
    bb_width_quantile_max: float = Field(ge=0, le=1)
    chop_quantile_min: float = Field(ge=0, le=1)
    regime_window: int = Field(gt=0)
    atr_period: int = Field(gt=0)
    stop_atr: float = Field(gt=0)
    target_atr: float = Field(gt=0)
    hold_bars: int = Field(gt=0)


class GdxGldParams(_Base):
    family: Literal["forge"]
    symbols: list[str]
    timeframe: str
    zscore_entry: float = Field(gt=0)
    zscore_exit: float
    zscore_stop: float = Field(gt=0)
    zscore_lookback: int = Field(gt=0)
    coint_window: int = Field(gt=0)
    cost_per_side_bps: float = Field(ge=0)


class ApolloEarningsParams(_Base):
    family: Literal["apollo"]
    score_floor: int = Field(ge=0, le=100)
    horizon_trading_days: int = Field(gt=0)
    max_planned_per_symbol_per_earnings: int = Field(ge=1)
    mode_default: Literal["research_only", "paper", "live"]


class StrategyRegistry(_Base):
    version: str
    promotion: PromotionConfig
    shortlist: list[ShortlistEntry]
    strategies: dict  # validated per-strategy below

    # Typed accessors for the known strategies — keeps call sites tidy.
    @property
    def gld_pm_long(self) -> GldPmLongParams:
        return GldPmLongParams.model_validate(self.strategies["forge_gld_pm_long"])

    @property
    def wick_gbpusd(self) -> WickGbpusdParams:
        return WickGbpusdParams.model_validate(self.strategies["forge_wick_gbpusd"])

    @property
    def gdx_gld(self) -> GdxGldParams:
        """Backtest-parameterised gdx_gld (research values)."""
        return GdxGldParams.model_validate(self.strategies["forge_gdx_gld"])

    @property
    def gdx_gld_live(self) -> GdxGldParams:
        """Live-runner gdx_gld (diverges from backtest: zscore_stop=3.5 vs 3.0).
        See config/strategies.json '_divergence_note' for rationale."""
        return GdxGldParams.model_validate(self.strategies["forge_gdx_gld_live"])

    @property
    def apollo_earnings_drift(self) -> ApolloEarningsParams:
        return ApolloEarningsParams.model_validate(self.strategies["apollo_earnings_drift"])


@lru_cache(maxsize=1)
def load_registry(path: Path | None = None) -> StrategyRegistry:
    """Load + validate the strategy registry. Cached after first read.

    Pass an explicit path to bypass cache (useful for tests)."""
    p = path or REGISTRY_PATH
    raw = json.loads(p.read_text(encoding="utf-8"))
    return StrategyRegistry.model_validate(raw)


def get_strategy(label: str) -> dict:
    """Return the raw (dict) params for a strategy label. Falls through to
    KeyError if the label is unknown — caller should handle."""
    return load_registry().strategies[label]


def shortlist_labels() -> list[str]:
    return [s.label for s in load_registry().shortlist]


if __name__ == "__main__":
    # Quick smoke — run standalone to validate the on-disk JSON.
    reg = load_registry()
    print(f"Registry version: {reg.version}")
    print(f"Shortlist: {shortlist_labels()}")
    print(f"Strategies defined: {sorted(reg.strategies.keys())}")
    # Force typed accessors to run (catches shape drift)
    _ = reg.gld_pm_long
    _ = reg.wick_gbpusd
    _ = reg.gdx_gld
    _ = reg.apollo_earnings_drift
    print("All typed accessors validated OK.")
