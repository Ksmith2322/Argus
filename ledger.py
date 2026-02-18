# line above: from typing import Dict, Optional, Tuple, Any
# ledger.py
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Dict, Optional, Tuple, Any


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


def bps(x: Decimal) -> Decimal:
    return x / Decimal("10000")


def apply_buy_slippage(cfg: Dict, mid_px: Decimal) -> Decimal:
    slip_bps = _as_decimal(cfg.get("SLIPPAGE_BPS", "0"), "0")
    return _as_decimal(mid_px) * (Decimal("1") + bps(slip_bps))


def apply_sell_slippage(cfg: Dict, mid_px: Decimal) -> Decimal:
    slip_bps = _as_decimal(cfg.get("SLIPPAGE_BPS", "0"), "0")
    return _as_decimal(mid_px) * (Decimal("1") - bps(slip_bps))


def fee_usd(cfg: Dict, notional_usd: Decimal) -> Decimal:
    fee_bps = _as_decimal(cfg.get("FEE_BPS", "0"), "0")
    return _as_decimal(notional_usd) * bps(fee_bps)


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
        start_cash = _as_decimal(cfg.get("START_CASH_USD", "0"), "0")
        return cls(cash_usd=start_cash)

    # -------------------------
    # State helpers
    # -------------------------
    def in_pos(self) -> bool:
        return self.position_qty > 0

    def exposure_usd(self, px: Decimal) -> Decimal:
        return self.position_qty * _as_decimal(px)

    def equity_usd(self, px: Decimal) -> Decimal:
        return self.cash_usd + self.exposure_usd(px)

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
            return Decimal("0")

        entry_fill = apply_buy_slippage(cfg, self.avg_entry_px)
        cur_fill = apply_sell_slippage(cfg, px)
        return (cur_fill - entry_fill) * self.position_qty

    # -------------------------
    # Sizing logic
    # -------------------------
    def _max_additional_exposure_usd(self, px: Decimal, max_total_exposure: Decimal) -> Decimal:
        """
        max_total_exposure is fraction of equity, e.g. 0.50.
        """
        px = _as_decimal(px)
        if px <= 0:
            return Decimal("0")

        eq = self.equity_usd(px)
        cap = eq * _as_decimal(max_total_exposure, "0")
        cur = self.exposure_usd(px)
        room = cap - cur
        return max(Decimal("0"), room)

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
        px = _as_decimal(px)
        if px <= 0:
            return Decimal("0"), "BAD_PRICE"

        max_trade_fraction = _as_decimal(cfg.get("MAX_TRADE_FRACTION", "0"), "0")
        max_total_exposure = _as_decimal(cfg.get("MAX_TOTAL_EXPOSURE", "0"), "0")
        min_order_usd = _as_decimal(cfg.get("MIN_ORDER_USD", "0"), "0")

        usd_per_trade = _as_decimal(cfg.get("USD_PER_TRADE", "0"), "0")
        fee_bps_val = _as_decimal(cfg.get("FEE_BPS", "0"), "0")

        if self.cash_usd <= 0:
            return Decimal("0"), "NO_CASH"

        if max_trade_fraction <= 0:
            return Decimal("0"), "MAX_TRADE_FRACTION_ZERO"

        # Base spend cap from cash fraction
        spend_cap = self.cash_usd * max_trade_fraction

        # Optional override: hard cap the intended notional per trade
        if usd_per_trade > 0:
            spend_cap = min(spend_cap, usd_per_trade)

        exposure_room = self._max_additional_exposure_usd(px, max_total_exposure)

        # fee-aware notional: total_cost = buy_notional * (1 + fee_bps)
        fee_mult = Decimal("1") + bps(fee_bps_val)

        # Most conservative cap among: spend, exposure room, available cash
        notional_cap = min(spend_cap, exposure_room, self.cash_usd)

        # The actual pre-fee buy notional
        buy_notional = notional_cap / fee_mult if fee_mult > 0 else Decimal("0")

        if buy_notional < min_order_usd:
            if exposure_room < min_order_usd:
                return Decimal("0"), "EXPOSURE_CAP"
            return Decimal("0"), "MIN_ORDER"

        qty = buy_notional / px
        if qty <= 0:
            return Decimal("0"), "QTY_ZERO"

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
        qty = _as_decimal(qty)
        px = _as_decimal(px)
        if qty <= 0:
            raise ValueError("QTY<=0")
        if px <= 0:
            raise ValueError("BAD_PRICE")

        fill_px = apply_buy_slippage(cfg, px)
        notional = qty * fill_px
        fee = fee_usd(cfg, notional)
        total_cost = notional + fee

        if total_cost > self.cash_usd:
            raise ValueError(f"INSUFFICIENT_CASH need={total_cost} cash={self.cash_usd}")

        if not self.in_pos():
            self.avg_entry_px = px
            self.entry_epoch = epoch
            self.peak_px = px
        else:
            # VWAP on mid px basis (not slippage-adjusted)
            new_qty = self.position_qty + qty
            if self.avg_entry_px is None:
                self.avg_entry_px = px
            else:
                self.avg_entry_px = (self.avg_entry_px * self.position_qty + px * qty) / new_qty

        self.position_qty += qty
        self.cash_usd -= total_cost
        self.peak_px = max(self.peak_px or px, px)

        return fill_px, total_cost

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
        px = _as_decimal(px)
        if not self.in_pos() or self.avg_entry_px is None:
            raise ValueError("NO_POSITION")
        if px <= 0:
            raise ValueError("BAD_PRICE")

        qty = self.position_qty if qty is None else min(_as_decimal(qty), self.position_qty)
        if qty <= 0:
            raise ValueError("QTY<=0")

        entry_fill = apply_buy_slippage(cfg, self.avg_entry_px)
        exit_fill = apply_sell_slippage(cfg, px)

        gross = (exit_fill - entry_fill) * qty
        notional_exit = qty * exit_fill
        fee = fee_usd(cfg, notional_exit)

        realized_add = gross - fee
        net_proceeds = notional_exit - fee

        self.cash_usd += net_proceeds
        self.realized_pnl_usd += realized_add
        self.position_qty -= qty

        if self.position_qty <= 0:
            self.position_qty = Decimal("0")
            self.avg_entry_px = None
            self.entry_epoch = None
            self.peak_px = None

        return exit_fill, net_proceeds, realized_add

    def update_peak(self, px: Decimal):
        px = _as_decimal(px)
        if self.in_pos() and px > 0:
            self.peak_px = max(self.peak_px or px, px)


# Backwards-compat alias
Ledger = VirtualLedger
