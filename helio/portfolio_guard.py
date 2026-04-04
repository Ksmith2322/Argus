"""
Cross-family portfolio risk aggregation.

Reads position state from ALL families (Argus FX, Helio swing, Hermes momentum,
Apollo mean reversion) and enforces portfolio-level limits.

CLI:  python -m helio.portfolio_guard
      python -m helio.portfolio_guard --check hermes XAUUSD LONG
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent

POSITION_SOURCES: Dict[str, str] = {
    "argus_fx":      str(REPO_ROOT / "argus_flow" / "logs" / "*" / "state.json"),
    "helio_swing":   str(REPO_ROOT / "helio" / "logs" / "{dia,gld,spy,mes,mgc,mnq,mym}" / "state.json"),
    "apollo":        str(REPO_ROOT / "helio" / "logs" / "apollo_*" / "state.json"),
    "hermes":        str(REPO_ROOT / "helio" / "logs" / "hermes_*" / "state.json"),
}

OUTPUT_PATH = REPO_ROOT / "helio" / "logs" / "portfolio_guard.json"

# ---------------------------------------------------------------------------
# Default limits
# ---------------------------------------------------------------------------
DEFAULT_MAX_TOTAL_POSITIONS   = 6
DEFAULT_MAX_PER_FAMILY        = 3
DEFAULT_MAX_DIRECTIONAL_BIAS  = 4
DEFAULT_MAX_CORRELATED_PAIRS  = 2

# ---------------------------------------------------------------------------
# Helpers - normalise instrument names for overlap detection
# ---------------------------------------------------------------------------
_SYMBOL_ALIASES = {
    # Map variations to a canonical form
    "eur_usd": "eurusd", "eur/usd": "eurusd",
    "gbp_usd": "gbpusd", "gbp/usd": "gbpusd",
    "usd_jpy": "usdjpy", "usd/jpy": "usdjpy",
    "eur_jpy": "eurjpy", "eur/jpy": "eurjpy",
    "gbp_jpy": "gbpjpy", "gbp/jpy": "gbpjpy",
    "aud_usd": "audusd", "aud/usd": "audusd",
    "aud_jpy": "audjpy", "aud/jpy": "audjpy",
    "cad_jpy": "cadjpy", "cad/jpy": "cadjpy",
    "xau_usd": "xauusd", "xau/usd": "xauusd", "gold_f": "xauusd",
}


def _normalise_symbol(raw: str) -> str:
    """Lowercase, strip underscores/slashes, apply alias table."""
    s = raw.lower().strip()
    if s in _SYMBOL_ALIASES:
        return _SYMBOL_ALIASES[s]
    # Strip common separators
    for ch in ("_", "/", "-"):
        s = s.replace(ch, "")
    return s


def _symbol_from_path(path: str, family: str) -> str:
    """Extract the instrument symbol from the state.json path."""
    parent = os.path.basename(os.path.dirname(path))
    # Apollo / Hermes dirs are prefixed: apollo_eurusd, hermes_gold_f
    if family in ("apollo", "hermes"):
        prefix = family + "_"
        if parent.startswith(prefix):
            return parent[len(prefix):]
    return parent


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class OpenPosition:
    family: str
    symbol: str           # raw directory name
    symbol_norm: str      # normalised for overlap detection
    direction: str        # LONG or SHORT
    entry_price: float
    state_path: str


@dataclass
class PortfolioCheck:
    allowed: bool
    reason: str
    metrics: dict
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core scanning
# ---------------------------------------------------------------------------

def _expand_glob(pattern: str) -> List[str]:
    """Expand a glob pattern (with brace expansion done manually for Windows)."""
    # Python glob doesn't support {a,b} syntax; expand manually
    if "{" in pattern and "}" in pattern:
        prefix, rest = pattern.split("{", 1)
        options, suffix = rest.split("}", 1)
        paths: List[str] = []
        for opt in options.split(","):
            paths.extend(glob.glob(prefix + opt + suffix))
        return paths
    return glob.glob(pattern)


def _load_position(path: str, family: str) -> Optional[OpenPosition]:
    """Read a state.json and return an OpenPosition if non-FLAT, else None."""
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    pos = data.get("position", "FLAT")
    if pos == "FLAT" or not pos:
        return None

    symbol_raw = _symbol_from_path(path, family)
    direction = pos  # LONG or SHORT

    return OpenPosition(
        family=family,
        symbol=symbol_raw,
        symbol_norm=_normalise_symbol(symbol_raw),
        direction=direction,
        entry_price=data.get("entry_price", 0.0),
        state_path=path,
    )


# Exclude internal/meta directories in argus_flow/logs that are not instruments
_ARGUS_SKIP_DIRS = {
    "_broker", "_locks", "_risk", "_scratch_weekly_onboarding",
    "_test_fault", "_test_fault2", "_test_fault3", "_tmp_pair_onboarding",
    "degradation", "drift",
}


def scan_all_positions() -> List[OpenPosition]:
    """Scan every position source and return list of open positions."""
    positions: List[OpenPosition] = []

    for family, pattern in POSITION_SOURCES.items():
        paths = _expand_glob(pattern)
        for p in paths:
            # Skip known non-instrument dirs for argus_fx
            if family == "argus_fx":
                dirname = os.path.basename(os.path.dirname(p))
                if dirname.startswith("_") or dirname in _ARGUS_SKIP_DIRS:
                    continue
            op = _load_position(p, family)
            if op is not None:
                positions.append(op)

    return positions


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------

def compute_metrics(positions: List[OpenPosition]) -> dict:
    """Compute portfolio-level metrics from open positions."""
    total = len(positions)

    # Positions by family
    by_family: Dict[str, int] = {}
    for p in positions:
        by_family[p.family] = by_family.get(p.family, 0) + 1

    # Directional bias
    longs = sum(1 for p in positions if p.direction == "LONG")
    shorts = sum(1 for p in positions if p.direction == "SHORT")
    directional_bias = longs - shorts  # positive = net long

    # Concentration
    concentration = 0.0
    if total > 0:
        concentration = max(by_family.values()) / total

    # Correlated exposure: instruments held by >1 family
    instrument_families: Dict[str, set] = {}
    for p in positions:
        instrument_families.setdefault(p.symbol_norm, set()).add(p.family)

    correlated = {
        sym: sorted(fams)
        for sym, fams in instrument_families.items()
        if len(fams) > 1
    }

    return {
        "total_open_positions": total,
        "positions_by_family": by_family,
        "longs": longs,
        "shorts": shorts,
        "directional_bias": directional_bias,
        "concentration": round(concentration, 3),
        "correlated_exposure": correlated,
        "correlated_pair_count": len(correlated),
        "positions_detail": [
            {"family": p.family, "symbol": p.symbol, "direction": p.direction}
            for p in positions
        ],
    }


# ---------------------------------------------------------------------------
# Limit enforcement
# ---------------------------------------------------------------------------

def check_limits(
    metrics: dict,
    *,
    max_total: int = DEFAULT_MAX_TOTAL_POSITIONS,
    max_per_family: int = DEFAULT_MAX_PER_FAMILY,
    max_bias: int = DEFAULT_MAX_DIRECTIONAL_BIAS,
    max_correlated: int = DEFAULT_MAX_CORRELATED_PAIRS,
) -> Tuple[bool, str, List[str]]:
    """
    Check portfolio metrics against limits.
    Returns (allowed, reason, warnings).
    """
    warnings: List[str] = []
    total = metrics["total_open_positions"]

    # Hard blocks
    if total >= max_total:
        return False, f"max_total_positions reached ({total}/{max_total})", warnings

    for fam, count in metrics["positions_by_family"].items():
        if count >= max_per_family:
            return False, f"max_per_family reached for {fam} ({count}/{max_per_family})", warnings

    abs_bias = abs(metrics["directional_bias"])
    if abs_bias >= max_bias:
        direction_word = "LONG" if metrics["directional_bias"] > 0 else "SHORT"
        return False, f"max_directional_bias reached ({abs_bias} net {direction_word}, limit {max_bias})", warnings

    if metrics["correlated_pair_count"] >= max_correlated:
        return False, f"max_correlated_pairs reached ({metrics['correlated_pair_count']}/{max_correlated})", warnings

    # Near-limit warnings
    if total >= max_total - 1:
        warnings.append(f"near max_total_positions ({total}/{max_total})")

    for fam, count in metrics["positions_by_family"].items():
        if count >= max_per_family - 1:
            warnings.append(f"near max_per_family for {fam} ({count}/{max_per_family})")

    if abs_bias >= max_bias - 1:
        warnings.append(f"near max_directional_bias ({abs_bias}/{max_bias})")

    if metrics["concentration"] > 0.6:
        warnings.append(f"concentration high ({metrics['concentration']:.0%} in one family)")

    if metrics["correlated_pair_count"] >= max_correlated - 1:
        warnings.append(f"near max_correlated_pairs ({metrics['correlated_pair_count']}/{max_correlated})")

    return True, "ok", warnings


# ---------------------------------------------------------------------------
# Main entry: check_new_entry
# ---------------------------------------------------------------------------

def check_new_entry(
    family: str,
    symbol: str,
    direction: str,
    *,
    max_total: int = DEFAULT_MAX_TOTAL_POSITIONS,
    max_per_family: int = DEFAULT_MAX_PER_FAMILY,
    max_bias: int = DEFAULT_MAX_DIRECTIONAL_BIAS,
    max_correlated: int = DEFAULT_MAX_CORRELATED_PAIRS,
) -> PortfolioCheck:
    """
    Check whether a new entry is allowed given current portfolio state.

    Parameters
    ----------
    family : str
        One of: argus_fx, helio_swing, apollo, hermes
    symbol : str
        Instrument symbol (e.g. EURUSD, mes, gold_f)
    direction : str
        LONG or SHORT

    Returns
    -------
    PortfolioCheck with allowed, reason, metrics, warnings
    """
    positions = scan_all_positions()

    # Simulate adding the proposed position for limit checking
    proposed = OpenPosition(
        family=family,
        symbol=symbol,
        symbol_norm=_normalise_symbol(symbol),
        direction=direction.upper(),
        entry_price=0.0,
        state_path="<proposed>",
    )
    simulated = positions + [proposed]
    metrics = compute_metrics(simulated)

    # Block opposite-direction trades on the same instrument across families
    # (hedging against yourself = paying spread both ways for zero net exposure)
    proposed_norm = _normalise_symbol(symbol)
    for existing in positions:
        if existing.symbol_norm == proposed_norm and existing.direction != direction.upper():
            return PortfolioCheck(
                allowed=False,
                reason=f"opposite_direction_conflict: {existing.family} is {existing.direction} {existing.symbol}, "
                       f"cannot open {direction.upper()} from {family}",
                metrics=compute_metrics(simulated),
                warnings=[],
            )

    allowed, reason, warnings = check_limits(
        metrics,
        max_total=max_total,
        max_per_family=max_per_family,
        max_bias=max_bias,
        max_correlated=max_correlated,
    )

    result = PortfolioCheck(
        allowed=allowed,
        reason=reason,
        metrics=metrics,
        warnings=warnings,
    )

    # Persist result
    _write_result(result, family, symbol, direction)

    return result


def check_current() -> PortfolioCheck:
    """Check current portfolio state without proposing a new entry."""
    positions = scan_all_positions()
    metrics = compute_metrics(positions)
    allowed, reason, warnings = check_limits(metrics)
    result = PortfolioCheck(
        allowed=allowed,
        reason=reason,
        metrics=metrics,
        warnings=warnings,
    )
    _write_result(result)
    return result


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _write_result(
    result: PortfolioCheck,
    family: str = "",
    symbol: str = "",
    direction: str = "",
) -> None:
    """Write check result to portfolio_guard.json."""
    out = asdict(result)
    out["checked_at"] = datetime.now(timezone.utc).isoformat()
    if family:
        out["proposed"] = {"family": family, "symbol": symbol, "direction": direction}

    os.makedirs(OUTPUT_PATH.parent, exist_ok=True)
    tmp = str(OUTPUT_PATH) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, indent=2)
    # Atomic-ish rename (Windows: replace if exists)
    if os.path.exists(str(OUTPUT_PATH)):
        os.replace(tmp, str(OUTPUT_PATH))
    else:
        os.rename(tmp, str(OUTPUT_PATH))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    parser = argparse.ArgumentParser(
        description="Cross-family portfolio risk guard",
    )
    parser.add_argument(
        "--check", nargs=3, metavar=("FAMILY", "SYMBOL", "DIRECTION"),
        help="Check if a new entry is allowed (e.g. --check hermes XAUUSD LONG)",
    )
    parser.add_argument("--max-total", type=int, default=DEFAULT_MAX_TOTAL_POSITIONS)
    parser.add_argument("--max-per-family", type=int, default=DEFAULT_MAX_PER_FAMILY)
    parser.add_argument("--max-bias", type=int, default=DEFAULT_MAX_DIRECTIONAL_BIAS)
    parser.add_argument("--max-correlated", type=int, default=DEFAULT_MAX_CORRELATED_PAIRS)

    args = parser.parse_args()

    limits = dict(
        max_total=args.max_total,
        max_per_family=args.max_per_family,
        max_bias=args.max_bias,
        max_correlated=args.max_correlated,
    )

    if args.check:
        family, symbol, direction = args.check
        result = check_new_entry(family, symbol, direction, **limits)
        label = f"Proposed: {family} {symbol} {direction}"
    else:
        result = check_current()
        label = "Current portfolio state"

    # Print
    print(f"\n{'='*60}")
    print(f"  PORTFOLIO GUARD  |  {label}")
    print(f"{'='*60}")
    print(f"  Allowed : {result.allowed}")
    if result.reason != "ok":
        print(f"  Reason  : {result.reason}")
    print()

    m = result.metrics
    print(f"  Open positions : {m['total_open_positions']}")
    print(f"  By family      : {m['positions_by_family'] or '(none)'}")
    print(f"  Longs / Shorts : {m['longs']} / {m['shorts']}")
    print(f"  Dir bias       : {m['directional_bias']:+d}")
    print(f"  Concentration  : {m['concentration']:.0%}")
    print(f"  Correlated     : {m['correlated_pair_count']} pair(s)")
    if m["correlated_exposure"]:
        for sym, fams in m["correlated_exposure"].items():
            print(f"    {sym}: {', '.join(fams)}")

    if m["positions_detail"]:
        print()
        print("  Positions:")
        for p in m["positions_detail"]:
            print(f"    {p['family']:12s}  {p['symbol']:12s}  {p['direction']}")

    if result.warnings:
        print()
        print("  Warnings:")
        for w in result.warnings:
            print(f"    - {w}")

    print(f"\n  Written to: {OUTPUT_PATH}")
    print()

    sys.exit(0 if result.allowed else 1)


if __name__ == "__main__":
    _cli()
