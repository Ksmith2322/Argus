"""Cluster exposure governor — prevents the 'many strategies secretly all the same bet'
failure mode by capping total notional per macro cluster.

Spec: project_cluster_exposure_model.md (Week 3 work pulled forward to 2026-04-27 after
PDT cascade incident showed the FX_USD concentration risk was real, not theoretical).

Pre-trade entry-point: `would_breach_cluster_cap(symbol, direction, notional_usd)`
returns None if OK, else the breached cluster name. Wired into helio/ibkr_execution.py:
submit_bracket() — passes entries with est_entry_px through this check first.

Snapshot reader: `compute_cluster_exposure()` returns the current fleet exposure
broken down by cluster + by symbol. Used by dashboard panels and continuous-monitor
(continuous monitor + Discord alerts deferred for now).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[1]

# ──────────────────────────────────────────────────────────────────────
# Cluster mapping: instrument → cluster memberships per direction.
# Naming uses the instrument symbol as IBKR sees it.
# A position can populate multiple clusters (e.g. USDJPY long → FX_USD_LONG + FX_JPY).
# ──────────────────────────────────────────────────────────────────────

CLUSTER_MAP: dict[str, dict[str, list[str]]] = {
    # FX pairs — base currency listed first; "long" = buy first / sell second
    "USDJPY": {"long": ["FX_USD_LONG", "FX_JPY"], "short": ["FX_USD_SHORT", "FX_JPY"]},
    "CADJPY": {"long": ["FX_JPY"], "short": ["FX_JPY"]},
    "GBPUSD": {"long": ["FX_USD_SHORT"], "short": ["FX_USD_LONG"]},  # long GBP/USD = sell USD
    "AUDUSD": {"long": ["FX_USD_SHORT"], "short": ["FX_USD_LONG"]},
    "EURUSD": {"long": ["FX_USD_SHORT"], "short": ["FX_USD_LONG"]},
    "USDCAD": {"long": ["FX_USD_LONG"], "short": ["FX_USD_SHORT"]},
    "USDCHF": {"long": ["FX_USD_LONG"], "short": ["FX_USD_SHORT"]},

    # Equity index ETFs (US large-cap)
    "SPY": {"long": ["EQUITY_BETA"], "short": ["EQUITY_BETA_SHORT"]},
    "QQQ": {"long": ["EQUITY_BETA"], "short": ["EQUITY_BETA_SHORT"]},
    "IWM": {"long": ["EQUITY_BETA"], "short": ["EQUITY_BETA_SHORT"]},

    # Volatility (UVXY/VIXY are LONG-VIX ETFs; long them = long vol; short them = short vol)
    "UVXY": {"long": ["LONG_VOL"], "short": ["SHORT_VOL"]},
    "VIXY": {"long": ["LONG_VOL"], "short": ["SHORT_VOL"]},

    # Metals
    "GLD": {"long": ["METALS"], "short": ["METALS"]},
    "GDX": {"long": ["METALS"], "short": ["METALS"]},
    "SLV": {"long": ["METALS"], "short": ["METALS"]},

    # International equity
    "EEM": {"long": ["INTERNATIONAL_EQ"], "short": ["INTERNATIONAL_EQ"]},
    "EWJ": {"long": ["INTERNATIONAL_EQ"], "short": ["INTERNATIONAL_EQ"]},
    "VGK": {"long": ["INTERNATIONAL_EQ"], "short": ["INTERNATIONAL_EQ"]},
    "EFA": {"long": ["INTERNATIONAL_EQ"], "short": ["INTERNATIONAL_EQ"]},
    "INDA": {"long": ["INTERNATIONAL_EQ"], "short": ["INTERNATIONAL_EQ"]},

    # Index futures (equity beta) — micro and full
    "MNQ": {"long": ["EQUITY_BETA"], "short": ["EQUITY_BETA_SHORT"]},
    "MES": {"long": ["EQUITY_BETA"], "short": ["EQUITY_BETA_SHORT"]},
    "MYM": {"long": ["EQUITY_BETA"], "short": ["EQUITY_BETA_SHORT"]},
    "M2K": {"long": ["EQUITY_BETA"], "short": ["EQUITY_BETA_SHORT"]},
    "NQ":  {"long": ["EQUITY_BETA"], "short": ["EQUITY_BETA_SHORT"]},
    "ES":  {"long": ["EQUITY_BETA"], "short": ["EQUITY_BETA_SHORT"]},

    # Duration / rates
    "ZN":  {"long": ["DURATION"], "short": ["DURATION"]},
    "ZF":  {"long": ["DURATION"], "short": ["DURATION"]},
    "ZT":  {"long": ["DURATION"], "short": ["DURATION"]},
    "TLT": {"long": ["DURATION"], "short": ["DURATION"]},
    "IEF": {"long": ["DURATION"], "short": ["DURATION"]},
}

# ──────────────────────────────────────────────────────────────────────
# Caps — as multiple of broker_equity (anchor)
# Per project_cluster_exposure_model.md. Tightened a notch since paper sizing
# was also tightened today (fleet_sizing.json v7) — keep cluster/per-class
# proportions sensible.
# ──────────────────────────────────────────────────────────────────────

CLUSTER_CAPS: dict[str, float] = {
    "FX_USD_LONG":     2.0,   # spec said 8x; tightened to match new 1.0x per-position FX cap
    "FX_USD_SHORT":    2.0,   # same
    "FX_JPY":          1.5,   # tightened from spec 5x
    "EQUITY_BETA":     2.5,   # 2026-04-27 v8: bumped 0.9→2.5 to allow 1 MNQ futures contract (~$54K) alongside small ETF positions
    "EQUITY_BETA_SHORT": 2.0, # bumped 0.6→2.0 to match
    "SHORT_VOL":       0.3,   # tightened from spec 0.5x — hard cap (convexity tail risk)
    "LONG_VOL":        0.6,   # tightened from spec 1.0x
    "DURATION":        2.0,   # bumped 1.0→2.0 to allow 1 ZN-class futures contract
    "METALS":          0.9,   # tightened from spec 1.5x
    "INTERNATIONAL_EQ": 1.2,  # tightened from spec 2.0x
}

SINGLE_INSTRUMENT_CAP_X         = 0.6   # stocks/ETFs — no single position > 60% of equity
FUTURES_SINGLE_INSTRUMENT_CAP_X = 2.0   # futures cap higher because risk = margin not notional (1 MNQ ~ $54K notional but ~$5.4K margin)
# 2026-05-01: FX needs a separate higher cap. FX risk is bounded by pip-stop
# distance (15 pips on a $30K notional trade = ~$45 risk), not by notional
# itself. Treating FX same as stocks (0.6x cap) was blocking argus_gbpusd
# entries: full-size at $31K exceeded 0.6x but IdealPro min is $25K, leaving
# no valid trade window. fleet_sizing.json v6 already allows fx 20x anchor;
# this matches that. Net portfolio risk still bounded by per-trade risk_pct
# and FX_USD cluster cap.
FX_SINGLE_INSTRUMENT_CAP_X      = 5.0   # one FX pair up to 5x equity (anchor=$31K -> $155K notional cap)
# 2026-05-07: tightened 8.0 -> 1.5. Previous value let the fleet stack so
# many positions that maintenance-margin cushion crashed to 1.57% — IBKR
# margin alert + near-forced-liquidation. 1.5x means total fleet notional
# can be 150% of equity (small leverage allowed), but no more. This is the
# fleet-wide guardrail that prevents what happened on 5/7 from recurring.
TOTAL_NOTIONAL_CAP_X            = 1.5

# Symbols that count as futures for the purposes of the per-instrument cap.
FUTURES_SYMBOLS: set[str] = {
    "MNQ", "MES", "MYM", "M2K",  # micro futures
    "NQ", "ES",                   # full-size index futures
    "ZN", "ZF", "ZT",             # rate futures
}

# FX pairs route through FX_SINGLE_INSTRUMENT_CAP_X instead of the stock cap.
# Match either canonical 6-char form (USDJPY) or dotted form (USD.JPY).
FX_SYMBOLS: set[str] = {
    "USDJPY", "EURUSD", "GBPUSD", "AUDUSD", "USDCAD", "USDCHF", "NZDUSD",
    "EURJPY", "GBPJPY", "CADJPY", "CHFJPY", "AUDJPY", "NZDJPY",
    "EURGBP", "EURCHF", "EURAUD", "EURCAD", "EURNZD",
    "GBPCHF", "GBPAUD", "GBPCAD", "GBPNZD",
    "AUDCHF", "AUDCAD", "AUDNZD",
    "NZDCHF", "NZDCAD", "CADCHF",
}

# ──────────────────────────────────────────────────────────────────────


def get_cluster_memberships(symbol: str, direction: str) -> list[str]:
    """Return the cluster names this position belongs to. Empty list if symbol
    is unknown — callers decide how to handle (typically: log warning, allow trade)."""
    if not symbol:
        return []
    sym = symbol.upper()
    if sym not in CLUSTER_MAP:
        return []
    return CLUSTER_MAP[sym].get(direction, [])


def _resolve_direction(t: dict) -> str:
    """Resolve direction from an open-trade record. Falls back to stop_px > entry_px → short."""
    explicit = t.get("direction") or t.get("side")
    if explicit in ("long", "short"):
        return explicit
    e = t.get("entry_px")
    s = t.get("stop_px")
    if e is not None and s is not None:
        try:
            return "short" if float(s) > float(e) else "long"
        except (TypeError, ValueError):
            pass
    return "long"


def _scan_open_positions() -> list[dict]:
    """Scan strategy heartbeat.json files for currently open positions.
    Same source as the dashboard /api/positions_open."""
    positions = []
    hb_roots = [
        REPO / "argus_flow" / "logs",
        REPO / "forge" / "logs",
        REPO / "apollo" / "logs",
        REPO / "hermes" / "logs",
        REPO / "titan" / "logs",
    ]
    for root in hb_roots:
        if not root.exists():
            continue
        for hb_path in list(root.glob("*/heartbeat.json")) + list(root.glob("heartbeat.json")):
            try:
                hb = json.loads(hb_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            system = hb.get("system") or hb_path.parent.name
            ot = hb.get("open_trade")
            if ot and isinstance(ot, dict):
                # Single-symbol strategies — symbol is implicit (the strategy's own instrument)
                sym = ot.get("symbol") or _system_default_symbol(system)
                positions.append({
                    "system": system,
                    "symbol": sym,
                    "direction": _resolve_direction(ot),
                    "entry_px": ot.get("entry_px"),
                    "size": ot.get("position_size") or ot.get("size") or 0,
                })
            ots = hb.get("open_trades")
            if ots and isinstance(ots, dict):
                for key, t in ots.items():
                    positions.append({
                        "system": system,
                        "symbol": key,  # the dict key IS the instrument for multi-symbol runners
                        "direction": _resolve_direction(t),
                        "entry_px": t.get("entry_px"),
                        "size": t.get("position_size") or t.get("size") or 0,
                    })
    return positions


# Map single-instrument runner system name → its default symbol (for runners that store
# open_trade without an explicit symbol field).
_SYSTEM_DEFAULT_SYMBOL = {
    "spy_mean_rev": "SPY",
    "vix_intraday": "UVXY",
    "vix_revert":   "SPY",
    "gld_pm_long":  "GLD",
    "nq_overnight": "MNQ",
    "nq_london_close": "MNQ",
    "fomc_drift":   "SPY",
    "wick_gbpusd":  "GBPUSD",
    "aud_asian_breakout": "AUDUSD",
}


def _system_default_symbol(system: str) -> str:
    return _SYSTEM_DEFAULT_SYMBOL.get(system, "")


def compute_cluster_exposure() -> dict:
    """Aggregate current open positions into cluster + per-symbol notional totals.

    Returns:
        {
          "by_cluster": {cluster: notional_usd},
          "by_symbol":  {symbol: notional_usd},
          "total_notional": float,
          "positions": list[dict],  # raw scanned positions for debugging
        }

    Notional is computed as |entry_px * size|. For FX, this uses the pair's quote
    convention which understates USD-equivalent for non-USD-quote pairs (e.g. JPY
    pairs); acceptable approximation for guard-rail purposes.
    """
    by_cluster: dict[str, float] = {}
    by_symbol: dict[str, float] = {}
    total = 0.0
    positions = _scan_open_positions()
    for p in positions:
        try:
            entry = float(p.get("entry_px") or 0)
            size = float(p.get("size") or 0)
        except (TypeError, ValueError):
            continue
        notional = abs(entry * size)
        if notional <= 0:
            continue
        sym = (p["symbol"] or "").upper()
        by_symbol[sym] = by_symbol.get(sym, 0.0) + notional
        for c in get_cluster_memberships(sym, p["direction"]):
            by_cluster[c] = by_cluster.get(c, 0.0) + notional
        total += notional
    return {
        "by_cluster": by_cluster,
        "by_symbol": by_symbol,
        "total_notional": total,
        "positions": positions,
    }


def would_breach_cluster_cap(
    symbol: str,
    direction: str,
    notional_usd: float,
) -> Optional[str]:
    """Pre-trade check: would adding this proposed position breach any cap?

    Returns None if the trade is OK to submit. Returns a string identifying the
    breached cap (e.g. "CLUSTER:FX_USD_LONG", "SINGLE_INSTRUMENT:USDJPY",
    "TOTAL_NOTIONAL") if the trade should be rejected.

    Reads the current fleet exposure from heartbeat files. Conservative: the
    proposed notional is added to current exposure, then checked against caps.
    """
    if notional_usd <= 0 or not symbol:
        return None

    try:
        from helio.fleet_sizing import get_sizing_anchor_usd
        anchor = float(get_sizing_anchor_usd())
    except Exception as exc:
        log.warning(f"cluster_exposure: anchor unavailable, skipping cap check: {exc}")
        return None
    if anchor <= 0:
        return None

    expo = compute_cluster_exposure()
    sym = symbol.upper()

    # 1. Single-instrument cap. Three regimes by asset class:
    #   - Futures: 2x (margin-not-notional risk)
    #   - FX: 5x (pip-stop bounds risk; notional is leverage-implied)
    #   - Everything else (stocks/ETFs): 0.6x
    sym_after = expo["by_symbol"].get(sym, 0.0) + notional_usd
    if sym in FUTURES_SYMBOLS:
        cap_x = FUTURES_SINGLE_INSTRUMENT_CAP_X
    elif sym in FX_SYMBOLS:
        cap_x = FX_SINGLE_INSTRUMENT_CAP_X
    else:
        cap_x = SINGLE_INSTRUMENT_CAP_X
    sym_cap_usd = cap_x * anchor
    if sym_after > sym_cap_usd:
        return f"SINGLE_INSTRUMENT:{sym}"

    # 2. Per-cluster caps
    for cluster in get_cluster_memberships(sym, direction):
        current = expo["by_cluster"].get(cluster, 0.0)
        cap_usd = CLUSTER_CAPS.get(cluster, float("inf")) * anchor
        if current + notional_usd > cap_usd:
            return f"CLUSTER:{cluster}"

    # 3. Total notional catch-all
    if expo["total_notional"] + notional_usd > TOTAL_NOTIONAL_CAP_X * anchor:
        return "TOTAL_NOTIONAL"

    return None
