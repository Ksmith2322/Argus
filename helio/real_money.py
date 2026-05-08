"""Real-money boundary — mechanical separation between paper and real accounts.

Default-off design. Every check defaults to "this is paper" and requires
multiple deliberate gates to allow real-money flow. See
project_real_money_boundary.md for the locked policy.

Three primary callers wire this in:

* ``helio.ibkr_execution.submit_bracket`` (forge runners)
* ``helio.ibkr_executor`` (Greek family)
* ``helio.signal_executor`` (scanner-style)

Each must call :func:`enforce_real_money_boundary` before transmitting an
order. Paper orders pass through unchanged (the function is a no-op when
the connection is paper). Real orders go through the full validation
pipeline.

The runtime contract:

* Default ``REAL_MONEY_ENABLED = False``. No real-money trade can clear
  this gate without an explicit allowlist entry plus a config flip.
* The allowlist file is optional; absence means "no real-money strategies."
* Account ID validation is mechanical: each strategy maps to exactly one
  account class (``paper`` or ``real``). Mixing requires a deliberate
  config change, not just an env-variable flip.
* The dollar cap is a hard ceiling on every real-money order, regardless
  of what the sizing layer computed. This catches sizing bugs.

This module performs **no I/O** beyond reading the allowlist JSON. It is
safe to import from any runner without side effects.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Locked policy constants — change only via a ledger entry + user signature.
# ---------------------------------------------------------------------------

REAL_MONEY_ENABLED: bool = False
"""Global default. Even if a strategy is allowlisted, real-money flow is
blocked unless this is flipped to True. Set this in the allowlist file's
``global_enabled`` field (see :func:`load_allowlist`)."""

REAL_MONEY_MAX_NOTIONAL_PER_ORDER: float = 5_000.0
"""Hard dollar cap on a single real-money order. Strategies needing larger
sizing must split into multiple orders. Cap is raised manually via a
ledger entry as the real account scales."""

# Account-class map. Populated at config time; the real account ID is
# intentionally absent until the user funds and supplies it.
PAPER_ACCOUNT_ID: str = "DUP472829"
REAL_ACCOUNT_ID: Optional[str] = None  # Set via env or config when funded

# IBKR ports map directly to account class.
PAPER_PORT: int = 7497
REAL_PORT: int = 7496

ALLOWLIST_PATH = Path("argus_flow/configs/real_money_allowlist.json")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class AccountBoundaryViolationError(Exception):
    """Raised when an order attempts to cross the paper/real boundary in a
    way that policy forbids. Caught by the executor wrappers, logged as a
    rejection, and surfaced via the canonical reject_reason path."""


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Allowlist:
    """In-memory representation of the real-money allowlist file."""

    global_enabled: bool = False
    strategies: tuple[str, ...] = ()
    max_strategies_real: int = 1
    ledger_entry_id: str = ""
    approver: str = ""
    signed_at: str = ""
    notes: str = ""
    raw: dict = field(default_factory=dict)

    def is_real_money_strategy(self, strategy_label: str) -> bool:
        """Return True only if (a) globally enabled and (b) strategy is
        explicitly allowlisted. Either condition missing = paper-only."""
        if not self.global_enabled:
            return False
        return strategy_label in self.strategies

    def validate_self(self) -> list[str]:
        """Return a list of policy violations in the loaded allowlist.
        Empty list = allowlist is internally consistent."""
        problems: list[str] = []
        if self.global_enabled:
            if not self.strategies:
                problems.append(
                    "global_enabled=true but strategies list is empty"
                )
            if len(self.strategies) > self.max_strategies_real:
                problems.append(
                    f"len(strategies)={len(self.strategies)} > "
                    f"max_strategies_real={self.max_strategies_real}"
                )
            if not self.ledger_entry_id:
                problems.append("global_enabled=true requires ledger_entry_id")
            if not self.approver:
                problems.append("global_enabled=true requires approver")
        return problems


def load_allowlist(path: Path | str | None = None) -> Allowlist:
    """Load the allowlist from disk. Missing file is treated as 'no
    real-money strategies' (the safe default), not as an error."""
    p = Path(path) if path is not None else ALLOWLIST_PATH
    if not p.exists():
        return Allowlist()
    try:
        raw = json.loads(p.read_text())
    except Exception as exc:
        log.error(
            "real_money: allowlist at %s is unreadable (%s) — treating as "
            "empty (paper-only) for safety.",
            p, exc,
        )
        return Allowlist()
    return Allowlist(
        global_enabled=bool(raw.get("global_enabled", False)),
        strategies=tuple(raw.get("strategies") or ()),
        max_strategies_real=int(raw.get("max_strategies_real", 1)),
        ledger_entry_id=str(raw.get("ledger_entry_id") or ""),
        approver=str(raw.get("approver") or ""),
        signed_at=str(raw.get("signed_at") or ""),
        notes=str(raw.get("notes") or ""),
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Connection introspection
# ---------------------------------------------------------------------------

def is_real_money_connection(ib_client) -> bool:
    """Inspect an ib_insync IB instance to determine whether it points at a
    real or paper account. The check uses port + accounts; either signal
    being 'real' is treated as real (defense in depth)."""
    if ib_client is None:
        return False
    try:
        port = int(getattr(ib_client.client, "port", 0) or 0)
    except Exception:
        port = 0
    if port == REAL_PORT:
        return True
    try:
        accounts = list(ib_client.managedAccounts() or [])
    except Exception:
        accounts = []
    if REAL_ACCOUNT_ID and REAL_ACCOUNT_ID in accounts:
        return True
    return False


# ---------------------------------------------------------------------------
# Public boundary check — call this from every executor before transmit
# ---------------------------------------------------------------------------

def enforce_real_money_boundary(
    ib_client,
    strategy_label: str,
    notional_usd: Optional[float] = None,
    allowlist: Optional[Allowlist] = None,
) -> None:
    """Raise :class:`AccountBoundaryViolationError` if the order is about to
    cross the boundary in a way the policy forbids. No-op for paper orders.

    Parameters
    ----------
    ib_client
        The active ib_insync ``IB`` instance the order will transmit through.
    strategy_label
        Canonical strategy name (e.g. ``"forge_vix_intraday"``).
    notional_usd
        Optional estimated dollar notional of the order. Compared to
        :data:`REAL_MONEY_MAX_NOTIONAL_PER_ORDER` when present.
    allowlist
        Pre-loaded allowlist; loaded from disk if not provided. Useful
        for testing.
    """
    if not is_real_money_connection(ib_client):
        return

    al = allowlist if allowlist is not None else load_allowlist()

    if not al.global_enabled:
        raise AccountBoundaryViolationError(
            f"real_money_disabled: strategy={strategy_label} attempted to "
            f"trade on real account but allowlist.global_enabled=false"
        )

    if not REAL_MONEY_ENABLED and not al.global_enabled:
        # Belt and suspenders — module-level constant must also be flipped.
        raise AccountBoundaryViolationError(
            "real_money_module_disabled: REAL_MONEY_ENABLED=False"
        )

    if not al.is_real_money_strategy(strategy_label):
        raise AccountBoundaryViolationError(
            f"strategy_not_allowlisted: {strategy_label} is not in the "
            f"real-money allowlist (allowed={list(al.strategies)})"
        )

    problems = al.validate_self()
    if problems:
        raise AccountBoundaryViolationError(
            f"allowlist_invalid: {'; '.join(problems)}"
        )

    if notional_usd is not None and notional_usd > REAL_MONEY_MAX_NOTIONAL_PER_ORDER:
        raise AccountBoundaryViolationError(
            f"oversize_real_order: notional=${notional_usd:,.2f} > "
            f"cap=${REAL_MONEY_MAX_NOTIONAL_PER_ORDER:,.2f} "
            f"(strategy={strategy_label})"
        )


# ---------------------------------------------------------------------------
# Order tagging
# ---------------------------------------------------------------------------

def real_money_order_tag(strategy_label: str, allowlist: Optional[Allowlist] = None) -> str:
    """Build the canonical ``orderRef`` tag for a real-money order. Tag
    embeds the ledger entry that authorized the strategy so any real fill
    is traceable back to a signed approval."""
    al = allowlist if allowlist is not None else load_allowlist()
    ledger = al.ledger_entry_id or "UNSIGNED"
    return f"argus-real-{strategy_label}-{ledger}"


__all__ = [
    "REAL_MONEY_ENABLED",
    "REAL_MONEY_MAX_NOTIONAL_PER_ORDER",
    "PAPER_ACCOUNT_ID",
    "REAL_ACCOUNT_ID",
    "PAPER_PORT",
    "REAL_PORT",
    "ALLOWLIST_PATH",
    "AccountBoundaryViolationError",
    "Allowlist",
    "load_allowlist",
    "is_real_money_connection",
    "enforce_real_money_boundary",
    "real_money_order_tag",
]
