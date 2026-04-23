"""helio/fleet_sizing.py — Central sizing for the whole fleet.

2026-04-23.v5: fallback + ceiling REMOVED. Preprod mirrors prod.
  - Anchor = broker account equity, always. No fallback_anchor_usd, no
    max_anchor_usd clamp. If the broker read fails and the last-known-good
    cache expires, get_sizing_anchor_usd() raises BrokerEquityUnavailableError
    and runners enter READ_ONLY (manage open positions, refuse new entries).
  - risk_pct is auto-selected per strategy based on measured live performance
    via `get_effective_risk_pct(strategy_label)`. More trades + better PF = higher tier.
  - Hard ceiling: 3% per trade. Drawdown brake halves the tier when current
    dd > 50% of peak_pnl.
  - Paper → live transition: no code change; same path reads real broker equity.

The old `get_initial_capital_usd()` is kept as an alias that returns the
current broker equity, so older callers continue to work without edits.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[1]
_CONFIG_PATH = _REPO / "argus_flow" / "configs" / "fleet_sizing.json"
_RISK_OVERSIGHT_PATH = _REPO / "argus_flow" / "logs" / "risk_oversight_report.json"
_PORTFOLIO_RISK_STATE = _REPO / "argus_flow" / "logs" / "_risk" / "portfolio_risk_state.json"

_FALLBACK_RISK_PCT = 0.005  # unproven-tier default (not a money fallback — just the baseline tier risk%)

_cached_config: dict[str, Any] | None = None
_cached_anchor: tuple[float, float] | None = None  # (equity, ts)  last-known-good from broker
_anchor_lock = threading.Lock()
_ANCHOR_CACHE_S = 30  # re-read broker equity at most every 30s during normal operation
_STALENESS_CEILING_S = 60  # after a broker outage, keep serving last-known-good only this long before raising


class BrokerEquityUnavailableError(RuntimeError):
    """Broker equity cannot be read and last-known-good cache has expired.
    Runners catching this should log CRITICAL, enter READ_ONLY mode (manage
    open positions, refuse new entries), and retry on the next cycle. Never
    fall back to a hardcoded anchor — paper and prod both go through this
    same code path."""


def _load_config() -> dict[str, Any]:
    global _cached_config
    if _cached_config is not None:
        return _cached_config
    try:
        _cached_config = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Minimal in-memory default if config file is missing — tiers/caps only,
        # NO money defaults. If broker is also unavailable, sizing will raise.
        _cached_config = {
            "max_risk_pct_per_trade_ceiling": 0.03,
            "fleet_max_open_risk_pct": 0.06,
            "tiers": [{"name": "unproven", "min_valid_trades": 0, "min_profit_factor": 0, "risk_pct": _FALLBACK_RISK_PCT}],
            "tier_window_days": 90,
            "drawdown_brake_threshold": 0.5,
            "notional_caps_by_asset_class": {"stock": 2.0, "etf": 2.0, "fx": 20.0, "micro_future": 5.0},
            "_fallback": True,
        }
    return _cached_config


def invalidate_cache() -> None:
    """Force re-read of config + broker equity. Called by tests or post-edit."""
    global _cached_config, _cached_anchor
    _cached_config = None
    _cached_anchor = None


def get_broker_equity_usd() -> float | None:
    """Read current broker equity from risk_oversight_report.json.

    Returns None if the file is missing/stale/malformed. The caller decides
    what to do with None (this module's get_sizing_anchor_usd handles it via
    stale-cache + raise)."""
    try:
        data = json.loads(_RISK_OVERSIGHT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    eq = data.get("broker_truth", {}).get("account_equity_usd")
    try:
        return float(eq) if eq is not None else None
    except (TypeError, ValueError):
        return None


def get_sizing_anchor_usd() -> float:
    """Current anchor capital = broker equity. No fallback, no ceiling.

    Behavior contract:
      - Fresh broker read (>0) → return it, refresh last-known-good cache.
      - Broker unavailable, but last-known-good cache < STALENESS_CEILING old
        → return last-known-good (brief TWS disconnect tolerated).
      - Broker unavailable, cache stale/absent → raise BrokerEquityUnavailableError.
        Runner catches and enters READ_ONLY mode.

    Cached briefly (30s) to avoid repeated disk reads on hot paths. Call
    `invalidate_cache()` after manually patching state."""
    global _cached_anchor
    import time
    now = time.time()
    with _anchor_lock:
        # Return fresh cache if recent
        if _cached_anchor is not None and (now - _cached_anchor[1]) < _ANCHOR_CACHE_S:
            return _cached_anchor[0]
        eq = get_broker_equity_usd()
        if eq is not None and eq > 0:
            _cached_anchor = (eq, now)
            return eq
        # Broker returned None or non-positive — try last-known-good window
        if _cached_anchor is not None and (now - _cached_anchor[1]) < _STALENESS_CEILING_S:
            _log.warning(
                "BROKER_EQUITY_STALE: broker unavailable; serving last-known-good $%.2f (age=%ds)",
                _cached_anchor[0], int(now - _cached_anchor[1]),
            )
            return _cached_anchor[0]
        # No fresh read, no usable cache — refuse to size
        raise BrokerEquityUnavailableError(
            f"broker equity unavailable and last-known-good cache "
            f"{'expired' if _cached_anchor else 'absent'}; "
            f"risk_oversight_report path: {_RISK_OVERSIGHT_PATH}"
        )


# Back-compat alias — old callers continue working unchanged.
def get_initial_capital_usd() -> float:
    return get_sizing_anchor_usd()


def get_max_risk_pct_ceiling() -> float:
    return float(_load_config().get("max_risk_pct_per_trade_ceiling", 0.03))


def get_fleet_max_open_risk_pct() -> float:
    return float(_load_config().get("fleet_max_open_risk_pct", 0.06))


def get_fleet_max_open_risk_usd() -> float:
    """Derived — anchor × max_open_risk_pct. Scales with account."""
    return get_sizing_anchor_usd() * get_fleet_max_open_risk_pct()


# ── Strategy performance + tier evaluation ───────────────────────

_TRADE_SOURCES: dict[str, dict] = {
    "argus_usdjpy":        {"path": "argus_flow/logs/usdjpy/trades.csv",  "valid_filter": True,  "ts": "ts"},
    "argus_gbpusd":        {"path": "argus_flow/logs/gbpusd/trades.csv",  "valid_filter": True,  "ts": "ts"},
    "argus_cadjpy":        {"path": "argus_flow/logs/cadjpy/trades.csv",  "valid_filter": True,  "ts": "ts"},
    "forge_gld_pm_long":   {"path": "forge/logs/gld_pm_long/trades.csv",  "valid_filter": False, "ts": "ts"},
    "forge_wick_gbpusd":   {"path": "forge/logs/wick_gbpusd/trades.csv",  "valid_filter": False, "ts": "ts"},
    "forge_nq_overnight":  {"path": "forge/logs/nq_overnight/trades.csv", "valid_filter": False, "ts": "ts"},
    "forge_jpy_pm_short":  {"path": "forge/logs/jpy_pm_short/trades.csv", "valid_filter": False, "ts": "ts"},
    "forge_gdx_gld":       {"path": "forge/logs/gdx_gld/trades.csv",      "valid_filter": False, "ts": "exit_date"},
}


def _parse_ts(s: str):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s.split(".")[0], fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def compute_strategy_stats(strategy_label: str, window_days: int | None = None) -> dict:
    """Read the strategy's trades.csv, compute tier-input stats over a recent
    window. Returns {trades, wins, losses, profit_factor, win_rate, pnl_usd}.

    Trades before the strategy's `live_cutoff` (config) are excluded from
    tier stats — prevents gdx_gld's 20-year back-fill from faking a tier
    promotion.
    """
    cfg = _load_config()
    if window_days is None:
        window_days = int(cfg.get("tier_window_days", 90))
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)

    # Per-strategy live cutoff: trades before this ts don't count toward
    # tier (e.g. back-fill). Take the MAX of window_cutoff and live_cutoff.
    live_cutoffs = cfg.get("strategy_live_cutoffs", {}) or {}
    live_cut_raw = live_cutoffs.get(strategy_label)
    if live_cut_raw:
        live_cut = _parse_ts(live_cut_raw)
        if live_cut and live_cut > cutoff:
            cutoff = live_cut

    spec = _TRADE_SOURCES.get(strategy_label)
    stats = {
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "profit_factor": 0.0,
        "win_rate": 0.0,
        "pnl_usd": 0.0,
        "peak_pnl_usd": 0.0,
        "current_drawdown_usd": 0.0,
        "current_drawdown_pct_of_peak": 0.0,
    }
    if not spec:
        return stats

    p = _REPO / spec["path"]
    if not p.exists():
        return stats

    wins_sum = 0.0
    losses_sum = 0.0
    count = 0
    wins = 0
    losses = 0
    pnl_total = 0.0
    trade_pnls: list[float] = []
    try:
        with open(p, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if spec["valid_filter"] and str(r.get("experiment_valid", "")).lower() != "true":
                    continue
                ts = _parse_ts(r.get(spec["ts"]) or r.get("entry_ts") or r.get("entry_date") or "")
                if ts is None or ts < cutoff:
                    continue
                pnl_raw = r.get("pnl_usd")
                if pnl_raw in (None, ""):
                    continue
                try:
                    pnl = float(pnl_raw)
                except ValueError:
                    continue
                count += 1
                pnl_total += pnl
                trade_pnls.append(pnl)
                if pnl > 0:
                    wins += 1
                    wins_sum += pnl
                elif pnl < 0:
                    losses += 1
                    losses_sum += abs(pnl)
    except Exception:
        return stats

    stats["trades"] = count
    stats["wins"] = wins
    stats["losses"] = losses
    stats["pnl_usd"] = round(pnl_total, 2)
    stats["win_rate"] = round(wins / count, 3) if count else 0.0
    if losses_sum > 0:
        stats["profit_factor"] = round(wins_sum / losses_sum, 3)
    elif wins_sum > 0:
        stats["profit_factor"] = 999.0  # no losses — effectively infinite PF
    running = 0.0
    peak = 0.0
    for pnl in trade_pnls:
        running += pnl
        peak = max(peak, running)
    current_dd = max(0.0, peak - running)
    stats["peak_pnl_usd"] = round(peak, 2)
    stats["current_drawdown_usd"] = round(current_dd, 2)
    stats["current_drawdown_pct_of_peak"] = round(current_dd / peak, 4) if peak > 0 else 0.0
    return stats


def get_effective_risk_pct(strategy_label: str) -> dict:
    """Return {risk_pct, tier_name, stats, drawdown_brake_applied} for a strategy.

    Selects the highest tier the strategy qualifies for based on recent stats,
    applies drawdown brake if current_dd > threshold of peak, clamps to ceiling.
    """
    cfg = _load_config()
    tiers = cfg.get("tiers", [])
    # Ensure tiers are sorted ascending by risk_pct so we can pick highest match
    sorted_tiers = sorted(tiers, key=lambda t: t.get("risk_pct", 0))

    stats = compute_strategy_stats(strategy_label)
    chosen = sorted_tiers[0] if sorted_tiers else {"name": "unproven", "risk_pct": _FALLBACK_RISK_PCT}
    for t in sorted_tiers:
        if stats["trades"] >= t.get("min_valid_trades", 0) and stats["profit_factor"] >= t.get("min_profit_factor", 0):
            chosen = t

    risk_pct = float(chosen.get("risk_pct", _FALLBACK_RISK_PCT))
    tier_name = chosen.get("name", "unproven")
    brake_applied = False

    # Per-strategy drawdown brake. Gate: only trigger when the strategy has
    # enough sample (>=10 trades) AND its own pnl has peaked and pulled back.
    # The prior version read a fleet-wide R-multiple counter that gave false
    # positives on session resets — not what we want.
    dd_pct = float(stats.get("current_drawdown_pct_of_peak", 0.0) or 0.0)
    dd_threshold = float(cfg.get("drawdown_brake_threshold", 0.5) or 0.5)
    if stats["trades"] >= 10 and dd_pct > dd_threshold:
        risk_pct *= 0.5
        brake_applied = True

    # Clamp to ceiling
    ceiling = get_max_risk_pct_ceiling()
    if risk_pct > ceiling:
        risk_pct = ceiling

    return {
        "strategy": strategy_label,
        "risk_pct": round(risk_pct, 5),
        "tier_name": tier_name,
        "stats": stats,
        "drawdown_brake_applied": brake_applied,
        "ceiling": ceiling,
    }


def compute_risk_usd(strategy_or_pct=None, strategy_label: str | None = None) -> float:
    """Dollar risk for a trade. Two call signatures supported:

      compute_risk_usd(0.01)                    # legacy: fixed risk_pct × anchor
      compute_risk_usd(strategy_label="x")      # new: tier-based
      compute_risk_usd("forge_gld_pm_long")     # new: positional strategy label

    Prefer the strategy_label form for prod. The legacy form is kept for
    back-compat with code that hasn't been retrofitted.
    """
    anchor = get_sizing_anchor_usd()
    # Label-based path
    if isinstance(strategy_or_pct, str):
        info = get_effective_risk_pct(strategy_or_pct)
        return info["risk_pct"] * anchor
    if strategy_label:
        info = get_effective_risk_pct(strategy_label)
        return info["risk_pct"] * anchor
    # Legacy numeric risk_pct path — clamp to ceiling
    try:
        pct = float(strategy_or_pct)
    except (TypeError, ValueError):
        pct = _FALLBACK_RISK_PCT
    ceiling = get_max_risk_pct_ceiling()
    if pct > ceiling:
        pct = ceiling
    return pct * anchor


def pnl_pct_of_fleet(pnl_usd: float) -> float:
    """Percent of current anchor. With dynamic anchor this reflects % return
    on the live account, not a fixed $10K base."""
    anchor = get_sizing_anchor_usd()
    if anchor <= 0:
        return 0.0
    return (float(pnl_usd) / anchor) * 100.0


# ── Notional cap (buying-power ceiling) ──────────────────────────

def max_notional_usd(asset_class: str) -> float:
    """Max position notional that a real funded account at this anchor could
    actually hold, given the asset class's typical leverage."""
    caps = _load_config().get("notional_caps_by_asset_class", {})
    mult = float(caps.get(asset_class, 1.0))
    return get_sizing_anchor_usd() * mult


# _aligned_env() removed 2026-04-23: ARGUS_DEFAULT_ACCOUNT_EQUITY_USD env var
# was a back-compat safety net for code paths that read equity at module load.
# Now all sizing flows through get_sizing_anchor_usd() which is broker-truth
# only. If any residual code reads the env var, it's either dead or needs
# refactor — not something we want to silently backstop.
