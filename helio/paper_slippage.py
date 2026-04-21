"""Synthetic slippage model for paper-mode fills.

Paper mode in argus_flow/runner_unified defaults to setting entry_price =
signal_mid (no broker hit). That gives zero slippage in trades.csv, which
inflates reported expectancy vs what real execution would deliver. This
module provides an OPT-IN slippage injection so paper fills carry a
realistic execution-friction haircut without needing the IBKR paper
simulator.

Model: half-spread adverse + random noise (0.2-1.5 pips adverse by default,
configurable per pair). Adverse direction: long fills slip UP, short fills
slip DOWN. This is the typical market-order behavior.

Usage (runner integration — opt-in per pair via config):
    from helio.paper_slippage import apply_entry_slippage, apply_exit_slippage

    # When paper entry fills:
    filled_px = apply_entry_slippage(
        signal_mid=signal_px, direction="long", symbol="GBPUSD",
        pip_size=0.0001, model="half_spread_plus_noise"
    )

    # When paper exit fills (crossing the spread back):
    filled_px = apply_exit_slippage(signal_mid=exit_px, direction="long", ...)

Config (future: per-pair override in fleet_sizing.json or a dedicated file):
    PAPER_SLIPPAGE_DEFAULT_PIPS = 0.8  # typical spread haircut
    PAPER_SLIPPAGE_NOISE_PIPS = 0.5   # +/- random jitter

Not wired into runner_unified by default (opt-in). Enabling it per pair is
a cohort-reset decision (changes the paper fill distribution).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Literal


# Per-symbol typical spread (pips) for the major pairs. Conservative upper
# bound for a retail paper account. Adjust if you want tighter/wider sim.
DEFAULT_SYMBOL_SPREADS_PIPS = {
    "USDJPY": 0.8,
    "GBPUSD": 1.0,
    "CADJPY": 1.2,
    "EURUSD": 0.6,
    "AUDUSD": 0.9,
    "EURJPY": 1.1,
    "GBPJPY": 1.6,
}

DEFAULT_NOISE_PIPS = 0.3  # +/- uniform noise on top of half-spread


SlippageModel = Literal[
    "none",                    # no slippage, paper fills at signal_mid (old behavior)
    "half_spread",             # always cross half the spread, deterministic
    "half_spread_plus_noise",  # half-spread + uniform noise (realistic default)
    "worst_case",              # full spread adverse (stress test)
]


@dataclass
class SlippageSpec:
    """Per-call slippage configuration."""
    model: SlippageModel = "half_spread_plus_noise"
    spread_pips: float | None = None  # None = lookup from symbol
    noise_pips: float = DEFAULT_NOISE_PIPS
    seed: int | None = None  # deterministic tests pass an int


def _spread_pips_for(symbol: str, spec: SlippageSpec) -> float:
    if spec.spread_pips is not None:
        return spec.spread_pips
    return DEFAULT_SYMBOL_SPREADS_PIPS.get(symbol.upper(), 1.0)


def _apply_model(signal_mid: float, direction: str, symbol: str,
                 pip_size: float, spec: SlippageSpec, is_entry: bool) -> float:
    """Compute slipped price.

    Long entry: pay the ask (above mid) → add slippage.
    Long exit:  receive the bid (below mid) → subtract slippage.
    Short entry: receive the bid → subtract slippage.
    Short exit:  pay the ask → add slippage.
    """
    if spec.model == "none":
        return signal_mid

    spread_p = _spread_pips_for(symbol, spec)
    rng = random.Random(spec.seed) if spec.seed is not None else random

    if spec.model == "half_spread":
        slip_pips = spread_p / 2.0
    elif spec.model == "half_spread_plus_noise":
        slip_pips = (spread_p / 2.0) + rng.uniform(0, spec.noise_pips)
    elif spec.model == "worst_case":
        slip_pips = spread_p
    else:
        raise ValueError(f"unknown slippage model: {spec.model}")

    slip_px = slip_pips * pip_size
    long_side = direction.upper() == "LONG"

    # Adverse direction: long entry/short exit pays up; short entry/long exit pays down
    if is_entry:
        return signal_mid + slip_px if long_side else signal_mid - slip_px
    else:  # exit
        return signal_mid - slip_px if long_side else signal_mid + slip_px


def apply_entry_slippage(signal_mid: float, direction: str, symbol: str,
                         pip_size: float = 0.0001,
                         spec: SlippageSpec | None = None) -> float:
    return _apply_model(signal_mid, direction, symbol, pip_size,
                        spec or SlippageSpec(), is_entry=True)


def apply_exit_slippage(signal_mid: float, direction: str, symbol: str,
                        pip_size: float = 0.0001,
                        spec: SlippageSpec | None = None) -> float:
    return _apply_model(signal_mid, direction, symbol, pip_size,
                        spec or SlippageSpec(), is_entry=False)


def slippage_pips(signal_mid: float, filled_px: float, direction: str,
                  pip_size: float = 0.0001) -> float:
    """Compute the adverse slippage in pips between signal and fill.

    Positive = adverse to the trade (filled worse than signal).
    Used for populating the `slippage_pips` column in trades.csv.
    """
    long_side = direction.upper() == "LONG"
    if long_side:
        # entry: filled > mid is adverse; exit: filled < mid is adverse
        # We just report the absolute adverse distance
        return abs(filled_px - signal_mid) / pip_size
    else:
        return abs(filled_px - signal_mid) / pip_size


# ------------------ Self-test ------------------
if __name__ == "__main__":
    # Deterministic with seed
    spec = SlippageSpec(model="half_spread_plus_noise", seed=42)

    # GBPUSD long entry at 1.25000 should fill higher
    long_entry = apply_entry_slippage(1.25000, "LONG", "GBPUSD", 0.0001, spec)
    assert long_entry > 1.25000, f"long entry should slip up: {long_entry}"

    # GBPUSD short entry at 1.25000 should fill lower
    short_entry = apply_entry_slippage(1.25000, "SHORT", "GBPUSD", 0.0001, SlippageSpec(model="half_spread_plus_noise", seed=42))
    assert short_entry < 1.25000, f"short entry should slip down: {short_entry}"

    # half_spread model is deterministic: GBPUSD half of 1.0 = 0.5 pips = 0.00005
    hs = apply_entry_slippage(1.25000, "LONG", "GBPUSD", 0.0001, SlippageSpec(model="half_spread"))
    assert abs(hs - 1.250050) < 1e-9, f"half_spread exact: {hs}"

    # worst_case = full spread
    wc = apply_entry_slippage(1.25000, "LONG", "GBPUSD", 0.0001, SlippageSpec(model="worst_case"))
    assert abs(wc - 1.250100) < 1e-9, f"worst_case exact: {wc}"

    # none = no change
    no = apply_entry_slippage(1.25000, "LONG", "GBPUSD", 0.0001, SlippageSpec(model="none"))
    assert no == 1.25000

    print("All slippage model self-tests pass.")
    print(f"  GBPUSD long half+noise seed42: {long_entry:.5f} (slipped {(long_entry-1.25)/0.0001:.2f} pips)")
    print(f"  GBPUSD short half+noise seed42: {short_entry:.5f} (slipped {(1.25-short_entry)/0.0001:.2f} pips)")
    print(f"  GBPUSD long half_spread: {hs:.5f}")
    print(f"  GBPUSD long worst_case: {wc:.5f}")
