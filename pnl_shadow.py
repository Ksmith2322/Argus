from decimal import Decimal
from typing import Tuple, Dict

def bps(x: Decimal) -> Decimal:
    return x / Decimal(10_000)

def shadow_fill_prices(cfg: Dict, mid_px: Decimal) -> Tuple[Decimal, Decimal]:
    """Returns (buy_fill_px, sell_fill_px) incorporating slippage."""
    buy_px = mid_px * (Decimal("1") + bps(cfg["SLIPPAGE_BPS"]))
    sell_px = mid_px * (Decimal("1") - bps(cfg["SLIPPAGE_BPS"]))
    return buy_px, sell_px

def shadow_pnl_usd(cfg: Dict, entry_px: Decimal, exit_px: Decimal, notional_usd: Decimal) -> Tuple[Decimal, Decimal]:
    """
    Shadow PnL for a notional USD position.
    - Buys notional_usd worth at entry, sells all at exit.
    - Applies fees on both sides.
    Returns (pnl_usd, pnl_pct).
    """
    if entry_px <= 0:
        return Decimal("0"), Decimal("0")

    qty = notional_usd / entry_px
    gross = (exit_px - entry_px) * qty

    entry_fee = notional_usd * bps(cfg["FEE_BPS"])
    exit_fee = (qty * exit_px) * bps(cfg["FEE_BPS"])

    pnl = gross - entry_fee - exit_fee
    pnl_pct = pnl / notional_usd
    return pnl, pnl_pct
