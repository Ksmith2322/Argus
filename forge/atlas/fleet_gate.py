"""
Atlas Fleet Gate
=================
Lightweight module that other fleet systems import to read Atlas's regime state.
Returns position sizing guidance and sector filters without heavy dependencies.

Modes:
    LOG_ONLY  — Log what Atlas would recommend, don't block anything (burn-in safe)
    GATE      — Actually enforce position size reductions and sector blocks

Usage in any fleet system:
    from forge.atlas.fleet_gate import check_atlas

    gate = check_atlas()
    if gate.reduce_size:
        log.info("Atlas recommends %.0f%% position size", gate.size_modifier * 100)
    if gate.avoid_sector("XLE"):
        log.info("Atlas: avoid XLE sector")
    if gate.event_window:
        log.info("Atlas: major event within 24h — %s", gate.upcoming_event)
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Paths
_FORGE_DIR = Path(__file__).resolve().parent
_REGIME_PATH = _FORGE_DIR.parent / "macro_regime.json"  # forge/macro_regime.json
_SCHEDULED_DB = _FORGE_DIR.parent / "data" / "atlas.db"
_HEARTBEAT_PATH = _FORGE_DIR.parent / "logs" / "atlas" / "heartbeat.json"

# Mode: LOG_ONLY during burn-in, GATE after
ATLAS_MODE = os.getenv("ATLAS_GATE_MODE", "LOG_ONLY").upper()
STALE_THRESHOLD_S = 600  # ignore regime if older than 10 minutes


@dataclass
class AtlasGate:
    """Result of an Atlas regime check."""
    available: bool = False          # Atlas is running and regime is fresh
    stale: bool = False              # regime file exists but is old
    regime: str = "unknown"          # RISK_ON, NEUTRAL, RISK_OFF, CRISIS
    alert_level: str = "unknown"     # normal, elevated, high, crisis
    size_modifier: float = 1.0       # 0.25-1.0 recommended position size scaling
    reduce_size: bool = False        # True if size_modifier < 1.0
    sectors_avoid: list = field(default_factory=list)
    sectors_favor: list = field(default_factory=list)
    fx_bias: str = "NEUTRAL"
    event_window: bool = False       # major scheduled event within 24h
    upcoming_event: str = ""         # description of upcoming event
    mode: str = "LOG_ONLY"           # LOG_ONLY or GATE
    raw: dict = field(default_factory=dict)

    def avoid_sector(self, sector: str) -> bool:
        """Check if a sector should be avoided."""
        return sector.upper() in [s.upper() for s in self.sectors_avoid]

    def favor_sector(self, sector: str) -> bool:
        """Check if a sector has a tailwind."""
        return sector.upper() in [s.upper() for s in self.sectors_favor]

    def should_block_entry(self) -> bool:
        """In GATE mode, return True if Atlas says don't enter new positions.
        In LOG_ONLY mode, always returns False."""
        if self.mode != "GATE":
            return False
        return self.regime == "CRISIS" or self.size_modifier <= 0.25

    def effective_size(self, base_size: float) -> float:
        """Apply Atlas sizing. In LOG_ONLY mode, returns base_size unchanged."""
        if self.mode != "GATE":
            return base_size
        return base_size * self.size_modifier

    def log_recommendation(self, system_name: str = ""):
        """Log what Atlas recommends (works in both modes)."""
        prefix = f"[Atlas→{system_name}]" if system_name else "[Atlas]"
        if not self.available:
            if self.stale:
                log.debug("%s regime stale — ignoring", prefix)
            return

        if self.reduce_size:
            log.info("%s regime=%s size_mod=%.2f (%s mode)",
                     prefix, self.regime, self.size_modifier, self.mode)
        if self.sectors_avoid:
            log.info("%s avoid sectors: %s", prefix, ", ".join(self.sectors_avoid))
        if self.event_window:
            log.info("%s event within 24h: %s", prefix, self.upcoming_event)


def _load_regime() -> Optional[dict]:
    """Load and validate the macro_regime.json file."""
    if not _REGIME_PATH.exists():
        return None
    try:
        data = json.loads(_REGIME_PATH.read_text(encoding="utf-8"))
        return data
    except (json.JSONDecodeError, OSError) as e:
        log.debug("Failed to read regime file: %s", e)
        return None


def _check_staleness(regime_data: dict) -> bool:
    """Return True if the regime data is stale (older than threshold)."""
    ts_str = regime_data.get("timestamp", "")
    if not ts_str:
        return True
    try:
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age > STALE_THRESHOLD_S
    except (ValueError, TypeError):
        return True


def _check_upcoming_events() -> tuple[bool, str]:
    """Check if a major scheduled event is within 24 hours."""
    if not _SCHEDULED_DB.exists():
        return False, ""
    try:
        import sqlite3
        conn = sqlite3.connect(str(_SCHEDULED_DB))
        conn.row_factory = sqlite3.Row
        now = datetime.now(timezone.utc)
        tomorrow = (now + timedelta(hours=24)).strftime("%Y-%m-%d")
        today = now.strftime("%Y-%m-%d")

        rows = conn.execute(
            "SELECT event_type, description, importance FROM scheduled_events "
            "WHERE event_date BETWEEN ? AND ? AND importance IN ('high', 'medium') "
            "ORDER BY event_date LIMIT 1",
            (today, tomorrow),
        ).fetchall()
        conn.close()

        if rows:
            row = rows[0]
            desc = f"{row['event_type']}: {row['description']}"
            return True, desc
        return False, ""
    except Exception as e:
        log.debug("Scheduled events check failed: %s", e)
        return False, ""


def check_atlas() -> AtlasGate:
    """
    Main entry point. Returns an AtlasGate with the current recommendation.

    Safe to call from any fleet system — no side effects, no heavy imports.
    If Atlas isn't running or regime is stale, returns a permissive default.
    """
    gate = AtlasGate(mode=ATLAS_MODE)

    # Load regime
    regime_data = _load_regime()
    if regime_data is None:
        return gate  # Atlas not available, all defaults (permissive)

    # Check staleness
    if _check_staleness(regime_data):
        gate.stale = True
        return gate  # Stale data, don't act on it

    gate.available = True
    gate.raw = regime_data

    # Extract regime
    regime = regime_data.get("regime", {})
    if isinstance(regime, dict):
        gate.regime = regime.get("overall", "unknown")
    elif isinstance(regime, str):
        gate.regime = regime

    gate.alert_level = regime_data.get("alert_level", "unknown")
    gate.size_modifier = regime_data.get("position_size_modifier", 1.0)
    gate.reduce_size = gate.size_modifier < 1.0

    # Fleet guidance
    guidance = regime_data.get("fleet_guidance", {})
    gate.sectors_avoid = guidance.get("sectors_avoid", [])
    gate.sectors_favor = guidance.get("sectors_favor", [])
    gate.fx_bias = guidance.get("fx_bias", "NEUTRAL")

    # Upcoming events
    gate.event_window, gate.upcoming_event = _check_upcoming_events()

    return gate


# ---------------------------------------------------------------------------
# Convenience functions for fleet systems
# ---------------------------------------------------------------------------

def get_size_modifier() -> float:
    """Quick check — returns 1.0 if Atlas unavailable or in LOG_ONLY."""
    gate = check_atlas()
    if gate.mode != "GATE" or not gate.available:
        return 1.0
    return gate.size_modifier


def is_sector_blocked(sector: str) -> bool:
    """Quick check — returns False if Atlas unavailable or in LOG_ONLY."""
    gate = check_atlas()
    if gate.mode != "GATE" or not gate.available:
        return False
    return gate.avoid_sector(sector)


def get_regime() -> str:
    """Quick check — returns regime string or 'unknown'."""
    gate = check_atlas()
    return gate.regime if gate.available else "unknown"


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    gate = check_atlas()
    print(f"\nAtlas Fleet Gate Check:")
    print(f"  Available:      {gate.available}")
    print(f"  Stale:          {gate.stale}")
    print(f"  Mode:           {gate.mode}")
    print(f"  Regime:         {gate.regime}")
    print(f"  Alert level:    {gate.alert_level}")
    print(f"  Size modifier:  {gate.size_modifier}")
    print(f"  Reduce size:    {gate.reduce_size}")
    print(f"  Sectors avoid:  {gate.sectors_avoid}")
    print(f"  Sectors favor:  {gate.sectors_favor}")
    print(f"  FX bias:        {gate.fx_bias}")
    print(f"  Event window:   {gate.event_window}")
    print(f"  Upcoming event: {gate.upcoming_event}")
    print(f"  Should block:   {gate.should_block_entry()}")
    print()
    gate.log_recommendation("test")
