from __future__ import annotations

import logging

_log = logging.getLogger("argus.sizing")

DEFAULT_JPY_PIP_VALUE_PER_UNIT_USD = 0.000067


def fx_pip_value_per_unit_usd(
    symbol: str,
    *,
    quote_price: float | None = None,
    usd_jpy_price: float | None = None,
) -> float:
    """Approximate USD value of one pip for one unit of FX."""

    symbol = (symbol or "").upper()
    if len(symbol) < 6:
        return 0.0001

    quote = symbol[3:6]
    if quote == "USD":
        return 0.0001
    if quote == "JPY":
        ref = usd_jpy_price or quote_price
        if ref and ref > 0:
            return 0.01 / ref
        return DEFAULT_JPY_PIP_VALUE_PER_UNIT_USD
    return 0.0001


def fx_notional_per_unit_usd(
    symbol: str,
    *,
    quote_price: float | None = None,
    usd_jpy_price: float | None = None,
) -> float:
    """Approximate USD notional represented by one base-currency unit.

    This is intentionally separate from pip value. A USDJPY unit is roughly
    $1 of notional, not ``1 / USDJPY``. For JPY crosses, base/USD is inferred
    from cross / USDJPY when both prices are available.
    """

    symbol = (symbol or "").upper()
    if len(symbol) < 6:
        return 1.0

    base = symbol[:3]
    quote = symbol[3:6]
    px = float(quote_price or 0.0)
    usd_jpy = float(usd_jpy_price or 0.0)

    if base == "USD":
        return 1.0
    if quote == "USD" and px > 0:
        return px
    if quote == "JPY" and px > 0 and usd_jpy > 0:
        return px / usd_jpy
    return 1.0


RAW_SIZE_RATIO_WARN = 2.0   # ratio at which we log a FX_SIZE_OVERSIZED warning
RAW_SIZE_RATIO_REFUSE = 50.0  # extreme upper bound — refuse to size at all


def fx_units_for_risk(
    *,
    equity_usd: float,
    risk_pct: float,
    stop_pips: float,
    symbol: str,
    min_units: int = 1000,
    max_units: int | None = None,
    quote_price: float | None = None,
    usd_jpy_price: float | None = None,
) -> int:
    """Size FX units so stop-loss risk is approximately equity * risk_pct.

    2026-05-21: added RAW_SIZE_RATIO sanity gate. When the configured
    (risk_pct, stop_pips) combination demands more notional than the
    account can support, downstream NOTIONAL_CAP catches it — but the
    operator only sees the post-cap effective risk and can't tell how
    badly the strategy was structurally undersized. This gate surfaces:
      - the raw notional ratio (raw_notional / equity)
      - the intended risk amount vs the effective risk after cap
      - the diagnosis (tighten stop OR lower risk_pct)

    Behavior unchanged: same return value as before, only adds log.
    Extreme cases (ratio >= RAW_SIZE_RATIO_REFUSE) refuse to size and
    return 0 — defense against runaway formulas producing 1000x ratios.
    """

    if equity_usd <= 0 or risk_pct <= 0 or stop_pips <= 0:
        return 0

    pip_value = fx_pip_value_per_unit_usd(
        symbol,
        quote_price=quote_price,
        usd_jpy_price=usd_jpy_price,
    )
    if pip_value <= 0:
        return 0

    risk_amount = equity_usd * risk_pct
    raw_units = risk_amount / (stop_pips * pip_value)

    # ── RAW_SIZE_RATIO sanity gate ──
    notional_per_unit = fx_notional_per_unit_usd(
        symbol, quote_price=quote_price, usd_jpy_price=usd_jpy_price,
    )
    if notional_per_unit > 0 and equity_usd > 0:
        raw_notional = raw_units * notional_per_unit
        notional_ratio = raw_notional / equity_usd
        if notional_ratio >= RAW_SIZE_RATIO_REFUSE:
            _log.error(
                f"FX_SIZE_REFUSED ratio={notional_ratio:.1f}x exceeds {RAW_SIZE_RATIO_REFUSE}x "
                f"symbol={symbol} raw_units={raw_units:.0f} raw_notional=${raw_notional:.0f} "
                f"equity=${equity_usd:.0f}. Formula produced a notional far beyond any "
                f"reasonable account-leverage ceiling — strategy parameters are broken. "
                f"Refusing to size."
            )
            return 0
        if notional_ratio > RAW_SIZE_RATIO_WARN:
            # Effective risk after notional cap (cap = equity at 1.0x ratio)
            effective_risk = risk_amount / notional_ratio
            _log.warning(
                f"FX_SIZE_OVERSIZED symbol={symbol} ratio={notional_ratio:.1f}x "
                f"raw_units={raw_units:.0f} raw_notional=${raw_notional:.0f} "
                f"equity=${equity_usd:.0f} intended_risk=${risk_amount:.2f} "
                f"effective_risk_after_cap=${effective_risk:.2f}. "
                f"Strategy params (risk_pct={risk_pct:.4f}, stop_pips={stop_pips}) demand "
                f"more capital than account has. Tighten stop OR lower risk_pct to bring "
                f"raw_notional within ~1x equity."
            )

    sized = int(raw_units // min_units) * min_units
    if sized < min_units:
        _log.warning(
            f"SIZING_ZERO: equity={equity_usd:.2f} risk_pct={risk_pct} "
            f"stop_pips={stop_pips} raw_units={raw_units:.0f} min_units={min_units} "
            f"symbol={symbol}"
        )
        return 0
    if max_units is not None:
        sized = min(sized, max_units)
    return sized


def futures_contracts_for_risk(
    *,
    equity_usd: float,
    risk_pct: float,
    entry_price: float,
    stop_bps: float,
    multiplier: float,
    min_contracts: int = 1,
    max_contracts: int | None = None,
) -> int:
    """Size futures contracts so stop-loss risk is approximately equity * risk_pct."""

    if equity_usd <= 0 or risk_pct <= 0 or entry_price <= 0 or stop_bps <= 0 or multiplier <= 0:
        return 0

    stop_distance = entry_price * (stop_bps / 10000.0)
    risk_per_contract = stop_distance * multiplier
    if risk_per_contract <= 0:
        return 0

    raw_contracts = int((equity_usd * risk_pct) // risk_per_contract)
    if raw_contracts < min_contracts:
        # If calculated contracts is 0 but we CAN afford 1 contract at up to 2% risk, allow it
        max_risk_amount = equity_usd * 0.02  # 2% ceiling for minimum viability
        if risk_per_contract <= max_risk_amount:
            return 1  # Allow minimum 1 contract for paper/small accounts
        return 0  # Truly cannot afford even 1 contract
    if max_contracts is not None:
        raw_contracts = min(raw_contracts, max_contracts)
    return raw_contracts
