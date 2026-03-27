"""Health Scaler — automatic position sizing based on strategy health.

Reads degradation_control.json and outputs sizing multipliers.
The runner can optionally consume this to scale position size.

Health states:
    GREEN  = 1.0x (full size)
    YELLOW = 0.5x (one persistent warning)
    ORANGE = 0.25x (two warnings or one mechanism breach)
    RED    = 0.0x (no new entries)

Usage:
    python -m argus_flow.ops.health_scaler
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONTROL_FILE = REPO / "argus_flow" / "logs" / "degradation_control.json"
HEALTH_FILE = REPO / "argus_flow" / "logs" / "health_state.json"


def compute_health(symbol: str, control: dict) -> dict:
    """Compute health state for one symbol from degradation control."""
    entry = control.get(symbol, {})
    status = entry.get("status", "INSUFFICIENT_DATA")
    reasons = entry.get("reasons", [])

    hard_pauses = [r for r in reasons if r.startswith("HARD_PAUSE")]
    warns = [r for r in reasons if r.startswith("WARN")]

    if status == "HARD_PAUSE" or hard_pauses:
        health = "RED"
        multiplier = 0.0
    elif len(warns) >= 2:
        health = "ORANGE"
        multiplier = 0.25
    elif warns:
        health = "YELLOW"
        multiplier = 0.5
    elif status == "INSUFFICIENT_DATA":
        health = "GREEN"  # allow trading while collecting data
        multiplier = 1.0
    else:
        health = "GREEN"
        multiplier = 1.0

    return {
        "symbol": symbol,
        "health": health,
        "size_multiplier": multiplier,
        "degradation_status": status,
        "reason_count": len(reasons),
        "top_reasons": reasons[:3],
    }


def main():
    if not CONTROL_FILE.exists():
        print("No degradation_control.json found. Run degradation_report.py first.")
        return

    control = json.loads(CONTROL_FILE.read_text())

    print(f"\n{'='*50}")
    print(f"  HEALTH SCALER — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"{'='*50}\n")

    health_states = {}
    colors = {"GREEN": "\033[32m", "YELLOW": "\033[33m", "ORANGE": "\033[35m", "RED": "\033[31m"}
    reset = "\033[0m"

    for symbol in sorted(control.keys()):
        h = compute_health(symbol, control)
        health_states[symbol] = h
        c = colors.get(h["health"], "")
        print(f"  {symbol:>8}: {c}{h['health']:>8}{reset}  size={h['size_multiplier']:.2f}x  ({h['degradation_status']})")
        for r in h.get("top_reasons", []):
            print(f"           {r}")

    # Save health state
    output = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbols": health_states,
    }
    HEALTH_FILE.write_text(json.dumps(output, indent=2) + "\n")
    print(f"\n  Saved: {HEALTH_FILE}")


if __name__ == "__main__":
    main()
