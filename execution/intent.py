# line above: from __future__ import annotations
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Dict, Any

from decisions import DecisionSnapshot


@dataclass(frozen=True)
class OrderIntent:
    # identity
    run_id: str
    symbol: str
    intent_id: str           # stable, restart-safe
    intent_type: str         # ENTRY_LONG, EXIT, etc.

    # order plan
    side: str                # BUY/SELL
    qty: Decimal
    order_type: str          # MARKET/LIMIT
    limit_px: Optional[Decimal] = None

    # traceability
    intent_epoch: int = 0
    intent_ts: str = ""
    entry_reason: str = ""
    risk_blocked_reason: str = ""

    # risk levels (optional but valuable)
    take_profit: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    trail_stop: Optional[Decimal] = None

    def client_order_id(self, leg: str) -> str:
        return f"{self.run_id}:{self.symbol}:{self.intent_id}:{leg}"

    def to_row(self) -> Dict[str, Any]:
        def _d(x):
            if x is None:
                return None
            if isinstance(x, Decimal):
                return str(x)
            return x

        return {
            "run_id": self.run_id,
            "symbol": self.symbol,
            "intent_id": self.intent_id,
            "intent_type": self.intent_type,
            "intent_epoch": int(self.intent_epoch),
            "intent_ts": self.intent_ts,
            "side": self.side,
            "qty": _d(self.qty),
            "order_type": self.order_type,
            "limit_px": _d(self.limit_px),
            "client_order_id_entry": self.client_order_id("ENTRY") if self.intent_type.startswith("ENTRY") else "",
            "client_order_id_exit": self.client_order_id("EXIT") if self.intent_type == "EXIT" else "",
            "entry_reason": self.entry_reason,
            "risk_blocked_reason": self.risk_blocked_reason,
            "take_profit": _d(self.take_profit),
            "stop_loss": _d(self.stop_loss),
            "trail_stop": _d(self.trail_stop),
        }


def _bucket_epoch(snap: DecisionSnapshot) -> int:
    # Prefer candle_start_1m if present (best alignment with your data model)
    if snap.candle_start_1m is not None:
        return int(snap.candle_start_1m)
    return int(snap.epoch // 60 * 60)


def derive_intent_type(snap: DecisionSnapshot) -> Optional[str]:
    a = (snap.action or "").upper().strip()
    if a in ("HOLD", ""):
        return None
    if a in ("BUY", "ENTER", "ENTRY"):
        return "ENTRY_LONG"
    if a in ("EXIT", "CLOSE", "SELL_EXIT"):
        return "EXIT"
    # If you later support shorts:
    if a in ("SELL", "SHORT"):
        return "ENTRY_SHORT"
    return None


def build_order_intent(
    run_id: str,
    snap: DecisionSnapshot,
    seq: int,
) -> Optional[OrderIntent]:
    it = derive_intent_type(snap)
    if it is None:
        return None

    # hard runtime blocks (intent should not be created if blocked)
    if snap.paused or snap.stale:
        return None
    if snap.lockout_until_epoch and snap.epoch < snap.lockout_until_epoch:
        return None
    if snap.cooldown_remaining_s and snap.cooldown_remaining_s > 0:
        return None
    if (snap.risk_blocked_reason or "").strip():
        return None

    bucket = _bucket_epoch(snap)
    intent_id = f"{bucket}:{seq:06d}"

    # sizing
    if it.startswith("ENTRY"):
        qty = snap.vol_sizing_qty or Decimal("0")
        side = "BUY" if it == "ENTRY_LONG" else "SELL"
    else:
        qty = abs(snap.position_qty or Decimal("0"))
        side = "SELL"  # assume long-only; if short later, this becomes BUY-to-cover

    if qty <= Decimal("0"):
        return None

    return OrderIntent(
        run_id=run_id,
        symbol=snap.symbol,
        intent_id=intent_id,
        intent_type=it,
        side=side,
        qty=qty,
        order_type="MARKET",
        limit_px=None,
        intent_epoch=int(snap.epoch),
        intent_ts=snap.ts,
        entry_reason=(snap.action_reason or "").strip(),
        risk_blocked_reason=(snap.risk_blocked_reason or "").strip(),
        take_profit=snap.take_profit,
        stop_loss=snap.stop_loss,
        trail_stop=snap.trail_stop,
    )