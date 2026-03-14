#!/usr/bin/env python3
# ledger.py
from __future__ import annotations

# line above: from typing import Dict, Optional, Tuple, Any
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Dict, Optional, Tuple, Any


# -------------------------
# Canonical quantization contract
# -------------------------
QTY_QUANT = Decimal("0.00000001")
PX_QUANT = Decimal("0.00000001")
MONEY_QUANT = Decimal("0.00000001")


# -------------------------
# Parsing helpers (robust cfg/env handling)
# -------------------------
def _as_decimal(x: Any, default: str = "0") -> Decimal:
    """
    Safe Decimal conversion for cfg/env values that may be strings, ints, floats, Decimal, or None.
    """
    if x is None:
        return Decimal(default)
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def _as_int(x: Any, default: int = 0) -> int:
    if x is None:
        return default
    try:
        return int(x)
    except (ValueError, TypeError):
        try:
            return int(float(str(x)))
        except (ValueError, TypeError):
            return default


def _q_qty(x: Any) -> Decimal:
    return _as_decimal(x, "0").quantize(QTY_QUANT, rounding=ROUND_DOWN)


def _q_px(x: Any) -> Decimal:
    return _as_decimal(x, "0").quantize(PX_QUANT, rounding=ROUND_DOWN)


def _q_money(x: Any) -> Decimal:
    return _as_decimal(x, "0").quantize(MONEY_QUANT, rounding=ROUND_DOWN)


def bps(x: Decimal) -> Decimal:
    return x / Decimal("10000")


def apply_buy_slippage(cfg: Dict, mid_px: Decimal) -> Decimal:
    slip_bps = _as_decimal(cfg.get("SLIPPAGE_BPS", "0"), "0")
    return _q_px(_as_decimal(mid_px) * (Decimal("1") + bps(slip_bps)))


def apply_sell_slippage(cfg: Dict, mid_px: Decimal) -> Decimal:
    slip_bps = _as_decimal(cfg.get("SLIPPAGE_BPS", "0"), "0")
    return _q_px(_as_decimal(mid_px) * (Decimal("1") - bps(slip_bps)))


def fee_usd(cfg: Dict, notional_usd: Decimal) -> Decimal:
    fee_bps = _as_decimal(cfg.get("FEE_BPS", "0"), "0")
    return _q_money(_as_decimal(notional_usd) * bps(fee_bps))


@dataclass
class VirtualLedger:
    cash_usd: Decimal
    position_qty: Decimal = Decimal("0")
    avg_entry_px: Optional[Decimal] = None  # mid px basis (not slippage-adjusted)
    realized_pnl_usd: Decimal = Decimal("0")
    entry_epoch: Optional[int] = None
    peak_px: Optional[Decimal] = None       # mid px peak (not slippage-adjusted)

    # --------- LINE ABOVE: peak_px: Optional[Decimal] = None
    @classmethod
    def from_config(cls, cfg: Dict) -> "VirtualLedger":
        """
        Create ledger using config:
          START_CASH_USD
        """
        start_cash = _q_money(cfg.get("START_CASH_USD", "0"))
        return cls(cash_usd=start_cash)

    def __post_init__(self) -> None:
        self.cash_usd = _q_money(self.cash_usd)
        self.position_qty = _q_qty(self.position_qty)
        self.realized_pnl_usd = _q_money(self.realized_pnl_usd)
        if self.avg_entry_px is not None:
            self.avg_entry_px = _q_px(self.avg_entry_px)
        if self.peak_px is not None:
            self.peak_px = _q_px(self.peak_px)
        self.normalize_state()

    # -------------------------
    # State helpers
    # -------------------------
    def in_pos(self) -> bool:
        return self.position_qty > 0

    def exposure_usd(self, px: Decimal) -> Decimal:
        return _q_money(self.position_qty * _q_px(px))

    def equity_usd(self, px: Decimal) -> Decimal:
        return _q_money(self.cash_usd + self.exposure_usd(px))

    def normalize_state(self) -> None:
        """
        Enforce internal consistency after restore or mutation.

        Rules:
          - qty <= 0 => flat state
          - flat state => avg_entry/entry_epoch/peak cleared
          - negative realized/cash allowed; do not clamp
          - all numeric surfaces obey one 8-decimal quantization contract
        """
        self.cash_usd = _q_money(self.cash_usd)
        self.position_qty = _q_qty(self.position_qty)
        self.realized_pnl_usd = _q_money(self.realized_pnl_usd)

        if self.position_qty <= 0:
            self.position_qty = Decimal("0").quantize(QTY_QUANT, rounding=ROUND_DOWN)
            self.avg_entry_px = None
            self.entry_epoch = None
            self.peak_px = None
            return

        if self.avg_entry_px is not None:
            self.avg_entry_px = _q_px(self.avg_entry_px)
            if self.avg_entry_px <= 0:
                self.avg_entry_px = None

        if self.peak_px is not None:
            self.peak_px = _q_px(self.peak_px)
            if self.peak_px <= 0:
                self.peak_px = None

        if self.peak_px is None and self.avg_entry_px is not None:
            self.peak_px = self.avg_entry_px

    def clear_position(self) -> None:
        """
        Clear only position-related state.
        Keeps cash and realized PnL intact.
        """
        # --------- LINE ABOVE: self.position_qty = Decimal("0")
        self.position_qty = Decimal("0").quantize(QTY_QUANT, rounding=ROUND_DOWN)
        self.avg_entry_px = None
        self.entry_epoch = None
        self.peak_px = None

    def reset(
        self,
        *,
        cash_usd: Optional[Decimal] = None,
        realized_pnl_usd: Optional[Decimal] = None,
    ) -> None:
        """
        Full ledger reset helper.
        """
        # --------- LINE ABOVE: if cash_usd is not None:
        if cash_usd is not None:
            self.cash_usd = _q_money(cash_usd)
        if realized_pnl_usd is not None:
            self.realized_pnl_usd = _q_money(realized_pnl_usd)

        self.clear_position()
        self.normalize_state()

    def restore_from_execution_truth(
        self,
        *,
        cash_usd: Any,
        position_qty: Any,
        avg_entry_px: Any,
        realized_pnl_usd: Any = None,
        entry_epoch: Any = None,
        peak_px: Any = None,
    ) -> None:
        """
        Restore ledger from reconciled execution truth.

        This method is intentionally dumb:
        - it does not decide what truth is
        - it only projects that truth into ledger state

        Inputs should come from restart reconcile, not from raw defaults.
        """
        # --------- LINE ABOVE: qty = _as_decimal(position_qty, "0")
        qty = _q_qty(position_qty)
        cash = _q_money(cash_usd)
        avg_entry = None if avg_entry_px is None or str(avg_entry_px).strip() == "" else _q_px(avg_entry_px)
        realized = self.realized_pnl_usd if realized_pnl_usd is None else _q_money(realized_pnl_usd)
        entry_e = _as_int(entry_epoch, 0)
        peak = None if peak_px is None or str(peak_px).strip() == "" else _q_px(peak_px)

        self.cash_usd = cash
        self.realized_pnl_usd = realized
        self.position_qty = qty

        if qty <= 0:
            self.avg_entry_px = None
            self.entry_epoch = None
            self.peak_px = None
        else:
            self.avg_entry_px = avg_entry
            self.entry_epoch = entry_e if entry_e > 0 else None
            self.peak_px = peak if peak is not None else avg_entry

        self.normalize_state()

    def apply_account_restore(
        self,
        *,
        cash_usd: Any,
        realized_pnl_usd: Any = None,
    ) -> None:
        """
        Restore non-position account surface only.
        Useful if account truth is separate from position truth.
        """
        # --------- LINE ABOVE: self.cash_usd = _as_decimal(cash_usd, "0")
        self.cash_usd = _q_money(cash_usd)
        if realized_pnl_usd is not None:
            self.realized_pnl_usd = _q_money(realized_pnl_usd)
        self.normalize_state()

    def restore_position_only(
        self,
        *,
        position_qty: Any,
        avg_entry_px: Any,
        entry_epoch: Any = None,
        peak_px: Any = None,
    ) -> None:
        """
        Restore only position surface.
        Keeps cash and realized PnL unchanged.
        """
        # --------- LINE ABOVE: qty = _as_decimal(position_qty, "0")
        qty = _q_qty(position_qty)
        avg_entry = None if avg_entry_px is None or str(avg_entry_px).strip() == "" else _q_px(avg_entry_px)
        entry_e = _as_int(entry_epoch, 0)
        peak = None if peak_px is None or str(peak_px).strip() == "" else _q_px(peak_px)

        self.position_qty = qty

        if qty <= 0:
            self.avg_entry_px = None
            self.entry_epoch = None
            self.peak_px = None
        else:
            self.avg_entry_px = avg_entry
            self.entry_epoch = entry_e if entry_e > 0 else None
            self.peak_px = peak if peak is not None else avg_entry

        self.normalize_state()

    def export_runtime_snapshot(self) -> Dict[str, Any]:
        """
        Minimal ledger-side snapshot view.
        """
        # --------- LINE ABOVE: return {
        return {
            "cash_usd": str(_q_money(self.cash_usd)),
            "position_qty": str(_q_qty(self.position_qty)),
            "avg_entry_px": "" if self.avg_entry_px is None else str(_q_px(self.avg_entry_px)),
            "realized_pnl_usd": str(_q_money(self.realized_pnl_usd)),
            "entry_epoch": 0 if self.entry_epoch is None else int(self.entry_epoch),
            "peak_px": "" if self.peak_px is None else str(_q_px(self.peak_px)),
            "in_pos": bool(self.in_pos()),
        }

    # -------------------------
    # PnL
    # -------------------------
    def unrealized_pnl_usd(self, px: Decimal, cfg: Dict) -> Decimal:
        """
        Unrealized PnL using:
          entry effective buy fill (avg_entry_px + buy slippage)
          current effective sell fill (px - sell slippage)
        """
        if not self.in_pos() or self.avg_entry_px is None:
            return Decimal("0").quantize(MONEY_QUANT, rounding=ROUND_DOWN)

        entry_fill = apply_buy_slippage(cfg, self.avg_entry_px)
        cur_fill = apply_sell_slippage(cfg, px)
        return _q_money((cur_fill - entry_fill) * self.position_qty)

    # -------------------------
    # Sizing logic
    # -------------------------
    def _max_additional_exposure_usd(self, px: Decimal, max_total_exposure: Decimal) -> Decimal:
        """
        max_total_exposure is fraction of equity, e.g. 0.50.
        """
        px = _q_px(px)
        if px <= 0:
            return Decimal("0").quantize(MONEY_QUANT, rounding=ROUND_DOWN)

        eq = self.equity_usd(px)
        cap = _q_money(eq * _as_decimal(max_total_exposure, "0"))
        cur = self.exposure_usd(px)
        room = cap - cur
        return _q_money(max(Decimal("0"), room))

    def compute_buy_qty(self, px: Decimal, cfg: Dict) -> Tuple[Decimal, str]:
        """
        Returns (qty, reason). qty=0 means cannot buy.

        Enforces:
          - MAX_TRADE_FRACTION of CASH per trade
          - optional USD_PER_TRADE cap (if > 0)
          - MAX_TOTAL_EXPOSURE of EQUITY
          - MIN_ORDER_USD

        Notes:
          - fee-aware sizing so total_cost stays within caps
          - sizes off mid px; slippage affects effective fill, but caps are handled conservatively via fee multiplier
        """
        px = _q_px(px)
        if px <= 0:
            return Decimal("0").quantize(QTY_QUANT, rounding=ROUND_DOWN), "BAD_PRICE"

        max_trade_fraction = _as_decimal(cfg.get("MAX_TRADE_FRACTION", "0"), "0")
        max_total_exposure = _as_decimal(cfg.get("MAX_TOTAL_EXPOSURE", "0"), "0")
        min_order_usd = _q_money(cfg.get("MIN_ORDER_USD", "0"))

        usd_per_trade = _q_money(cfg.get("USD_PER_TRADE", "0"))
        # Compounding: scale USD_PER_TRADE as % of equity instead of fixed amount
        compound_pct = _as_decimal(cfg.get("COMPOUND_SIZE_PCT", "0"), "0")
        if compound_pct > 0:
            eq = self.equity_usd(px)
            usd_per_trade = _q_money(eq * compound_pct)
        fee_bps_val = _as_decimal(cfg.get("FEE_BPS", "0"), "0")

        if self.cash_usd <= 0:
            return Decimal("0").quantize(QTY_QUANT, rounding=ROUND_DOWN), "NO_CASH"

        if max_trade_fraction <= 0:
            return Decimal("0").quantize(QTY_QUANT, rounding=ROUND_DOWN), "MAX_TRADE_FRACTION_ZERO"

        spend_cap = _q_money(self.cash_usd * max_trade_fraction)

        if usd_per_trade > 0:
            spend_cap = min(spend_cap, usd_per_trade)

        exposure_room = self._max_additional_exposure_usd(px, max_total_exposure)

        fee_mult = Decimal("1") + bps(fee_bps_val)

        notional_cap = min(spend_cap, exposure_room, self.cash_usd)
        buy_notional = _q_money(notional_cap / fee_mult) if fee_mult > 0 else Decimal("0").quantize(MONEY_QUANT, rounding=ROUND_DOWN)

        if buy_notional < min_order_usd:
            if exposure_room < min_order_usd:
                return Decimal("0").quantize(QTY_QUANT, rounding=ROUND_DOWN), "EXPOSURE_CAP"
            return Decimal("0").quantize(QTY_QUANT, rounding=ROUND_DOWN), "MIN_ORDER"

        qty = _q_qty(buy_notional / px)
        if qty <= 0:
            return Decimal("0").quantize(QTY_QUANT, rounding=ROUND_DOWN), "QTY_ZERO"

        return qty, "OK"

    def compute_buy_qty_and_reason(self, px: Decimal, cfg: Dict) -> Tuple[Decimal, str]:
        """
        Convenience alias so engine/main never has to guess signatures.
        """
        return self.compute_buy_qty(px, cfg)

    # -------------------------
    # Fills
    # -------------------------
    def buy(self, qty: Decimal, px: Decimal, epoch: int, cfg: Dict) -> Tuple[Decimal, Decimal]:
        """
        Paper buy fill.

        Returns:
          (effective_fill_px, total_cost_usd)
        """
        qty = _q_qty(qty)
        px = _q_px(px)
        if qty <= 0:
            raise ValueError("QTY<=0")
        if px <= 0:
            raise ValueError("BAD_PRICE")

        fill_px = apply_buy_slippage(cfg, px)
        notional = _q_money(qty * fill_px)
        fee = fee_usd(cfg, notional)
        total_cost = _q_money(notional + fee)

        if total_cost > self.cash_usd:
            raise ValueError(f"INSUFFICIENT_CASH need={total_cost} cash={self.cash_usd}")

        if not self.in_pos():
            self.avg_entry_px = px
            self.entry_epoch = epoch
            self.peak_px = px
        else:
            new_qty = _q_qty(self.position_qty + qty)
            if self.avg_entry_px is None:
                self.avg_entry_px = px
            else:
                self.avg_entry_px = _q_px(
                    ((self.avg_entry_px * self.position_qty) + (px * qty)) / new_qty
                )

        self.position_qty = _q_qty(self.position_qty + qty)
        self.cash_usd = _q_money(self.cash_usd - total_cost)
        self.peak_px = _q_px(max(self.peak_px or px, px))
        self.normalize_state()

        return fill_px, total_cost

    # --------- LINE ABOVE: return fill_px, total_cost
    def apply_fill(
        self,
        *,
        side: str,
        qty: Decimal,
        px: Decimal,
        epoch: int,
        cfg: Dict,
        fee_override_usd: Optional[Decimal] = None,
    ) -> Tuple[Decimal, Decimal, Decimal]:
        """
        Canonical fill-driven ledger mutation for Phase 8.

        Returns:
          (effective_fill_px, cash_delta_usd, realized_pnl_delta_usd)

        Notes:
          - BUY:
              cash_delta is negative total spend
              realized_pnl_delta is 0
          - SELL:
              cash_delta is positive net proceeds
              realized_pnl_delta is realized gain/loss
          - qty is assumed to be the actual fill quantity
          - side must be BUY or SELL
        """
        side_u = str(side).strip().upper()
        qty_d = _q_qty(qty)
        px_d = _q_px(px)
        fee_override = None if fee_override_usd is None else _q_money(fee_override_usd)

        if qty_d <= 0:
            raise ValueError("QTY<=0")
        if px_d <= 0:
            raise ValueError("BAD_PRICE")

        if side_u == "BUY":
            fill_px = apply_buy_slippage(cfg, px_d)
            notional = _q_money(qty_d * fill_px)
            fee = fee_usd(cfg, notional) if fee_override is None else fee_override
            total_cost = _q_money(notional + fee)

            if total_cost > self.cash_usd:
                raise ValueError(f"INSUFFICIENT_CASH need={total_cost} cash={self.cash_usd}")

            if not self.in_pos():
                self.avg_entry_px = px_d
                self.entry_epoch = epoch
                self.peak_px = px_d
            else:
                new_qty = _q_qty(self.position_qty + qty_d)
                if self.avg_entry_px is None:
                    self.avg_entry_px = px_d
                else:
                    self.avg_entry_px = _q_px(
                        ((self.avg_entry_px * self.position_qty) + (px_d * qty_d)) / new_qty
                    )

            self.position_qty = _q_qty(self.position_qty + qty_d)
            self.cash_usd = _q_money(self.cash_usd - total_cost)
            self.peak_px = _q_px(max(self.peak_px or px_d, px_d))
            self.normalize_state()

            return fill_px, _q_money(-total_cost), Decimal("0").quantize(MONEY_QUANT, rounding=ROUND_DOWN)

        if side_u == "SELL":
            if not self.in_pos() or self.avg_entry_px is None:
                raise ValueError("NO_POSITION")

            sell_qty = min(qty_d, self.position_qty)
            sell_qty = _q_qty(sell_qty)
            if sell_qty <= 0:
                raise ValueError("QTY<=0")

            entry_fill = apply_buy_slippage(cfg, self.avg_entry_px)
            exit_fill = apply_sell_slippage(cfg, px_d)

            gross = _q_money((exit_fill - entry_fill) * sell_qty)
            notional_exit = _q_money(sell_qty * exit_fill)
            fee = fee_usd(cfg, notional_exit) if fee_override is None else fee_override

            realized_add = _q_money(gross - fee)
            net_proceeds = _q_money(notional_exit - fee)

            self.cash_usd = _q_money(self.cash_usd + net_proceeds)
            self.realized_pnl_usd = _q_money(self.realized_pnl_usd + realized_add)
            self.position_qty = _q_qty(self.position_qty - sell_qty)

            if self.position_qty <= 0:
                self.position_qty = Decimal("0").quantize(QTY_QUANT, rounding=ROUND_DOWN)
                self.avg_entry_px = None
                self.entry_epoch = None
                self.peak_px = None

            self.normalize_state()
            return exit_fill, net_proceeds, realized_add

        raise ValueError(f"UNSUPPORTED_SIDE:{side_u}")

    def sell_all(self, px: Decimal, epoch: int, cfg: Dict) -> Tuple[Decimal, Decimal, Decimal]:
        return self.sell(px=px, epoch=epoch, cfg=cfg, qty=None)

    def sell(
        self,
        px: Decimal,
        epoch: int,
        cfg: Dict,
        qty: Optional[Decimal] = None,
    ) -> Tuple[Decimal, Decimal, Decimal]:
        """
        Paper sell fill.

        Returns:
          (effective_sell_px, net_proceeds_usd, realized_pnl_usd)
        """
        px = _q_px(px)
        if not self.in_pos() or self.avg_entry_px is None:
            raise ValueError("NO_POSITION")
        if px <= 0:
            raise ValueError("BAD_PRICE")

        qty = self.position_qty if qty is None else min(_q_qty(qty), self.position_qty)
        qty = _q_qty(qty)
        if qty <= 0:
            raise ValueError("QTY<=0")

        entry_fill = apply_buy_slippage(cfg, self.avg_entry_px)
        exit_fill = apply_sell_slippage(cfg, px)

        gross = _q_money((exit_fill - entry_fill) * qty)
        notional_exit = _q_money(qty * exit_fill)
        fee = fee_usd(cfg, notional_exit)

        realized_add = _q_money(gross - fee)
        net_proceeds = _q_money(notional_exit - fee)

        self.cash_usd = _q_money(self.cash_usd + net_proceeds)
        self.realized_pnl_usd = _q_money(self.realized_pnl_usd + realized_add)
        self.position_qty = _q_qty(self.position_qty - qty)

        if self.position_qty <= 0:
            self.position_qty = Decimal("0").quantize(QTY_QUANT, rounding=ROUND_DOWN)
            self.avg_entry_px = None
            self.entry_epoch = None
            self.peak_px = None

        self.normalize_state()
        return exit_fill, net_proceeds, realized_add

    def update_peak(self, px: Decimal) -> None:
        px = _q_px(px)
        if self.in_pos() and px > 0:
            self.peak_px = _q_px(max(self.peak_px or px, px))
            self.normalize_state()


# Backwards-compat alias
Ledger = VirtualLedger