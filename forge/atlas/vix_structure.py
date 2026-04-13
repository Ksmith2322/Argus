"""
VIX Term Structure Monitor
===========================
Monitor the VIX futures curve for contango/backwardation and transitions.
The VIX term structure is the single best early warning for regime shifts:
  - Contango (spot < futures) = normal, trends follow through
  - Backwardation (spot > futures) = fear is elevated and INCREASING
  - Inversion transition = the most powerful signal

Usage:
    python -m forge.atlas.vix_structure --check   # print current term structure
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("atlas.vix_structure")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Structure classification thresholds (spot / 3m ratio)
STEEP_CONTANGO_CEILING = 0.85
MILD_CONTANGO_CEILING = 0.95
FLAT_CEILING = 1.00
MILD_BACKWARDATION_CEILING = 1.10
# Above 1.10 = steep backwardation

# History file for transition detection
HISTORY_PATH = Path(__file__).resolve().parent.parent / "logs" / "atlas" / "vix_structure_history.json"

# Macro regime file
DEFAULT_REGIME_PATH = Path(__file__).resolve().parent / "macro_regime.json"


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def _fetch_vix_data() -> dict:
    """
    Fetch VIX spot and term structure data.
    Try in order:
      1. ^VIX and ^VIX3M (spot and 3-month VIX)
      2. ^VIX9D and ^VIX (9-day as short end, VIX as long end — inverted logic)
      3. Fallback: VIX level only with estimated structure
    """
    import yfinance as yf

    result = {"source": None, "vix_spot": None, "vix_3m": None, "inverted_logic": False}

    # --- Attempt 1: ^VIX + ^VIX3M ---
    try:
        vix_data = yf.Ticker("^VIX").history(period="10d")
        vix3m_data = yf.Ticker("^VIX3M").history(period="10d")

        if len(vix_data) > 0 and len(vix3m_data) > 0:
            result["vix_spot"] = float(vix_data["Close"].iloc[-1])
            result["vix_3m"] = float(vix3m_data["Close"].iloc[-1])
            result["source"] = "VIX+VIX3M"
            log.info(
                "VIX term structure via ^VIX (%.2f) + ^VIX3M (%.2f)",
                result["vix_spot"], result["vix_3m"],
            )
            return result
    except Exception as e:
        log.debug("VIX3M fetch failed: %s", e)

    # --- Attempt 2: ^VIX9D + ^VIX (inverted — 9D is short end) ---
    try:
        vix_data = yf.Ticker("^VIX").history(period="10d")
        vix9d_data = yf.Ticker("^VIX9D").history(period="10d")

        if len(vix_data) > 0 and len(vix9d_data) > 0:
            vix9d = float(vix9d_data["Close"].iloc[-1])
            vix_spot = float(vix_data["Close"].iloc[-1])
            # VIX9D is the short end, VIX is the longer end
            # ratio = short / long — same interpretation as spot/3m
            result["vix_spot"] = vix9d  # short-term vol
            result["vix_3m"] = vix_spot  # longer-term vol (VIX ~30d)
            result["source"] = "VIX9D+VIX"
            result["inverted_logic"] = True
            log.info(
                "VIX term structure via ^VIX9D (%.2f) / ^VIX (%.2f)",
                vix9d, vix_spot,
            )
            return result
    except Exception as e:
        log.debug("VIX9D fetch failed: %s", e)

    # --- Attempt 3: VIX only — estimate structure from level ---
    try:
        vix_data = yf.Ticker("^VIX").history(period="10d")
        if len(vix_data) > 0:
            vix_spot = float(vix_data["Close"].iloc[-1])
            # Heuristic: contango is typical when VIX < 25
            # Estimate 3M as VIX * (1 + contango_premium)
            if vix_spot < 20:
                estimated_3m = vix_spot * 1.15  # steep contango in calm markets
            elif vix_spot < 25:
                estimated_3m = vix_spot * 1.05  # mild contango
            elif vix_spot < 35:
                estimated_3m = vix_spot * 0.97  # mild backwardation
            else:
                estimated_3m = vix_spot * 0.90  # steep backwardation in crisis

            result["vix_spot"] = vix_spot
            result["vix_3m"] = round(estimated_3m, 2)
            result["source"] = "VIX_ESTIMATED"
            log.warning(
                "Using VIX-only estimate: spot=%.2f, est_3m=%.2f",
                vix_spot, estimated_3m,
            )
            return result
    except Exception as e:
        log.error("All VIX data sources failed: %s", e)

    return result


# ---------------------------------------------------------------------------
# Structure analysis
# ---------------------------------------------------------------------------

def _classify_structure(ratio: float) -> str:
    """Classify the term structure from the spot/3m ratio."""
    if ratio < FLAT_CEILING:
        return "CONTANGO"
    elif ratio <= MILD_BACKWARDATION_CEILING:
        return "BACKWARDATION"
    else:
        return "BACKWARDATION"


def _classify_signal(ratio: float) -> tuple[str, str]:
    """
    Map the ratio to a signal level and detail message.

    Returns (signal, detail).
    """
    if ratio < STEEP_CONTANGO_CEILING:
        return (
            "NORMAL",
            "VIX in steep contango — market complacent, trends likely to follow through",
        )
    elif ratio < MILD_CONTANGO_CEILING:
        return (
            "NORMAL",
            "VIX in mild contango — normal conditions",
        )
    elif ratio < FLAT_CEILING:
        return (
            "CAUTION",
            "Term structure flattening — volatility may increase",
        )
    elif ratio < MILD_BACKWARDATION_CEILING:
        return (
            "WARNING",
            "Fear is elevated and INCREASING — reduce new long exposure fleet-wide",
        )
    else:
        return (
            "DANGER",
            "Acute stress — halt all new longs fleet-wide",
        )


def _load_history() -> list[dict]:
    """Load VIX structure history for transition detection."""
    if HISTORY_PATH.exists():
        try:
            return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save_history(history: list[dict]) -> None:
    """Save VIX structure history. Keep last 30 entries."""
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Trim to last 30 readings
    history = history[-30:]
    HISTORY_PATH.write_text(
        json.dumps(history, indent=2, default=str), encoding="utf-8",
    )


def _detect_transition(current_structure: str, history: list[dict]) -> str | None:
    """
    Detect structure transitions over the last 5 days.

    Returns:
      - "INVERTING" if was CONTANGO 5 days ago and is now FLAT or BACKWARDATION
      - "NORMALIZING" if was BACKWARDATION and is now CONTANGO
      - None if no transition
    """
    if len(history) < 2:
        return None

    # Look at entries from ~5 days ago (or the oldest we have if less)
    # History is sorted oldest-first. Look for entries with dates 3-7 days back.
    now = datetime.now(timezone.utc)
    past_structures: list[str] = []

    for entry in history:
        try:
            ts = datetime.fromisoformat(entry["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_days = (now - ts).total_seconds() / 86400
            if 3 <= age_days <= 7:
                past_structures.append(entry.get("structure", ""))
        except (KeyError, ValueError):
            continue

    if not past_structures:
        # Use oldest entry as fallback
        past_structures = [history[0].get("structure", "")]

    past_dominant = max(set(past_structures), key=past_structures.count)

    if past_dominant == "CONTANGO" and current_structure == "BACKWARDATION":
        return "INVERTING"
    if past_dominant == "CONTANGO" and current_structure == "FLAT":
        return "INVERTING"
    if past_dominant == "BACKWARDATION" and current_structure == "CONTANGO":
        return "NORMALIZING"

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_vix_term_structure() -> dict:
    """
    Returns:
    {
        "vix_spot": 19.2,
        "vix3m": 22.5,
        "ratio": 0.853,
        "structure": "CONTANGO",
        "steepness": -0.147,
        "transition": None,
        "signal": "NORMAL",
        "detail": "VIX in normal contango, no imminent risk signal",
        "source": "VIX+VIX3M",
        "timestamp": "2026-04-11T12:00:00+00:00",
    }
    """
    data = _fetch_vix_data()

    if data["vix_spot"] is None or data["vix_3m"] is None:
        return {
            "vix_spot": None,
            "vix3m": None,
            "ratio": None,
            "structure": "UNKNOWN",
            "steepness": None,
            "transition": None,
            "signal": "UNKNOWN",
            "detail": "Failed to fetch VIX data from all sources",
            "source": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    vix_spot = data["vix_spot"]
    vix_3m = data["vix_3m"]

    # Ratio: spot / 3m
    # Below 1.0 = contango (normal), above 1.0 = backwardation (fear)
    ratio = round(vix_spot / vix_3m, 4) if vix_3m > 0 else 1.0
    steepness = round(ratio - 1.0, 4)  # negative = contango, positive = backwardation

    structure = _classify_structure(ratio)
    signal, detail = _classify_signal(ratio)

    # Transition detection
    history = _load_history()
    transition = _detect_transition(structure, history)

    # Upgrade signal if inverting
    if transition == "INVERTING" and signal in ("NORMAL", "CAUTION"):
        signal = "WARNING"
        detail += " | TRANSITION: Term structure INVERTING — this is the most powerful early warning"

    if transition == "NORMALIZING":
        detail += " | TRANSITION: Term structure normalizing — all clear to resume"

    # Save current reading to history
    reading = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "vix_spot": round(vix_spot, 2),
        "vix3m": round(vix_3m, 2),
        "ratio": ratio,
        "structure": structure,
        "signal": signal,
    }
    history.append(reading)
    _save_history(history)

    return {
        "vix_spot": round(vix_spot, 2),
        "vix3m": round(vix_3m, 2),
        "ratio": ratio,
        "structure": structure,
        "steepness": steepness,
        "transition": transition,
        "signal": signal,
        "detail": detail,
        "source": data["source"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def update_atlas_regime_with_vix_structure(
    regime_path: str | Path | None = None,
) -> dict:
    """
    Read the current macro_regime.json, add vix_structure fields, write back.

    Call this from the Atlas runner after regime computation, before writing
    the regime file.

    Integration point in forge/atlas/runner.py — add after line ~199:

        # --- 4b. VIX term structure overlay ---
        from forge.atlas.vix_structure import update_atlas_regime_with_vix_structure
        try:
            vix_result = update_atlas_regime_with_vix_structure()
            log.info("VIX structure: %s | signal=%s | ratio=%.3f",
                     vix_result.get("structure", "?"),
                     vix_result.get("signal", "?"),
                     vix_result.get("ratio", 0))
        except Exception as e:
            log.error("VIX structure update failed: %s", e)

    Returns the VIX structure dict.
    """
    if regime_path is None:
        regime_path = DEFAULT_REGIME_PATH
    else:
        regime_path = Path(regime_path)

    # Get current VIX term structure
    vix_data = get_vix_term_structure()

    # Read existing regime file if it exists
    regime = {}
    if regime_path.exists():
        try:
            regime = json.loads(regime_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Could not read regime file: %s", e)

    # Add VIX structure fields
    regime["vix_structure"] = {
        "vix_spot": vix_data["vix_spot"],
        "vix3m": vix_data["vix3m"],
        "ratio": vix_data["ratio"],
        "structure": vix_data["structure"],
        "steepness": vix_data["steepness"],
        "transition": vix_data["transition"],
        "signal": vix_data["signal"],
        "detail": vix_data["detail"],
        "source": vix_data["source"],
        "updated_at": vix_data["timestamp"],
    }

    # If VIX structure signals DANGER, override fleet guidance
    if vix_data["signal"] == "DANGER":
        guidance = regime.get("fleet_guidance", {})
        guidance["vix_override"] = "HALT_NEW_LONGS"
        guidance["vix_override_reason"] = vix_data["detail"]
        regime["fleet_guidance"] = guidance
        log.warning("VIX DANGER: halting all new longs fleet-wide")

    elif vix_data["signal"] == "WARNING":
        guidance = regime.get("fleet_guidance", {})
        guidance["vix_override"] = "REDUCE_SIZE"
        guidance["vix_override_reason"] = vix_data["detail"]
        regime["fleet_guidance"] = guidance
        log.warning("VIX WARNING: reducing position sizes fleet-wide")

    # Write back
    regime_path.parent.mkdir(parents=True, exist_ok=True)
    regime_path.write_text(
        json.dumps(regime, indent=4, default=str), encoding="utf-8",
    )
    log.info("Updated %s with VIX structure data", regime_path)

    return vix_data


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="VIX Term Structure Monitor — early warning for regime shifts",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Print current VIX term structure analysis",
    )
    parser.add_argument(
        "--update-regime", action="store_true",
        help="Fetch VIX structure and update macro_regime.json",
    )
    parser.add_argument(
        "--regime-path", type=str, default=None,
        help="Path to macro_regime.json (default: forge/atlas/macro_regime.json)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.update_regime:
        result = update_atlas_regime_with_vix_structure(args.regime_path)
        print(json.dumps(result, indent=2, default=str))
        return

    # Default / --check: print current structure
    result = get_vix_term_structure()

    print()
    print("=" * 55)
    print("VIX TERM STRUCTURE MONITOR")
    print("=" * 55)
    print(f"  Timestamp:   {result['timestamp']}")
    print(f"  Source:      {result['source']}")
    print(f"  VIX Spot:    {result['vix_spot']}")
    print(f"  VIX 3M:     {result['vix3m']}")
    print(f"  Ratio:       {result['ratio']}")
    print(f"  Structure:   {result['structure']}")
    print(f"  Steepness:   {result['steepness']}")
    print(f"  Transition:  {result['transition'] or 'None'}")
    print(f"  Signal:      {result['signal']}")
    print(f"  Detail:      {result['detail']}")
    print("=" * 55)

    # Color-coded summary
    signal = result["signal"]
    if signal == "NORMAL":
        summary = "All clear. Normal market conditions."
    elif signal == "CAUTION":
        summary = "Watch closely. Volatility may be shifting."
    elif signal == "WARNING":
        summary = "REDUCE EXPOSURE. Fear is elevated and increasing."
    elif signal == "DANGER":
        summary = "HALT NEW LONGS. Acute market stress detected."
    else:
        summary = "Unable to determine VIX structure."

    print(f"\n  >> {summary}")
    print()


if __name__ == "__main__":
    main()
