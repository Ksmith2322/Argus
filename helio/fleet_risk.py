"""apollo/ops/fleet_risk.py -- Cross-system position check.

Before Apollo enters any position, check what Titan, Ares, and Hermes hold.
Prevents double exposure (e.g., Titan long NVDA + Apollo long NVDA for earnings).
"""
import json
import logging
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_log = logging.getLogger("apollo.risk")


def get_fleet_positions() -> dict[str, list[str]]:
    """Scan all systems for open positions. Returns {system: [symbols]}."""
    positions = {}

    # Titan
    titan_pos = REPO / "titan" / "logs" / "positions.json"
    if titan_pos.exists():
        try:
            data = json.loads(titan_pos.read_text())
            symbols = [s for s in data.keys() if s not in ("last_rebalance", "last_signal", "holdings")]
            if symbols:
                positions["Titan"] = symbols
        except Exception:
            pass

    # Ares
    ares_pos = REPO / "ares" / "logs" / "positions.json"
    if ares_pos.exists():
        try:
            data = json.loads(ares_pos.read_text())
            holdings = list(data.get("holdings", {}).keys())
            if holdings:
                positions["Ares"] = holdings
        except Exception:
            pass

    # Hermes
    hermes_pos = REPO / "hermes" / "logs" / "positions.json"
    if hermes_pos.exists():
        try:
            data = json.loads(hermes_pos.read_text())
            symbols = [s for s in data.keys() if s not in ("last_rebalance", "last_signal", "holdings")]
            if symbols:
                positions["Hermes"] = symbols
        except Exception:
            pass

    # Apollo (self)
    apollo_pos = REPO / "apollo" / "logs" / "positions.json"
    if apollo_pos.exists():
        try:
            data = json.loads(apollo_pos.read_text())
            symbols = list(data.get("positions", {}).keys())
            if symbols:
                positions["Apollo"] = symbols
        except Exception:
            pass

    # Argus (FX — different asset class, less risk of overlap)
    # Skip — FX pairs don't overlap with stocks

    return positions


def check_exposure(symbol: str) -> dict:
    """Check if a symbol has exposure elsewhere in the fleet.

    Returns {blocked: bool, reason: str, existing: [{system, symbol}]}
    """
    fleet = get_fleet_positions()
    existing = []

    for system, symbols in fleet.items():
        if symbol in symbols:
            existing.append({"system": system, "symbol": symbol})

    blocked = len(existing) > 0
    reason = ""
    if blocked:
        systems = ", ".join(e["system"] for e in existing)
        reason = f"DOUBLE_EXPOSURE: {symbol} already held in {systems}"
        _log.warning(reason)

    return {
        "blocked": blocked,
        "reason": reason,
        "existing": existing,
        "fleet_positions": fleet,
    }


def fleet_summary() -> str:
    """Human-readable fleet position summary."""
    fleet = get_fleet_positions()
    if not fleet:
        return "Fleet: no open positions across any system."

    lines = ["Fleet Positions:"]
    for system, symbols in fleet.items():
        lines.append(f"  {system}: {', '.join(symbols)}")
    return "\n".join(lines)
