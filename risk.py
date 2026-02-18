# risk.py
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple


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


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
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


def _normalize_epoch_seconds(e: int) -> int:
    """
    Normalize epoch units (ms -> s) so day boundaries and lockouts are correct.
    """
    if not e:
        return 0
    if e >= 100_000_000_000:  # ms
        return int(e // 1000)
    return int(e)


@dataclass
class RiskManager:
    day_key: str
    trades_today: int
    daily_realized_pnl_usd: Decimal
    lockout_until_epoch: int  # 0 means not locked

    # --------- LINE ABOVE: lockout_until_epoch: int  # 0 means not locked
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

        # Prevent "day rewinds" spam:
        # If seeded with a later day than the first BT candle, auto-sync once
        # as long as we have no trade state to lose.
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
                # Counters already zero; keep them zero.
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

        # Reset daily counters
        self.day_key = new_day
        self.trades_today = 0
        self.daily_realized_pnl_usd = Decimal("0")

        # Decide lockout behavior
        clear_lockout = _as_bool(cfg.get("CLEAR_LOCKOUT_ON_DAY_RESET", False), False)
        if clear_lockout:
            self.lockout_until_epoch = 0

        lockout_note = "cleared" if clear_lockout else "kept"

        msg = (
            f"Reset day {old_day} -> {new_day} | "
            f"trades_today {old_trades}->0 | "
            f"daily_realized {old_realized:.6f}->0 | "
            f"lockout_until {old_lockout}->{self.lockout_until_epoch} ({lockout_note})"
        )

        return {
            "old_day": old_day,
            "new_day": new_day,
            "old_trades_today": old_trades,
            "old_daily_realized": str(old_realized),
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

        # Day roll first (engine/main can log payload from ensure_day())
        self.ensure_day(now_epoch, cfg)

        # Lockout gate
        if self.lockout_until_epoch and now_epoch < int(self.lockout_until_epoch):
            return False, "RISK_LOCKOUT_COOLDOWN"

        # Trades/day gate
        max_trades = _as_int(cfg.get("MAX_TRADES_PER_DAY", 0), 0)
        if max_trades > 0 and self.trades_today >= max_trades:
            return False, "RISK_MAX_TRADES_PER_DAY"

        # Daily max loss gate (realized only)
        daily_max_loss = _as_decimal(cfg.get("DAILY_MAX_LOSS_USD", "0"), "0")
        if daily_max_loss > 0:
            if self.daily_realized_pnl_usd <= (Decimal("0") - daily_max_loss):
                return False, "RISK_DAILY_MAX_LOSS"

        return True, ""

    def record_entry(self, now_epoch: int, cfg: Dict) -> None:
        # --------- LINE ABOVE: def record_entry(self, now_epoch: int, cfg: Dict) -> None:
        now_epoch = _normalize_epoch_seconds(int(now_epoch))
        self.ensure_day(now_epoch, cfg)
        self.trades_today += 1

    def record_exit(self, realized_pnl_usd: Decimal, now_epoch: int, cfg: Dict) -> None:
        now_epoch = _normalize_epoch_seconds(int(now_epoch))
        self.ensure_day(now_epoch, cfg)

        # Defensive: accept strings/floats/Decimal
        realized_d = _as_decimal(realized_pnl_usd, "0")
        self.daily_realized_pnl_usd += realized_d

        # Loss cooldown
        cooldown_after_loss = _as_int(cfg.get("COOLDOWN_AFTER_LOSS_SECONDS", 0), 0)
        if realized_d < 0 and cooldown_after_loss > 0:
            self.lockout_until_epoch = max(
                int(self.lockout_until_epoch or 0),
                now_epoch + cooldown_after_loss,
            )

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

        Returns (qty, reason)
        """
        # --------- LINE ABOVE: Returns (qty, reason)
        if vol is None:
            return Decimal("0"), "NO_VOL"
        vol_d = _as_decimal(vol, "0")
        if vol_d <= 0:
            return Decimal("0"), "NO_VOL"

        px_d = _as_decimal(px, "0")
        if px_d <= 0:
            return Decimal("0"), "BAD_PX"

        risk_usd = _as_decimal(cfg.get("RISK_PER_TRADE_USD", "5"), "5")
        mult = _as_decimal(cfg.get("VOL_STOP_MULT", "2.5"), "2.5")

        stop_dist = vol_d * mult  # fraction (ex: 0.004 * 2.5)
        if stop_dist <= 0:
            return Decimal("0"), "BAD_VOL"

        risk_per_unit = px_d * stop_dist
        if risk_per_unit <= 0:
            return Decimal("0"), "BAD_VOL"

        qty = risk_usd / risk_per_unit
        if qty <= 0:
            return Decimal("0"), "QTY_ZERO"

        # Clamp absurd sizes (safety rail; keeps bugs from “infinite qty”)
        max_qty = _as_decimal(cfg.get("VOL_MAX_QTY", "0"), "0")
        if max_qty > 0 and qty > max_qty:
            return Decimal(max_qty), f"VOL_OK|CLAMP_MAX_QTY={max_qty}"

        return qty, "VOL_OK"
