"""Runtime invariant: killed strategies own no broker exposure.

The static test (test_killed_strategy_invariant.py) proves the config-level
invariant — killed strategies have factor=0.0, no_restart=True, not on the
real-money allowlist. This module is the runtime side: it inspects broker
positions and flags any position attributable to a killed strategy unless
an explicit unwind ticket exists.

Codex audit 2026-05-18 X6:
  > Fleet_monitor has `no_restart` for some killed strategies, but state
  > files can still report open trades and broker positions can remain.
  > Need: killed → no restart, no new entry, no owned exposure unless
  > explicit unwind ticket exists.

This addresses the third clause."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# Map killed strategies to the symbols they traded. Position attribution at
# the broker is by symbol, so we have to know which broker symbols belong
# to which killed strategy. Conservative: any position in one of these
# symbols, when no other active strategy claims it, is flagged.
#
# This list should be maintained alongside helio.roi_filter.KILLED_STRATEGY_CUTOFFS.
KILLED_STRATEGY_SYMBOLS: dict[str, tuple[str, ...]] = {
    # Tier 1 explicit kills
    "forge_spy_mean_rev":     ("SPY",),
    "forge_multi_orb":        ("QQQ", "MES"),
    "forge_vix_intraday":     ("UVXY", "VIX"),
    "forge_nq_london_close":  ("MNQ", "NQ"),
    # 2026-05-23 rigor sprint killer:
    "forge_nq_overnight":     ("MNQ", "NQ"),
    "forge_spy_trend_follower": ("SPY",),
    # PEAD's 18-name Apollo watchlist (forge_pead 5/19 + Apollo core)
    "forge_pead":             (
        "AAPL", "AMD", "AMZN", "ARM", "GILD", "GOOGL", "INTC",
        "KLAC", "LRCX", "MRNA", "MU", "PEP", "PLTR", "REGN",
        "SOFI", "TMO", "TSM", "WMT",
    ),
    # 2026-05-20 sunset batch — formalized into kill registry 2026-05-24
    "forge_vix_carry":        ("SVXY", "VIX"),
    "forge_coint_pairs":      ("KO", "PEP", "XOM", "CVX", "HD", "LOW",
                                  "GLD", "SLV", "EWZ", "EWW", "TLT", "IEF",
                                  "V", "MA", "MSFT", "GOOGL"),
    "forge_aud_asian_breakout": ("AUDUSD",),
    "forge_cuebanks":         ("YM", "MYM"),
    "forge_mamba":            ("YM", "MYM", "NQ", "MNQ"),
    "forge_tori":             ("YM", "MYM"),
    "forge_jpy_pm_short":     ("USDJPY",),
    "forge_wick_gbpusd":      ("GBPUSD",),
    "forge_vix_revert":       ("UVXY", "VIX"),
    "forge_fomc_drift":       ("SPY",),
    "forge_tom_international": ("EEM", "EWJ", "VGK", "EFA", "FXI", "INDA"),
    "forge_rebalance":        ("SPY",),
    "forge_atlas":            ("SPY",),  # regime classifier; SPY proxy
    "forge_themis":           ("SPY",),
    "forge_gdx_gld":          ("GDX", "GLD"),
    "apollo":                  (),  # scanner; no direct positions
    "hermes":                  (),  # scanner
    "titan":                   (),  # scanner
    "argus_gbpusd":           ("GBPUSD",),
    "argus_usdjpy":           ("USDJPY",),
    "argus_cadjpy":           ("CADJPY",),
    # 2026-05-25: gate-killed before deployment (no runner ever shipped).
    # See docs/AUDIT_2026_05_25_PART2/OVERNIGHT_DRIFT_KILL.md.
    "forge_overnight_drift_qqq": ("SPY", "QQQ", "IWM"),
    # 2026-05-25 (same evening): gate-killed at every parameter combo.
    # H1 (2007-2016) strong, H2 (2016-2026) erodes — central-bank
    # backstops likely amputated the credit-leads-equity signal.
    # See docs/AUDIT_2026_05_25_PART2/CREDIT_SPREAD_KILL.md.
    "forge_credit_spread_regime": ("SPY",),
}


UNWIND_TICKETS_PATH = Path("argus_flow/configs/killed_strategy_unwind_tickets.json")


def _load_unwind_tickets(path: Path | str | None = None) -> list[dict]:
    """Load active unwind tickets. Missing file = no tickets.

    Schema (per ticket):
      {
        "strategy": "forge_vix_intraday",
        "symbol": "UVXY",
        "max_qty": 283,
        "operator": "ksmith2322",
        "signed_at": "2026-05-12T15:00:00+00:00",
        "expires_at": "2026-05-13T00:00:00+00:00",
        "reason": "Manually flatten UVXY orphan from 5/12 emergency shutdown."
      }
    """
    p = Path(path) if path is not None else UNWIND_TICKETS_PATH
    try:
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    tickets = data.get("tickets") if isinstance(data, dict) else data
    if not isinstance(tickets, list):
        return []
    return tickets


def _ticket_is_active(ticket: dict, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    expires = ticket.get("expires_at")
    if not expires:
        return False
    try:
        exp_dt = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
    except ValueError:
        return False
    return now < exp_dt


def find_killed_strategy_orphans(
    broker_positions: dict[str, dict],
    active_strategy_symbols: Iterable[str] = (),
    tickets: list[dict] | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Return a list of broker positions that belong to a killed strategy
    without a matching active unwind ticket. Each entry:
        {strategy, symbol, direction, qty, has_unwind_ticket}.

    `broker_positions` is a {symbol: {direction, qty, ...}} map matching the
    position_monitor `get_ibkr_positions()` shape.

    `active_strategy_symbols` is a set of symbols owned by ACTIVE strategies.
    Used to short-circuit: a SPY position is fine if forge_spy_trend_follower
    (active) claims it, even though forge_spy_mean_rev (killed) also traded SPY."""
    if tickets is None:
        tickets = _load_unwind_tickets()
    active_tickets = [t for t in tickets if _ticket_is_active(t, now)]
    active_set = {str(s).upper() for s in active_strategy_symbols}

    orphans: list[dict] = []
    for killed_strat, syms in KILLED_STRATEGY_SYMBOLS.items():
        for sym in syms:
            sym_u = sym.upper()
            pos = broker_positions.get(sym_u) or broker_positions.get(sym)
            if not pos:
                continue
            direction = str(pos.get("direction", "FLAT")).upper()
            qty = float(pos.get("qty", 0) or 0)
            if direction == "FLAT" or qty == 0:
                continue
            if sym_u in active_set:
                # An active strategy claims this symbol; not a killed orphan.
                continue
            has_ticket = any(
                str(t.get("strategy", "")).lower() == killed_strat.lower()
                and str(t.get("symbol", "")).upper() == sym_u
                and abs(qty) <= float(t.get("max_qty", 0) or 0)
                for t in active_tickets
            )
            orphans.append({
                "strategy": killed_strat,
                "symbol": sym_u,
                "direction": direction,
                "qty": qty,
                "has_unwind_ticket": has_ticket,
            })
    return orphans
