"""
Fleet Conviction Scorer
========================
Lightweight orchestration module that ANY fleet runner can call before entering
a trade. Reads all available signals (Atlas regime, Atlas cascades, options flow,
Themis congressional, cross-system positions) and returns a sizing multiplier.

Usage from any runner:
    from forge.conviction import score_conviction

    result = score_conviction(
        system="titan", ticker="MSFT", direction="LONG", signal_strength=0.8
    )
    size = base_size * result["size_multiplier"]

CLI:
    python -m forge.conviction --ticker MSFT --direction LONG --system titan
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[1]
_FORGE = _REPO / "forge"

# Systems that trade FX pairs (skip options check)
_FX_SYSTEMS = {"argus"}
# Systems that trade ETF pairs (skip options check)
_ETF_PAIR_SYSTEMS = {"gdx_gld"}

# Sector mapping for cross-system sector overlap detection
_SECTOR_MAP = {
    # Tech
    "MSFT": "XLK", "AAPL": "XLK", "NVDA": "XLK", "GOOG": "XLK", "GOOGL": "XLK",
    "META": "XLK", "AMZN": "XLY", "TSLA": "XLY", "AMD": "XLK", "INTC": "XLK",
    "CRM": "XLK", "AVGO": "XLK", "ORCL": "XLK", "ADBE": "XLK", "CSCO": "XLK",
    # Financials
    "JPM": "XLF", "BAC": "XLF", "WFC": "XLF", "GS": "XLF", "MS": "XLF",
    "C": "XLF", "BRK-B": "XLF", "AXP": "XLF", "V": "XLF", "MA": "XLF",
    # Energy
    "XOM": "XLE", "CVX": "XLE", "COP": "XLE", "SLB": "XLE", "EOG": "XLE",
    "OXY": "XLE", "MPC": "XLE", "VLO": "XLE",
    # Healthcare
    "UNH": "XLV", "JNJ": "XLV", "PFE": "XLV", "ABBV": "XLV", "MRK": "XLV",
    "LLY": "XLV", "TMO": "XLV", "ABT": "XLV",
    # Industrials
    "CAT": "XLI", "BA": "XLI", "HON": "XLI", "GE": "XLI", "UPS": "XLI",
    "RTX": "XLI", "DE": "XLI", "LMT": "XLI",
    # Consumer
    "WMT": "XLP", "PG": "XLP", "KO": "XLP", "PEP": "XLP", "COST": "XLP",
    "HD": "XLY", "NKE": "XLY", "MCD": "XLY", "SBUX": "XLY",
    # Materials / Mining
    "GDX": "XME", "GLD": "XME", "NEM": "XME", "FCX": "XME",
    # ETFs map to themselves
    "SPY": "SPY", "QQQ": "QQQ", "IWM": "IWM", "DIA": "DIA",
    "XLF": "XLF", "XLE": "XLE", "XLK": "XLK", "XLV": "XLV",
    "XLI": "XLI", "XLY": "XLY", "XLP": "XLP", "XLU": "XLU",
}


def _get_sector(ticker: str) -> Optional[str]:
    """Return the sector ETF for a ticker, or None if unknown."""
    return _SECTOR_MAP.get(ticker.upper())


# ---------------------------------------------------------------------------
# Factor 1: Atlas Regime
# ---------------------------------------------------------------------------

def _score_atlas_regime(direction: str) -> dict:
    """Read macro_regime.json and score based on regime vs direction."""
    try:
        from forge.atlas.fleet_gate import check_atlas

        gate = check_atlas()
        if not gate.available:
            return {"score": 0.0, "detail": "Atlas unavailable"}

        regime = gate.regime.upper()
        event_window = gate.event_window
        upcoming = gate.upcoming_event

        score = 0.0
        details = []

        # Regime alignment
        if regime == "CRISIS":
            score -= 0.5
            details.append("CRISIS regime")
        elif regime == "RISK_ON":
            if direction == "LONG":
                score += 0.3
                details.append("RISK_ON + LONG")
            else:
                score -= 0.1
                details.append("RISK_ON but SHORT")
        elif regime == "RISK_OFF":
            if direction == "LONG":
                score -= 0.3
                details.append("RISK_OFF + LONG")
            else:
                score += 0.2
                details.append("RISK_OFF + SHORT")

        # Event within 24h
        if event_window:
            score -= 0.2
            details.append(f"event within 24h: {upcoming}")

        detail_str = ", ".join(details) if details else f"regime={regime}"
        return {"score": round(score, 2), "detail": detail_str}

    except Exception as e:
        log.debug("Atlas regime check failed: %s", e)
        return {"score": 0.0, "detail": f"error: {e}"}


# ---------------------------------------------------------------------------
# Factor 2: Atlas Event Cascade
# ---------------------------------------------------------------------------

def _score_atlas_cascade(ticker: str, direction: str) -> dict:
    """Check if Atlas has an active cascade prediction for this ticker's sector."""
    try:
        from forge.atlas.db.schema import get_connection, DB_PATH

        if not Path(DB_PATH).exists():
            return {"score": 0.0, "detail": "Atlas DB not found"}

        sector = _get_sector(ticker)
        if sector is None:
            return {"score": 0.0, "detail": f"no sector mapping for {ticker}"}

        conn = get_connection()
        try:
            # Look for active cascade predictions for this sector's ETF
            # that haven't been scored yet (still pending)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            rows = conn.execute("""
                SELECT cp.predicted_direction, cp.confidence, cp.asset
                FROM cascade_predictions cp
                WHERE cp.actual_car IS NULL
                  AND cp.asset = ?
                  AND (cp.deadline IS NULL OR cp.deadline >= ?)
                ORDER BY cp.prediction_ts DESC
                LIMIT 5
            """, (sector, now)).fetchall()

            if not rows:
                return {"score": 0.0, "detail": f"no active cascades for {sector}"}

            # Aggregate direction signal from cascades
            dir_val = 1 if direction == "LONG" else -1
            aligned = 0
            opposed = 0
            for row in rows:
                pred_dir = row["predicted_direction"]
                if pred_dir is not None:
                    if pred_dir == dir_val:
                        aligned += 1
                    else:
                        opposed += 1

            if aligned > opposed:
                score = 0.2
                detail = f"cascade aligned with {direction} on {sector} ({aligned} predictions)"
            elif opposed > aligned:
                score = -0.2
                detail = f"cascade opposes {direction} on {sector} ({opposed} counter-predictions)"
            else:
                score = 0.0
                detail = f"cascade mixed on {sector}"

            return {"score": score, "detail": detail}
        finally:
            conn.close()

    except Exception as e:
        log.debug("Atlas cascade check failed: %s", e)
        return {"score": 0.0, "detail": f"error: {e}"}


# ---------------------------------------------------------------------------
# Factor 3: Options Flow
# ---------------------------------------------------------------------------

def _score_options_flow(ticker: str, direction: str, system: str) -> dict:
    """Compute options sentiment score. Skip for FX/ETF-pair systems."""
    if system.lower() in _FX_SYSTEMS | _ETF_PAIR_SYSTEMS:
        return {"score": 0.0, "detail": f"skipped for {system}"}

    try:
        from forge.options_flow import check_options_flow

        flow = check_options_flow(ticker)
        if flow is None or flow.get("data_quality") == "unavailable":
            return {"score": 0.0, "detail": "no options data"}

        score = 0.0
        details = []
        pc = flow["pc_volume_ratio"]
        bias = flow["bias"]

        # PC ratio alignment
        if pc < 0.7 and direction == "LONG":
            score += 0.2
            details.append(f"PC ratio {pc:.2f} bullish + LONG")
        elif pc > 1.3 and direction == "SHORT":
            score += 0.2
            details.append(f"PC ratio {pc:.2f} bearish + SHORT")
        elif (pc < 0.7 and direction == "SHORT") or (pc > 1.3 and direction == "LONG"):
            score -= 0.2
            details.append(f"PC ratio {pc:.2f} contradicts {direction}")

        # Unusual activity bonus
        if flow.get("unusual_activity"):
            score += 0.1
            details.append("unusual volume detected")

        detail_str = ", ".join(details) if details else f"PC={pc:.2f} {bias}"
        return {"score": round(score, 2), "detail": detail_str}

    except Exception as e:
        log.debug("Options flow check failed: %s", e)
        return {"score": 0.0, "detail": f"error: {e}"}


# ---------------------------------------------------------------------------
# Factor 4: Themis Congressional Signal
# ---------------------------------------------------------------------------

def _score_themis(ticker: str, direction: str) -> dict:
    """Check Themis DB for congressional trading signals on this ticker."""
    try:
        from forge.themis.db import get_connection, DB_PATH

        if not Path(DB_PATH).exists():
            return {"score": 0.0, "detail": "Themis DB not found"}

        conn = get_connection()
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")

            rows = conn.execute("""
                SELECT signal_type, member_count, description
                FROM signals
                WHERE ticker = ?
                  AND status = 'active'
                  AND detected_at >= ?
                ORDER BY detected_at DESC
            """, (ticker.upper(), cutoff)).fetchall()

            if not rows:
                return {"score": 0.0, "detail": "no Themis signals"}

            score = 0.0
            details = []

            for row in rows:
                sig_type = row["signal_type"]

                if sig_type == "CLUSTER_BUY" and direction == "LONG":
                    score += 0.2
                    details.append("cluster buy detected")
                elif sig_type == "CLUSTER_BUY" and direction == "SHORT":
                    score -= 0.1
                    details.append("cluster buy opposes SHORT")
                elif sig_type == "CLUSTER_SELL" and direction == "SHORT":
                    score += 0.2
                    details.append("cluster sell supports SHORT")
                elif sig_type == "CLUSTER_SELL" and direction == "LONG":
                    score -= 0.1
                    details.append("cluster sell opposes LONG")
                elif sig_type == "BIG_BUY" and direction == "LONG":
                    score += 0.1
                    details.append("big buy detected")
                elif sig_type == "BIG_SELL" and direction == "LONG":
                    score -= 0.2
                    details.append("big sell opposes LONG")
                elif sig_type == "BIG_SELL" and direction == "SHORT":
                    score += 0.1
                    details.append("big sell supports SHORT")
                elif sig_type == "UNANIMOUS_DIRECTION":
                    desc = row["description"] or ""
                    if ("buys" in desc.lower() and direction == "LONG") or \
                       ("sells" in desc.lower() and direction == "SHORT"):
                        score += 0.15
                        details.append("unanimous direction aligned")
                    else:
                        score -= 0.1
                        details.append("unanimous direction opposes")

            # Cap Themis contribution
            score = max(-0.3, min(0.3, score))

            detail_str = ", ".join(details) if details else "signals found but neutral"
            return {"score": round(score, 2), "detail": detail_str}
        finally:
            conn.close()

    except Exception as e:
        log.debug("Themis check failed: %s", e)
        return {"score": 0.0, "detail": f"error: {e}"}


# ---------------------------------------------------------------------------
# Factor 5: Cross-System Check
# ---------------------------------------------------------------------------

def _score_cross_system(ticker: str, direction: str, system: str) -> dict:
    """Check fleet positions for overlap."""
    try:
        from helio.fleet_risk import get_fleet_positions

        fleet = get_fleet_positions()
        if not fleet:
            return {"score": 0.0, "detail": "no fleet positions"}

        score = 0.0
        details = []
        our_sector = _get_sector(ticker)

        for sys_name, symbols in fleet.items():
            # Don't penalize ourselves
            if sys_name.lower() == system.lower():
                continue

            # Same ticker overlap
            if ticker.upper() in [s.upper() for s in symbols]:
                score -= 0.1
                details.append(f"{sys_name} holds {ticker}")

            # Same sector overlap
            if our_sector:
                for sym in symbols:
                    if _get_sector(sym) == our_sector and sym.upper() != ticker.upper():
                        score -= 0.05
                        details.append(f"{sys_name} holds {sym} (same sector {our_sector})")
                        break  # One penalty per system

        detail_str = ", ".join(details) if details else "no other system signals"
        return {"score": round(score, 2), "detail": detail_str}

    except Exception as e:
        log.debug("Cross-system check failed: %s", e)
        return {"score": 0.0, "detail": f"error: {e}"}


# ---------------------------------------------------------------------------
# Main scorer
# ---------------------------------------------------------------------------

def score_conviction(
    system: str,
    ticker: str,
    direction: str,
    signal_strength: float = 1.0,
) -> dict:
    """
    Score conviction for a proposed trade across all available intelligence.

    Parameters:
        system: "titan", "hermes", "apollo", "argus", "gdx_gld"
        ticker: stock/pair being traded
        direction: "LONG" or "SHORT"
        signal_strength: system's own confidence (0-1), default 1.0

    Returns:
        {
            "size_multiplier": 0.25 - 2.0,
            "conviction": "low" | "medium" | "high" | "max",
            "factors": { factor_name: {"score": float, "detail": str} },
            "warnings": [],
            "total_score": float,
        }
    """
    direction = direction.upper()
    system = system.lower()
    ticker = ticker.upper()

    # Start with the system's own signal strength
    total_score = signal_strength

    # Gather all factors
    factors = {}

    factors["atlas_regime"] = _score_atlas_regime(direction)
    total_score += factors["atlas_regime"]["score"]

    factors["atlas_event"] = _score_atlas_cascade(ticker, direction)
    total_score += factors["atlas_event"]["score"]

    factors["options_flow"] = _score_options_flow(ticker, direction, system)
    total_score += factors["options_flow"]["score"]

    factors["themis_signal"] = _score_themis(ticker, direction)
    total_score += factors["themis_signal"]["score"]

    factors["cross_system"] = _score_cross_system(ticker, direction, system)
    total_score += factors["cross_system"]["score"]

    # Convert to multiplier and conviction level
    warnings = []

    if total_score < 0.0:
        multiplier = 0.25
        conviction = "low"
        warnings.append("signals conflicting — minimum size")
    elif total_score < 0.5:
        multiplier = 0.5
        conviction = "low"
    elif total_score < 0.8:
        multiplier = 0.75
        conviction = "low"
    elif total_score < 1.2:
        multiplier = 1.0
        conviction = "medium"
    elif total_score < 1.5:
        multiplier = 1.5
        conviction = "high"
    else:
        multiplier = 2.0
        conviction = "max"

    # Log the decision
    factor_summary = " | ".join(
        f"{k}={v['score']:+.2f}" for k, v in factors.items() if v["score"] != 0
    )
    log.info(
        "[conviction] %s %s %s: base=%.2f total=%.2f -> %sx (%s) [%s]",
        system, direction, ticker, signal_strength, total_score,
        multiplier, conviction, factor_summary or "no modifiers",
    )

    return {
        "size_multiplier": multiplier,
        "conviction": conviction,
        "factors": factors,
        "warnings": warnings,
        "total_score": round(total_score, 3),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fleet Conviction Scorer")
    parser.add_argument("--ticker", required=True, help="Ticker symbol")
    parser.add_argument("--direction", required=True, choices=["LONG", "SHORT"])
    parser.add_argument("--system", required=True,
                        choices=["titan", "hermes", "apollo", "argus", "gdx_gld"])
    parser.add_argument("--strength", type=float, default=1.0,
                        help="System signal strength 0-1 (default 1.0)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )

    result = score_conviction(
        system=args.system,
        ticker=args.ticker,
        direction=args.direction,
        signal_strength=args.strength,
    )

    print()
    print("=" * 60)
    print(f"CONVICTION SCORE: {args.system.upper()} {args.direction} {args.ticker}")
    print("=" * 60)
    print(f"  Total score:     {result['total_score']:.3f}")
    print(f"  Size multiplier: {result['size_multiplier']}x")
    print(f"  Conviction:      {result['conviction']}")
    print()
    print("  Factors:")
    for name, factor in result["factors"].items():
        marker = "  " if factor["score"] == 0 else ("+ " if factor["score"] > 0 else "- ")
        print(f"    {marker}{name:20s} {factor['score']:+.2f}  {factor['detail']}")
    if result["warnings"]:
        print()
        print("  Warnings:")
        for w in result["warnings"]:
            print(f"    !! {w}")
    print()
