#!/usr/bin/env python3
# risk.py
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple


Q8 = Decimal("0.00000001")


def day_key_from_epoch(epoch: int) -> str:
    """
    Convert an epoch seconds timestamp into a UTC day key: YYYY-MM-DD
    """
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d")


def _as_decimal(v: Any, default: str = "0") -> Decimal:
    """
    Safe Decimal conversion for cfg/env values that may be strings, ints, floats, Decimal, or None.
    """
    if v is None:
        return Decimal(default)
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(default)


def _q8(v: Any) -> Decimal:
    return _as_decimal(v, "0").quantize(Q8, rounding=ROUND_DOWN)


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        try:
            return int(float(str(v)))
        except Exception:
            return int(default)


def _as_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default


def _as_str(v: Any, default: str = "") -> str:
    if v is None:
        return default
    try:
        return str(v).strip()
    except Exception:
        return default


def _normalize_epoch_seconds(e: int) -> int:
    """
    Normalize epoch units (ms -> s) so day boundaries and lockouts are correct.
    """
    if not e:
        return 0
    if e >= 100_000_000_000:  # ms
        return int(e // 1000)
    return int(e)


def _fmt_dec(v: Decimal, places: str = "0.000000") -> str:
    try:
        return format(v.quantize(Decimal(places), rounding=ROUND_DOWN), "f")
    except Exception:
        return str(v)


def check_spread_guard(
    spread_bps: Optional[Decimal],
    cfg: Dict,
) -> Tuple[bool, str]:
    """
    Pure function: block entry if spread exceeds configured threshold.

    Expected cfg keys:
      MAX_ENTRY_SPREAD_BPS (default 0 => disabled)
    """
    # --------- LINE ABOVE: Expected cfg keys:
    max_spread_bps = _as_decimal(cfg.get("MAX_ENTRY_SPREAD_BPS", "0"), "0")
    if max_spread_bps <= 0:
        return True, ""

    if spread_bps is None:
        return False, "RISK_SPREAD_UNKNOWN"

    spread_d = _as_decimal(spread_bps, "0")
    if spread_d > max_spread_bps:
        return False, f"RISK_SPREAD_TOO_WIDE>{_fmt_dec(max_spread_bps, '0.01')}bps"

    return True, ""


def check_daily_loss_guard(
    daily_realized_pnl_usd: Decimal,
    cfg: Dict,
) -> Tuple[bool, str]:
    """
    Pure function: block new entries once realized daily loss breaches threshold.

    Expected cfg keys:
      DAILY_MAX_LOSS_USD (default 0 => disabled)
    """
    # --------- LINE ABOVE: Expected cfg keys:
    daily_max_loss = _as_decimal(cfg.get("DAILY_MAX_LOSS_USD", "0"), "0")
    pnl_d = _as_decimal(daily_realized_pnl_usd, "0")

    if daily_max_loss <= 0:
        return True, ""

    if pnl_d <= (Decimal("0") - daily_max_loss):
        return False, "RISK_DAILY_MAX_LOSS"

    return True, ""


def check_position_limit_guard(
    *,
    symbol: str,
    proposed_qty: Decimal,
    current_qty: Decimal,
    px: Decimal,
    cfg: Dict,
) -> Tuple[bool, str]:
    """
    Pure function: checks symbol-level notional / qty limits for a proposed entry.

    Expected cfg keys:
      MAX_POSITION_QTY_PER_SYMBOL (default 0 => disabled)
      MAX_POSITION_USD_PER_SYMBOL (default 0 => disabled)
    """
    # --------- LINE ABOVE: Expected cfg keys:
    symbol_name = str(symbol or "").strip() or "UNKNOWN"

    proposed_qty_d = _q8(proposed_qty)
    current_qty_d = _q8(current_qty)
    px_d = _q8(px)

    if proposed_qty_d <= 0:
        return False, "RISK_BAD_PROPOSED_QTY"
    if px_d <= 0:
        return False, "RISK_BAD_PX"

    target_qty = _q8(current_qty_d + proposed_qty_d)

    max_qty = _as_decimal(cfg.get("MAX_POSITION_QTY_PER_SYMBOL", "0"), "0")
    if max_qty > 0 and target_qty > max_qty:
        return False, f"RISK_MAX_POSITION_QTY_{symbol_name}"

    max_usd = _as_decimal(cfg.get("MAX_POSITION_USD_PER_SYMBOL", "0"), "0")
    target_notional = _q8(target_qty * px_d)
    if max_usd > 0 and target_notional > max_usd:
        return False, f"RISK_MAX_POSITION_USD_{symbol_name}"

    return True, ""


def check_portfolio_exposure_guard(
    *,
    current_exposure_usd: Decimal,
    proposed_notional_usd: Decimal,
    cfg: Dict,
) -> Tuple[bool, str]:
    """
    Pure function: checks aggregate portfolio exposure cap.

    Expected cfg keys:
      MAX_PORTFOLIO_EXPOSURE_USD (default 0 => disabled)
    """
    # --------- LINE ABOVE: Expected cfg keys:
    max_exposure = _as_decimal(cfg.get("MAX_PORTFOLIO_EXPOSURE_USD", "0"), "0")
    if max_exposure <= 0:
        return True, ""

    current_d = _q8(current_exposure_usd)
    proposed_d = _q8(proposed_notional_usd)
    if proposed_d <= 0:
        return False, "RISK_BAD_PROPOSED_NOTIONAL"

    if _q8(current_d + proposed_d) > max_exposure:
        return False, "RISK_MAX_PORTFOLIO_EXPOSURE"

    return True, ""


def check_market_data_guard(
    *,
    stale: bool,
    now_epoch: int,
    last_market_data_epoch: Optional[int],
    cfg: Dict,
) -> Tuple[bool, str]:
    """
    Pure function: blocks entries when market data is stale or heartbeat is missing.

    Expected cfg keys:
      MAX_MARKET_DATA_AGE_SECONDS (default 0 => disabled age check)
    """
    # --------- LINE ABOVE: Expected cfg keys:
    if bool(stale):
        return False, "RISK_STALE_MARKET_DATA"

    max_age = _as_int(cfg.get("MAX_MARKET_DATA_AGE_SECONDS", 0), 0)
    if max_age <= 0:
        return True, ""

    now_s = _normalize_epoch_seconds(int(now_epoch))
    last_s = _normalize_epoch_seconds(int(last_market_data_epoch or 0))
    if last_s <= 0:
        return False, "RISK_MARKET_DATA_HEARTBEAT_MISSING"

    age = now_s - last_s
    if age > max_age:
        return False, "RISK_MARKET_DATA_TOO_OLD"

    return True, ""


def check_adapter_heartbeat_guard(
    *,
    heartbeat_ok: Optional[bool],
    cfg: Dict,
) -> Tuple[bool, str]:
    """
    Pure function: block new entries when adapter heartbeat is required and unhealthy.

    Expected cfg keys:
      EXECUTION_HEARTBEAT_REQUIRED (default false)
    """
    # --------- LINE ABOVE: Expected cfg keys:
    required = _as_bool(cfg.get("EXECUTION_HEARTBEAT_REQUIRED", False), False)
    if not required:
        return True, ""

    if heartbeat_ok is True:
        return True, ""

    return False, "RISK_ADAPTER_HEARTBEAT_FAILED"


def check_duplicate_fill_guard(
    *,
    duplicate_fill_seen: bool,
) -> Tuple[bool, str]:
    """
    Pure function: block new entries if runtime detected duplicate fill replay.
    """
    # --------- LINE ABOVE: Pure function: block new entries if runtime detected duplicate fill replay.
    if duplicate_fill_seen:
        return False, "RISK_DUPLICATE_FILL_REPLAY"
    return True, ""


def check_unacked_order_guard(
    *,
    order_ack_pending: bool,
    order_ack_started_epoch: Optional[int],
    now_epoch: int,
    cfg: Dict,
) -> Tuple[bool, str]:
    """
    Pure function: block new entries when an order is stuck waiting for ACK too long.

    Expected cfg keys:
      ORDER_ACK_TIMEOUT_SECONDS (default 30)
    """
    # --------- LINE ABOVE: Expected cfg keys:
    if not order_ack_pending:
        return True, ""

    started_s = _normalize_epoch_seconds(int(order_ack_started_epoch or 0))
    now_s = _normalize_epoch_seconds(int(now_epoch))
    if started_s <= 0:
        return False, "RISK_ORDER_ACK_PENDING_UNKNOWN_START"

    timeout_s = float(cfg.get("ORDER_ACK_TIMEOUT_SECONDS", 30) or 30)
    if timeout_s <= 0:
        timeout_s = 30.0

    age = now_s - started_s
    if age >= int(timeout_s):
        return False, "RISK_ORDER_ACK_TIMEOUT"

    return False, "RISK_ORDER_ACK_PENDING"


def check_partial_fill_guard(
    *,
    partial_fill_pending: bool,
    partial_fill_started_epoch: Optional[int],
    now_epoch: int,
    cfg: Dict,
) -> Tuple[bool, str]:
    """
    Pure function: block new entries when an order is stuck partially filled too long.

    Expected cfg keys:
      PARTIAL_FILL_TIMEOUT_SECONDS (default 120)
    """
    # --------- LINE ABOVE: Expected cfg keys:
    if not partial_fill_pending:
        return True, ""

    started_s = _normalize_epoch_seconds(int(partial_fill_started_epoch or 0))
    now_s = _normalize_epoch_seconds(int(now_epoch))
    if started_s <= 0:
        return False, "RISK_PARTIAL_FILL_PENDING_UNKNOWN_START"

    timeout_s = float(cfg.get("PARTIAL_FILL_TIMEOUT_SECONDS", 120) or 120)
    if timeout_s <= 0:
        timeout_s = 120.0

    age = now_s - started_s
    if age >= int(timeout_s):
        return False, "RISK_PARTIAL_FILL_TIMEOUT"

    return False, "RISK_PARTIAL_FILL_PENDING"


def check_adapter_timeout_guard(
    *,
    adapter_call_timed_out: bool,
) -> Tuple[bool, str]:
    """
    Pure function: block entries when runtime detects adapter timeout.
    """
    # --------- LINE ABOVE: Pure function: block entries when runtime detects adapter timeout.
    if adapter_call_timed_out:
        return False, "RISK_ADAPTER_TIMEOUT"
    return True, ""


@dataclass
class RuntimeRiskState:
    heartbeat_ok: bool = True
    last_market_data_epoch: int = 0
    stale_market_data: bool = False

    duplicate_fill_seen: bool = False
    last_duplicate_fill_id: str = ""

    order_ack_pending: bool = False
    order_ack_started_epoch: int = 0
    order_ack_client_order_id: str = ""

    partial_fill_pending: bool = False
    partial_fill_started_epoch: int = 0
    partial_fill_order_id: str = ""

    adapter_call_timed_out: bool = False
    adapter_timeout_reason: str = ""

    failure_lock_reason: str = ""
    failure_lock_set_epoch: int = 0

    def snapshot(self, now_epoch: Optional[int] = None, cfg: Optional[Dict] = None) -> Dict[str, Any]:
        now_s = _normalize_epoch_seconds(int(now_epoch or 0)) if now_epoch is not None else 0

        ack_age = 0
        if self.order_ack_pending and self.order_ack_started_epoch > 0 and now_s > 0:
            ack_age = max(0, now_s - _normalize_epoch_seconds(self.order_ack_started_epoch))

        partial_age = 0
        if self.partial_fill_pending and self.partial_fill_started_epoch > 0 and now_s > 0:
            partial_age = max(0, now_s - _normalize_epoch_seconds(self.partial_fill_started_epoch))

        market_age = 0
        if self.last_market_data_epoch > 0 and now_s > 0:
            market_age = max(0, now_s - _normalize_epoch_seconds(self.last_market_data_epoch))

        out = {
            "heartbeat_ok": bool(self.heartbeat_ok),
            "last_market_data_epoch": int(self.last_market_data_epoch or 0),
            "market_data_age_s": int(market_age),
            "stale_market_data": bool(self.stale_market_data),
            "duplicate_fill_seen": bool(self.duplicate_fill_seen),
            "last_duplicate_fill_id": self.last_duplicate_fill_id,
            "order_ack_pending": bool(self.order_ack_pending),
            "order_ack_started_epoch": int(self.order_ack_started_epoch or 0),
            "order_ack_age_s": int(ack_age),
            "order_ack_client_order_id": self.order_ack_client_order_id,
            "partial_fill_pending": bool(self.partial_fill_pending),
            "partial_fill_started_epoch": int(self.partial_fill_started_epoch or 0),
            "partial_fill_age_s": int(partial_age),
            "partial_fill_order_id": self.partial_fill_order_id,
            "adapter_call_timed_out": bool(self.adapter_call_timed_out),
            "adapter_timeout_reason": self.adapter_timeout_reason,
            "failure_lock_reason": self.failure_lock_reason,
            "failure_lock_set_epoch": int(self.failure_lock_set_epoch or 0),
        }

        if cfg:
            out["order_ack_timeout_seconds"] = float(cfg.get("ORDER_ACK_TIMEOUT_SECONDS", 30) or 30)
            out["partial_fill_timeout_seconds"] = float(cfg.get("PARTIAL_FILL_TIMEOUT_SECONDS", 120) or 120)
            out["max_market_data_age_seconds"] = int(cfg.get("MAX_MARKET_DATA_AGE_SECONDS", 0) or 0)
            out["execution_heartbeat_required"] = bool(cfg.get("EXECUTION_HEARTBEAT_REQUIRED", False))

        return out


@dataclass
class RiskManager:
    day_key: str
    trades_today: int
    daily_realized_pnl_usd: Decimal
    lockout_until_epoch: int  # 0 means not locked
    runtime: RuntimeRiskState = field(default_factory=RuntimeRiskState)

    # --------- LINE ABOVE: runtime: RuntimeRiskState = field(default_factory=RuntimeRiskState)
    @classmethod
    def new(cls, epoch: Optional[int] = None) -> "RiskManager":
        """
        If epoch is provided, initialize day_key from that tick (UTC).
        Otherwise initializes using "now" in UTC.

        Best practice:
          - Backtests should pass the first candle/tick epoch here.
          - Live can omit epoch (uses now).
        """
        if epoch is None:
            epoch = int(datetime.now(timezone.utc).timestamp())

        epoch = _normalize_epoch_seconds(int(epoch))

        return cls(
            day_key=day_key_from_epoch(epoch),
            trades_today=0,
            daily_realized_pnl_usd=Decimal("0"),
            lockout_until_epoch=0,
        )

    def ensure_day(self, now_epoch: int, cfg: Dict) -> Optional[Dict]:
        """
        Ensures day-boundary correctness (UTC day).
        If day changes, resets daily counters and returns a payload
        that engine/main can log as RISK_DAY_RESET.

        Notes:
        - We DO NOT clear lockout by default across midnight.
          Set CLEAR_LOCKOUT_ON_DAY_RESET=true to clear.
        - Backtests can legitimately start "before today".
          If RiskManager was seeded with a later day (e.g., live 'now'),
          we can auto-sync ONCE when no trades have occurred yet.

        Config knobs:
          AUTO_SYNC_DAY_ON_REWIND (default true):
            If we detect new_day < current day_key AND we have not traded yet,
            we will snap day_key back to new_day once to prevent infinite
            "IGNORED day rewind" spam in backtests.
        """
        now_epoch = _normalize_epoch_seconds(int(now_epoch))
        new_day = day_key_from_epoch(now_epoch)

        if new_day == self.day_key:
            return None

        if new_day < self.day_key:
            # --------- LINE ABOVE: if new_day < self.day_key:
            auto_sync = _as_bool(cfg.get("AUTO_SYNC_DAY_ON_REWIND", True), True)
            safe_to_sync = (
                self.trades_today == 0
                and self.daily_realized_pnl_usd == Decimal("0")
                and int(self.lockout_until_epoch or 0) == 0
            )

            if auto_sync and safe_to_sync:
                old_day = self.day_key
                self.day_key = new_day
                msg = f"AUTO_SYNC day {old_day} -> {new_day} (epoch={now_epoch})"
                return {
                    "old_day": old_day,
                    "new_day": new_day,
                    "msg": msg,
                    "auto_synced": True,
                }

            return {
                "old_day": self.day_key,
                "new_day": new_day,
                "msg": f"IGNORED day rewind {self.day_key} -> {new_day} (epoch={now_epoch})",
                "ignored_rewind": True,
            }

        old_day = self.day_key
        old_trades = self.trades_today
        old_realized = self.daily_realized_pnl_usd
        old_lockout = self.lockout_until_epoch

        self.day_key = new_day
        self.trades_today = 0
        self.daily_realized_pnl_usd = Decimal("0")

        clear_lockout = _as_bool(cfg.get("CLEAR_LOCKOUT_ON_DAY_RESET", False), False)
        if clear_lockout:
            self.lockout_until_epoch = 0

        lockout_note = "cleared" if clear_lockout else "kept"

        msg = (
            f"Reset day {old_day} -> {new_day} | "
            f"trades_today {old_trades}->0 | "
            f"daily_realized {old_realized:.8f}->0 | "
            f"lockout_until {old_lockout}->{self.lockout_until_epoch} ({lockout_note})"
        )

        return {
            "old_day": old_day,
            "new_day": new_day,
            "old_trades_today": old_trades,
            "old_daily_realized": str(_q8(old_realized)),
            "old_lockout_until_epoch": old_lockout,
            "new_lockout_until_epoch": self.lockout_until_epoch,
            "msg": msg,
        }

    def can_enter(self, now_epoch: int, cfg: Dict) -> Tuple[bool, str]:
        """
        Gate NEW entries only. Exits should still be allowed.
        """
        # --------- LINE ABOVE: Gate NEW entries only. Exits should still be allowed.
        now_epoch = _normalize_epoch_seconds(int(now_epoch))

        self.ensure_day(now_epoch, cfg)

        if self.runtime.failure_lock_reason:
            return False, self.runtime.failure_lock_reason

        if self.lockout_until_epoch and now_epoch < int(self.lockout_until_epoch):
            return False, "RISK_LOCKOUT_COOLDOWN"

        max_trades = _as_int(cfg.get("MAX_TRADES_PER_DAY", 0), 0)
        if max_trades > 0 and self.trades_today >= max_trades:
            return False, "RISK_MAX_TRADES_PER_DAY"

        ok, reason = check_daily_loss_guard(self.daily_realized_pnl_usd, cfg)
        if not ok:
            return False, reason

        return True, ""

    def can_enter_with_context(
        self,
        *,
        now_epoch: int,
        cfg: Dict,
        spread_bps: Optional[Decimal] = None,
        proposed_qty: Optional[Decimal] = None,
        current_qty: Optional[Decimal] = None,
        px: Optional[Decimal] = None,
        current_exposure_usd: Optional[Decimal] = None,
        stale: bool = False,
        last_market_data_epoch: Optional[int] = None,
        symbol: str = "",
        heartbeat_ok: Optional[bool] = None,
        duplicate_fill_seen: bool = False,
        order_ack_pending: bool = False,
        order_ack_started_epoch: Optional[int] = None,
        partial_fill_pending: bool = False,
        partial_fill_started_epoch: Optional[int] = None,
        adapter_call_timed_out: bool = False,
    ) -> Tuple[bool, str]:
        """
        Context-aware entry gate for Phase 8.
        Keeps can_enter() backward-compatible while allowing richer runtime checks.
        """
        # --------- LINE ABOVE: Keeps can_enter() backward-compatible while allowing richer runtime checks.
        ok, reason = self.can_enter(now_epoch, cfg)
        if not ok:
            return False, reason

        ok, reason = check_adapter_timeout_guard(
            adapter_call_timed_out=adapter_call_timed_out,
        )
        if not ok:
            return False, reason

        ok, reason = check_adapter_heartbeat_guard(
            heartbeat_ok=heartbeat_ok,
            cfg=cfg,
        )
        if not ok:
            return False, reason

        ok, reason = check_duplicate_fill_guard(
            duplicate_fill_seen=duplicate_fill_seen,
        )
        if not ok:
            return False, reason

        ok, reason = check_unacked_order_guard(
            order_ack_pending=order_ack_pending,
            order_ack_started_epoch=order_ack_started_epoch,
            now_epoch=now_epoch,
            cfg=cfg,
        )
        if not ok:
            return False, reason

        ok, reason = check_partial_fill_guard(
            partial_fill_pending=partial_fill_pending,
            partial_fill_started_epoch=partial_fill_started_epoch,
            now_epoch=now_epoch,
            cfg=cfg,
        )
        if not ok:
            return False, reason

        ok, reason = check_market_data_guard(
            stale=stale,
            now_epoch=now_epoch,
            last_market_data_epoch=last_market_data_epoch,
            cfg=cfg,
        )
        if not ok:
            return False, reason

        ok, reason = check_spread_guard(spread_bps, cfg)
        if not ok:
            return False, reason

        if proposed_qty is not None and current_qty is not None and px is not None:
            ok, reason = check_position_limit_guard(
                symbol=symbol,
                proposed_qty=proposed_qty,
                current_qty=current_qty,
                px=px,
                cfg=cfg,
            )
            if not ok:
                return False, reason

        if proposed_qty is not None and px is not None and current_exposure_usd is not None:
            ok, reason = check_portfolio_exposure_guard(
                current_exposure_usd=current_exposure_usd,
                proposed_notional_usd=_q8(_as_decimal(proposed_qty, "0") * _as_decimal(px, "0")),
                cfg=cfg,
            )
            if not ok:
                return False, reason

        return True, ""

    def update_runtime_health(
        self,
        *,
        now_epoch: int,
        heartbeat_ok: Optional[bool] = None,
        stale_market_data: Optional[bool] = None,
        last_market_data_epoch: Optional[int] = None,
        duplicate_fill_seen: Optional[bool] = None,
        duplicate_fill_id: str = "",
        order_ack_pending: Optional[bool] = None,
        order_ack_started_epoch: Optional[int] = None,
        order_ack_client_order_id: str = "",
        partial_fill_pending: Optional[bool] = None,
        partial_fill_started_epoch: Optional[int] = None,
        partial_fill_order_id: str = "",
        adapter_call_timed_out: Optional[bool] = None,
        adapter_timeout_reason: str = "",
    ) -> None:
        """
        Runtime orchestration should call this as facts change.
        This function does not decide policy; it only stores health facts.
        """
        # --------- LINE ABOVE: This function does not decide policy; it only stores health facts.
        _ = _normalize_epoch_seconds(int(now_epoch))

        if heartbeat_ok is not None:
            self.runtime.heartbeat_ok = bool(heartbeat_ok)

        if stale_market_data is not None:
            self.runtime.stale_market_data = bool(stale_market_data)

        if last_market_data_epoch is not None:
            self.runtime.last_market_data_epoch = _normalize_epoch_seconds(int(last_market_data_epoch))

        if duplicate_fill_seen is not None:
            self.runtime.duplicate_fill_seen = bool(duplicate_fill_seen)
            if duplicate_fill_id:
                self.runtime.last_duplicate_fill_id = duplicate_fill_id

        if order_ack_pending is not None:
            self.runtime.order_ack_pending = bool(order_ack_pending)
            if not self.runtime.order_ack_pending:
                self.runtime.order_ack_started_epoch = 0
                self.runtime.order_ack_client_order_id = ""
            else:
                if order_ack_started_epoch is not None:
                    self.runtime.order_ack_started_epoch = _normalize_epoch_seconds(int(order_ack_started_epoch))
                if order_ack_client_order_id:
                    self.runtime.order_ack_client_order_id = order_ack_client_order_id

        if partial_fill_pending is not None:
            self.runtime.partial_fill_pending = bool(partial_fill_pending)
            if not self.runtime.partial_fill_pending:
                self.runtime.partial_fill_started_epoch = 0
                self.runtime.partial_fill_order_id = ""
            else:
                if partial_fill_started_epoch is not None:
                    self.runtime.partial_fill_started_epoch = _normalize_epoch_seconds(int(partial_fill_started_epoch))
                if partial_fill_order_id:
                    self.runtime.partial_fill_order_id = partial_fill_order_id

        if adapter_call_timed_out is not None:
            self.runtime.adapter_call_timed_out = bool(adapter_call_timed_out)
            self.runtime.adapter_timeout_reason = _as_str(adapter_timeout_reason, "")

    def runtime_entry_gate(self, now_epoch: int, cfg: Dict) -> Tuple[bool, str]:
        """
        Convenience gate using internal runtime state.
        """
        # --------- LINE ABOVE: Convenience gate using internal runtime state.
        return self.can_enter_with_context(
            now_epoch=now_epoch,
            cfg=cfg,
            stale=self.runtime.stale_market_data,
            last_market_data_epoch=self.runtime.last_market_data_epoch,
            heartbeat_ok=self.runtime.heartbeat_ok,
            duplicate_fill_seen=self.runtime.duplicate_fill_seen,
            order_ack_pending=self.runtime.order_ack_pending,
            order_ack_started_epoch=self.runtime.order_ack_started_epoch,
            partial_fill_pending=self.runtime.partial_fill_pending,
            partial_fill_started_epoch=self.runtime.partial_fill_started_epoch,
            adapter_call_timed_out=self.runtime.adapter_call_timed_out,
        )

    def apply_failure_policy(self, now_epoch: int, cfg: Dict) -> Tuple[bool, str]:
        """
        Escalates runtime failures into lockout policy.
        Returns (changed, reason).

        Policy:
        - duplicate fill replay => hard lockout
        - adapter timeout => hard lockout
        - heartbeat failure => hard lockout if required
        - ACK timeout => hard lockout
        - partial fill timeout => hard lockout
        - stale/too-old market data => veto only, not automatic lockout
        """
        # --------- LINE ABOVE: stale/too-old market data => veto only, not automatic lockout
        now_s = _normalize_epoch_seconds(int(now_epoch))

        ok, reason = check_duplicate_fill_guard(
            duplicate_fill_seen=self.runtime.duplicate_fill_seen,
        )
        if not ok:
            self.force_failure_lockout(now_s, reason)
            return True, reason

        ok, reason = check_adapter_timeout_guard(
            adapter_call_timed_out=self.runtime.adapter_call_timed_out,
        )
        if not ok:
            self.force_failure_lockout(now_s, reason)
            return True, reason

        ok, reason = check_adapter_heartbeat_guard(
            heartbeat_ok=self.runtime.heartbeat_ok,
            cfg=cfg,
        )
        if not ok:
            self.force_failure_lockout(now_s, reason)
            return True, reason

        ok, reason = check_unacked_order_guard(
            order_ack_pending=self.runtime.order_ack_pending,
            order_ack_started_epoch=self.runtime.order_ack_started_epoch,
            now_epoch=now_s,
            cfg=cfg,
        )
        if not ok and reason == "RISK_ORDER_ACK_TIMEOUT":
            self.force_failure_lockout(now_s, reason)
            return True, reason

        ok, reason = check_partial_fill_guard(
            partial_fill_pending=self.runtime.partial_fill_pending,
            partial_fill_started_epoch=self.runtime.partial_fill_started_epoch,
            now_epoch=now_s,
            cfg=cfg,
        )
        if not ok and reason == "RISK_PARTIAL_FILL_TIMEOUT":
            self.force_failure_lockout(now_s, reason)
            return True, reason

        return False, ""

    def force_failure_lockout(self, now_epoch: int, reason: str) -> None:
        """
        Failure lockout is intentionally stronger than cooldown lockout.
        It uses a very large horizon until operator/runtime explicitly clears it.
        """
        # --------- LINE ABOVE: It uses a very large horizon until operator/runtime explicitly clears it.
        now_s = _normalize_epoch_seconds(int(now_epoch))
        self.runtime.failure_lock_reason = _as_str(reason, "RISK_FAILURE_LOCKOUT")
        self.runtime.failure_lock_set_epoch = now_s

        # Roughly year 2100-safe horizon; effectively "manual clear required"
        self.lockout_until_epoch = max(int(self.lockout_until_epoch or 0), 4_102_444_800)

    def clear_failure_flags(
        self,
        *,
        clear_duplicate_fill: bool = True,
        clear_adapter_timeout: bool = True,
        clear_ack_pending: bool = False,
        clear_partial_fill_pending: bool = False,
        clear_heartbeat_failure: bool = False,
    ) -> None:
        """
        Explicitly clears runtime failure facts.
        Use carefully: runtime should only clear facts once state is actually repaired.
        """
        # --------- LINE ABOVE: Use carefully: runtime should only clear facts once state is actually repaired.
        if clear_duplicate_fill:
            self.runtime.duplicate_fill_seen = False
            self.runtime.last_duplicate_fill_id = ""

        if clear_adapter_timeout:
            self.runtime.adapter_call_timed_out = False
            self.runtime.adapter_timeout_reason = ""

        if clear_ack_pending:
            self.runtime.order_ack_pending = False
            self.runtime.order_ack_started_epoch = 0
            self.runtime.order_ack_client_order_id = ""

        if clear_partial_fill_pending:
            self.runtime.partial_fill_pending = False
            self.runtime.partial_fill_started_epoch = 0
            self.runtime.partial_fill_order_id = ""

        if clear_heartbeat_failure:
            self.runtime.heartbeat_ok = True

        self.runtime.failure_lock_reason = ""
        self.runtime.failure_lock_set_epoch = 0

    def record_entry(self, now_epoch: int, cfg: Dict) -> None:
        # --------- LINE ABOVE: def record_entry(self, now_epoch: int, cfg: Dict) -> None:
        now_epoch = _normalize_epoch_seconds(int(now_epoch))
        self.ensure_day(now_epoch, cfg)
        self.trades_today += 1

    def record_exit(self, realized_pnl_usd: Decimal, now_epoch: int, cfg: Dict) -> None:
        now_epoch = _normalize_epoch_seconds(int(now_epoch))
        self.ensure_day(now_epoch, cfg)

        realized_d = _q8(realized_pnl_usd)
        self.daily_realized_pnl_usd = _q8(self.daily_realized_pnl_usd + realized_d)

        cooldown_after_loss = _as_int(cfg.get("COOLDOWN_AFTER_LOSS_SECONDS", 0), 0)
        if realized_d < 0 and cooldown_after_loss > 0:
            self.lockout_until_epoch = max(
                int(self.lockout_until_epoch or 0),
                now_epoch + cooldown_after_loss,
            )

    def force_lockout(self, until_epoch: int) -> None:
        """
        Hard set or extend lockout. Useful for external runtime failures.
        """
        # --------- LINE ABOVE: Hard set or extend lockout. Useful for external runtime failures.
        until_s = _normalize_epoch_seconds(int(until_epoch))
        self.lockout_until_epoch = max(int(self.lockout_until_epoch or 0), until_s)

    def clear_lockout(self) -> None:
        """
        Explicit operator/runtime unlock.
        """
        # --------- LINE ABOVE: Explicit operator/runtime unlock.
        self.lockout_until_epoch = 0

    def cooldown_remaining_seconds(self, now_epoch: int) -> int:
        """
        Remaining cooldown seconds, never negative.
        """
        # --------- LINE ABOVE: Remaining cooldown seconds, never negative.
        now_s = _normalize_epoch_seconds(int(now_epoch))
        if not self.lockout_until_epoch:
            return 0
        return max(0, int(self.lockout_until_epoch) - now_s)

    def snapshot(self, now_epoch: Optional[int] = None) -> Dict[str, Any]:
        """
        Lightweight serialization helper for logging/debug/rebuild.
        """
        # --------- LINE ABOVE: Lightweight serialization helper for logging/debug/rebuild.
        cooldown_remaining = 0
        if now_epoch is not None:
            cooldown_remaining = self.cooldown_remaining_seconds(now_epoch)

        return {
            "day_key": self.day_key,
            "trades_today": int(self.trades_today),
            "daily_realized_pnl_usd": str(_q8(self.daily_realized_pnl_usd)),
            "lockout_until_epoch": int(self.lockout_until_epoch or 0),
            "cooldown_remaining_s": int(cooldown_remaining),
            "runtime": self.runtime.snapshot(now_epoch=now_epoch),
        }

    # =========================
    # Phase 4 helper (optional)
    # =========================
    def size_by_vol(self, px: Decimal, vol: Optional[Decimal], cfg: Dict) -> Tuple[Decimal, str]:
        """
        Optional position sizing helper:
          qty = RISK_PER_TRADE_USD / (px * (vol * VOL_STOP_MULT))

        Expected cfg keys (optional):
          RISK_PER_TRADE_USD (default 5)
          VOL_STOP_MULT (default 2.5)
          VOL_MAX_QTY (default 0 => disabled)

        Returns (qty, reason)
        """
        # --------- LINE ABOVE: Returns (qty, reason)
        if vol is None:
            return Decimal("0").quantize(Q8, rounding=ROUND_DOWN), "NO_VOL"

        vol_d = _as_decimal(vol, "0")
        if vol_d <= 0:
            return Decimal("0").quantize(Q8, rounding=ROUND_DOWN), "NO_VOL"

        px_d = _as_decimal(px, "0")
        if px_d <= 0:
            return Decimal("0").quantize(Q8, rounding=ROUND_DOWN), "BAD_PX"

        risk_usd = _as_decimal(cfg.get("RISK_PER_TRADE_USD", "5"), "5")
        mult = _as_decimal(cfg.get("VOL_STOP_MULT", "2.5"), "2.5")

        stop_dist = vol_d * mult
        if stop_dist <= 0:
            return Decimal("0").quantize(Q8, rounding=ROUND_DOWN), "BAD_VOL"

        risk_per_unit = px_d * stop_dist
        if risk_per_unit <= 0:
            return Decimal("0").quantize(Q8, rounding=ROUND_DOWN), "BAD_VOL"

        qty = _q8(risk_usd / risk_per_unit)
        if qty <= 0:
            return Decimal("0").quantize(Q8, rounding=ROUND_DOWN), "QTY_ZERO"

        max_qty = _as_decimal(cfg.get("VOL_MAX_QTY", "0"), "0")
        if max_qty > 0 and qty > max_qty:
            return _q8(max_qty), f"VOL_OK|CLAMP_MAX_QTY={_q8(max_qty)}"

        return qty, "VOL_OK"