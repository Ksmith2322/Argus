"""
Macro regime state machine.

Four dimensions:
  - Rates:     EASING / NEUTRAL / TIGHTENING
  - Volatility: LOW / NORMAL / HIGH / CRISIS
  - Growth:    RECESSION / SLOWING / EXPANDING
  - Liquidity: CONTRACTING / NEUTRAL / EXPANDING

Combines into an overall classification: RISK_ON, NEUTRAL, RISK_OFF, or CRISIS.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dimension classifiers
# ---------------------------------------------------------------------------

def _classify_vol(vix: float) -> str:
    if vix > 35:
        return "CRISIS"
    if vix > 25:
        return "HIGH"
    if vix > 15:
        return "NORMAL"
    return "LOW"


def _classify_rates(yield_2y_change_60d: float) -> str:
    """Classify rate regime based on 60-day change in 2Y yield.

    Positive change = market pricing tighter policy.
    Thresholds: > +0.15 = TIGHTENING, < -0.15 = EASING, else NEUTRAL.
    """
    if yield_2y_change_60d > 0.15:
        return "TIGHTENING"
    if yield_2y_change_60d < -0.15:
        return "EASING"
    return "NEUTRAL"


def _classify_growth(ism: float) -> str:
    """Classify growth based on ISM Manufacturing PMI.

    < 47 = RECESSION, 47-50 = SLOWING, > 50 = EXPANDING.
    """
    if ism < 47:
        return "RECESSION"
    if ism <= 50:
        return "SLOWING"
    return "EXPANDING"


def _classify_liquidity(hy_spread_change_20d: float) -> str:
    """Classify liquidity based on 20-day change in HY-Treasury spread.

    Positive change = spreads widening = liquidity contracting.
    Thresholds: > +0.20 = CONTRACTING, < -0.20 = EXPANDING, else NEUTRAL.
    """
    if hy_spread_change_20d > 0.20:
        return "CONTRACTING"
    if hy_spread_change_20d < -0.20:
        return "EXPANDING"
    return "NEUTRAL"


def _classify_overall(vol: str, rates: str, growth: str, liquidity: str) -> str:
    """Derive overall regime from the four dimensions.

    Scoring: each dimension contributes a risk score.
    """
    score = 0

    # Vol contribution
    vol_scores = {"LOW": -2, "NORMAL": 0, "HIGH": 2, "CRISIS": 4}
    score += vol_scores.get(vol, 0)

    # Rates contribution
    rates_scores = {"EASING": -1, "NEUTRAL": 0, "TIGHTENING": 1}
    score += rates_scores.get(rates, 0)

    # Growth contribution
    growth_scores = {"EXPANDING": -1, "SLOWING": 1, "RECESSION": 3}
    score += growth_scores.get(growth, 0)

    # Liquidity contribution
    liq_scores = {"EXPANDING": -1, "NEUTRAL": 0, "CONTRACTING": 2}
    score += liq_scores.get(liquidity, 0)

    if score >= 6:
        return "CRISIS"
    if score >= 2:
        return "RISK_OFF"
    if score <= -2:
        return "RISK_ON"
    return "NEUTRAL"


def _alert_level(overall: str) -> str:
    return {
        "RISK_ON": "normal",
        "NEUTRAL": "normal",
        "RISK_OFF": "elevated",
        "CRISIS": "critical",
    }.get(overall, "normal")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_regime(
    vix: float,
    yield_2y_change_60d: float,
    ism: float,
    hy_spread_change_20d: float,
) -> dict:
    """Compute the macro regime from raw inputs.

    Returns a dict with dimension classifications, overall regime,
    alert level, position size modifier, and raw inputs.
    """
    vol = _classify_vol(vix)
    rates = _classify_rates(yield_2y_change_60d)
    growth = _classify_growth(ism)
    liquidity = _classify_liquidity(hy_spread_change_20d)
    overall = _classify_overall(vol, rates, growth, liquidity)

    regime = {
        "overall": overall,
        "rates": rates,
        "vol": vol,
        "growth": growth,
        "liquidity": liquidity,
    }

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "regime": regime,
        "alert_level": _alert_level(overall),
        "position_size_modifier": get_position_size_modifier(regime),
        "raw_inputs": {
            "vix": vix,
            "yield_2y_change_60d": yield_2y_change_60d,
            "ism": ism,
            "hy_spread_change_20d": hy_spread_change_20d,
        },
    }


def fetch_regime_inputs() -> dict:
    """Fetch current macro regime inputs from yfinance and FRED.

    Returns a dict with keys: vix, yield_2y_change_60d, ism,
    hy_spread_change_20d.

    ISM is hardcoded (monthly release, update manually).
    """
    import yfinance as yf

    # --- VIX ---
    vix_data = yf.Ticker("^VIX").history(period="5d")
    vix = float(vix_data["Close"].iloc[-1]) if len(vix_data) > 0 else 20.0

    # --- 2Y yield 60-day change ---
    # Use ^IRX (13-week T-bill) as proxy, or IEF inverse for 2Y
    # Better: use 2Y treasury yield via yfinance if available
    try:
        twy = yf.Ticker("2YY=F").history(period="90d")  # 2Y yield futures
        if len(twy) >= 2:
            current_2y = float(twy["Close"].iloc[-1])
            past_2y = float(twy["Close"].iloc[0]) if len(twy) < 60 else float(twy["Close"].iloc[-60])
            yield_2y_change_60d = current_2y - past_2y
        else:
            yield_2y_change_60d = 0.0
    except Exception:
        # Fallback: use SHY ETF as inverse proxy
        try:
            shy = yf.Ticker("SHY").history(period="90d")
            if len(shy) >= 60:
                shy_return = (float(shy["Close"].iloc[-1]) / float(shy["Close"].iloc[-60])) - 1
                # SHY drops when yields rise; approximate yield change
                yield_2y_change_60d = -shy_return * 10  # rough scaling
            else:
                yield_2y_change_60d = 0.0
        except Exception:
            yield_2y_change_60d = 0.0

    # --- HY spread change (20d) ---
    # Proxy: HYG vs TLT relative performance
    try:
        hyg = yf.Ticker("HYG").history(period="30d")
        tlt = yf.Ticker("TLT").history(period="30d")
        if len(hyg) >= 20 and len(tlt) >= 20:
            hyg_ret = (float(hyg["Close"].iloc[-1]) / float(hyg["Close"].iloc[-20])) - 1
            tlt_ret = (float(tlt["Close"].iloc[-1]) / float(tlt["Close"].iloc[-20])) - 1
            # When HYG underperforms TLT, spreads are widening
            hy_spread_change_20d = -(hyg_ret - tlt_ret) * 10  # scale to approx spread change
        else:
            hy_spread_change_20d = 0.0
    except Exception:
        hy_spread_change_20d = 0.0

    # --- ISM Manufacturing ---
    # Monthly release; hardcode most recent value.
    # Update this after each ISM release.
    ISM_LATEST = 49.0  # Mar 2026 estimate -- UPDATE MANUALLY

    return {
        "vix": round(vix, 2),
        "yield_2y_change_60d": round(yield_2y_change_60d, 4),
        "ism": ISM_LATEST,
        "hy_spread_change_20d": round(hy_spread_change_20d, 4),
    }


def get_position_size_modifier(regime: dict) -> float:
    """Return a position size multiplier (0.25 - 1.0) based on regime.

    Vol dimension is the primary driver:
      CRISIS = 0.25, HIGH = 0.50, NORMAL = 0.75, LOW = 1.0

    Further reduced by 25% if growth = RECESSION.
    """
    vol_mod = {
        "CRISIS": 0.25,
        "HIGH": 0.50,
        "NORMAL": 0.75,
        "LOW": 1.0,
    }
    modifier = vol_mod.get(regime.get("vol", "NORMAL"), 0.75)

    if regime.get("growth") == "RECESSION":
        modifier *= 0.75

    # Floor at 0.25
    return max(round(modifier, 2), 0.25)


def get_fleet_guidance(regime: dict, active_events: list | None = None) -> dict:
    """Return fleet-level guidance based on current regime and active events.

    Returns a dict with: sectors_avoid, sectors_favor, fx_bias,
    position_size_modifier.
    """
    active_events = active_events or []
    overall = regime.get("overall", "NEUTRAL")
    vol = regime.get("vol", "NORMAL")
    rates = regime.get("rates", "NEUTRAL")
    growth = regime.get("growth", "EXPANDING")

    sectors_avoid: list[str] = []
    sectors_favor: list[str] = []
    fx_bias = "NEUTRAL"

    # --- Overall regime guidance ---
    if overall == "CRISIS":
        sectors_avoid.extend(["XLF", "XLY", "IWM", "EEM"])
        sectors_favor.extend(["GLD", "TLT"])
        fx_bias = "DEFENSIVE"  # favor USD, JPY
    elif overall == "RISK_OFF":
        sectors_avoid.extend(["XLY", "IWM"])
        sectors_favor.extend(["XLU", "GLD", "TLT"])
        fx_bias = "DEFENSIVE"
    elif overall == "RISK_ON":
        sectors_avoid.extend(["XLU"])
        sectors_favor.extend(["QQQ", "IWM", "EEM"])
        fx_bias = "RISK_ON"  # favor EM FX, AUD
    # NEUTRAL: no strong sector tilts

    # --- Rate-specific adjustments ---
    if rates == "TIGHTENING":
        if "QQQ" in sectors_favor:
            sectors_favor.remove("QQQ")
        if "QQQ" not in sectors_avoid:
            sectors_avoid.append("QQQ")
        if "XLF" not in sectors_favor:
            sectors_favor.append("XLF")
    elif rates == "EASING":
        if "XLF" in sectors_favor:
            sectors_favor.remove("XLF")
        if "QQQ" not in sectors_favor:
            sectors_favor.append("QQQ")
        if "XLU" not in sectors_favor:
            sectors_favor.append("XLU")

    # --- Growth-specific ---
    if growth == "RECESSION":
        if "XLI" not in sectors_avoid:
            sectors_avoid.append("XLI")
        if "TLT" not in sectors_favor:
            sectors_favor.append("TLT")
    elif growth == "EXPANDING":
        if "XLI" not in sectors_favor:
            sectors_favor.append("XLI")

    # --- Vol-specific ---
    if vol in ("HIGH", "CRISIS"):
        if "HYG" not in sectors_avoid:
            sectors_avoid.append("HYG")

    # --- Event overlays ---
    for event in active_events:
        etype = event if isinstance(event, str) else event.get("event_type", "")
        if etype in ("TARIFF_ANNOUNCE", "SANCTIONS"):
            if "EEM" not in sectors_avoid:
                sectors_avoid.append("EEM")
            if "FXI" not in sectors_avoid:
                sectors_avoid.append("FXI")
        if etype in ("OIL_SUPPLY_DISRUPTION", "OPEC_CUT"):
            if "XLE" not in sectors_favor:
                sectors_favor.append("XLE")
        if etype == "MILITARY_CONFLICT":
            if "GLD" not in sectors_favor:
                sectors_favor.append("GLD")

    # Deduplicate and remove contradictions (avoid takes precedence)
    sectors_avoid = list(dict.fromkeys(sectors_avoid))
    sectors_favor = [s for s in dict.fromkeys(sectors_favor) if s not in sectors_avoid]

    return {
        "sectors_avoid": sectors_avoid,
        "sectors_favor": sectors_favor,
        "fx_bias": fx_bias,
        "position_size_modifier": get_position_size_modifier(regime),
    }


def update_regime_file(regime: dict, output_path: str | Path | None = None) -> Path:
    """Write the full regime state to a JSON file.

    *regime* should be the output of ``compute_regime()``.
    If *output_path* is None, writes to ``forge/atlas/macro_regime.json``
    relative to the repo root.
    """
    if output_path is None:
        output_path = Path(__file__).resolve().parents[2] / "macro_regime.json"
    else:
        output_path = Path(output_path)

    # Attach fleet guidance (no active events context here; caller can
    # re-generate with events if needed)
    data = dict(regime)
    if "fleet_guidance" not in data:
        data["fleet_guidance"] = get_fleet_guidance(data.get("regime", {}))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(data, f, indent=4, default=str)

    logger.info("Wrote macro regime to %s", output_path)
    return output_path
