"""apollo/strategies/catalyst_signals.py -- Multi-source catalyst signal detection.

Extracts 4 signal types from free yfinance data:
  1. Options flow — unusual call/put activity, put/call ratio shifts
  2. Insider transactions — net buying/selling in last 30/90 days
  3. Analyst revisions — upgrade/downgrade clustering
  4. News sentiment — headline keyword scoring (basic)

All signals return a score [-50, +50] and direction bias.
Positive = bullish catalyst, Negative = bearish catalyst.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    yf = None

_log = logging.getLogger("apollo.signals")


def analyze_options_flow(symbol: str) -> dict:
    """Analyze options activity for unusual flow signals.

    Key metrics:
      - Put/Call volume ratio (< 0.5 = very bullish, > 1.5 = very bearish)
      - Put/Call OI ratio (institutional positioning)
      - ATM implied volatility (how big a move market expects)
      - Unusual volume (total options vol vs open interest)
    """
    result = {
        "signal": 0, "direction": "neutral", "details": [],
        "pc_vol_ratio": None, "pc_oi_ratio": None,
        "atm_iv": None, "total_call_vol": 0, "total_put_vol": 0,
    }

    if yf is None:
        return result

    try:
        ticker = yf.Ticker(symbol)
        expirations = ticker.options
        if not expirations:
            return result

        # Get nearest expiry chain
        chain = ticker.option_chain(expirations[0])
        calls = chain.calls
        puts = chain.puts

        if calls.empty or puts.empty:
            return result

        # Total volumes
        call_vol = calls["volume"].sum()
        put_vol = puts["volume"].sum()
        call_oi = calls["openInterest"].sum()
        put_oi = puts["openInterest"].sum()

        result["total_call_vol"] = int(call_vol) if not np.isnan(call_vol) else 0
        result["total_put_vol"] = int(put_vol) if not np.isnan(put_vol) else 0

        # Put/Call ratios
        pc_vol = put_vol / call_vol if call_vol > 0 else 1.0
        pc_oi = put_oi / call_oi if call_oi > 0 else 1.0
        result["pc_vol_ratio"] = round(pc_vol, 3)
        result["pc_oi_ratio"] = round(pc_oi, 3)

        # Score based on P/C ratio
        if pc_vol < 0.4:
            result["signal"] += 25
            result["details"].append(f"VERY_BULLISH: P/C vol={pc_vol:.2f} (heavy call buying)")
        elif pc_vol < 0.6:
            result["signal"] += 15
            result["details"].append(f"BULLISH: P/C vol={pc_vol:.2f}")
        elif pc_vol > 1.5:
            result["signal"] -= 20
            result["details"].append(f"BEARISH: P/C vol={pc_vol:.2f} (heavy put buying)")
        elif pc_vol > 1.2:
            result["signal"] -= 10
            result["details"].append(f"CAUTIOUS: P/C vol={pc_vol:.2f}")

        # OI positioning (institutional)
        if pc_oi < 0.5:
            result["signal"] += 10
            result["details"].append(f"Institutional bullish OI: P/C={pc_oi:.2f}")
        elif pc_oi > 1.3:
            result["signal"] -= 10
            result["details"].append(f"Institutional bearish OI: P/C={pc_oi:.2f}")

        # ATM implied volatility
        info = ticker.info or {}
        price = info.get("currentPrice", info.get("regularMarketPrice", 0))
        if price > 0:
            atm_calls = calls.iloc[(calls["strike"] - price).abs().argsort()[:3]]
            atm_iv = atm_calls["impliedVolatility"].mean()
            result["atm_iv"] = round(atm_iv * 100, 1) if not np.isnan(atm_iv) else None
            if atm_iv > 0.8:
                result["details"].append(f"HIGH_IV: {atm_iv*100:.0f}% — market expects big move")

        # Unusual volume (vol > 2x OI = unusual activity)
        if call_oi > 0 and call_vol / call_oi > 2:
            result["signal"] += 10
            result["details"].append(f"UNUSUAL_CALL_VOL: {call_vol/call_oi:.1f}x OI")
        if put_oi > 0 and put_vol / put_oi > 2:
            result["signal"] -= 10
            result["details"].append(f"UNUSUAL_PUT_VOL: {put_vol/put_oi:.1f}x OI")

        result["direction"] = "bullish" if result["signal"] > 5 else ("bearish" if result["signal"] < -5 else "neutral")

    except Exception as e:
        _log.debug(f"{symbol} options error: {e}")

    return result


def analyze_insider_activity(symbol: str, days: int = 90) -> dict:
    """Analyze insider buying/selling patterns.

    Strong signal: multiple insiders buying in last 30 days.
    Weak signal: routine selling (officers sell for tax/liquidity).
    """
    result = {
        "signal": 0, "direction": "neutral", "details": [],
        "buys_30d": 0, "sells_30d": 0, "buys_90d": 0, "sells_90d": 0,
        "net_value_30d": 0,
    }

    if yf is None:
        return result

    try:
        ticker = yf.Ticker(symbol)
        transactions = ticker.insider_transactions

        if transactions is None or transactions.empty:
            return result

        now = datetime.now()
        cutoff_30 = now - timedelta(days=30)
        cutoff_90 = now - timedelta(days=90)

        for _, row in transactions.iterrows():
            text = str(row.get("Text", "")).lower()
            shares = row.get("Shares", 0) or 0
            value = row.get("Value", 0) or 0
            date_str = row.get("Start Date", "")

            try:
                txn_date = pd.Timestamp(date_str)
            except Exception:
                continue

            is_buy = "purchase" in text or "acquisition" in text
            is_sell = "sale" in text and "gift" not in text

            if txn_date >= pd.Timestamp(cutoff_90):
                if is_buy:
                    result["buys_90d"] += 1
                elif is_sell:
                    result["sells_90d"] += 1

            if txn_date >= pd.Timestamp(cutoff_30):
                if is_buy:
                    result["buys_30d"] += 1
                    result["net_value_30d"] += value
                elif is_sell:
                    result["sells_30d"] += 1
                    result["net_value_30d"] -= value

        # Score
        if result["buys_30d"] >= 3:
            result["signal"] += 30
            result["details"].append(f"STRONG_INSIDER_BUY: {result['buys_30d']} buys in 30d (${result['net_value_30d']/1e6:.1f}M)")
        elif result["buys_30d"] >= 1:
            result["signal"] += 15
            result["details"].append(f"INSIDER_BUY: {result['buys_30d']} buys in 30d")

        # Heavy selling is only mildly bearish (officers sell routinely)
        if result["sells_30d"] >= 5 and result["buys_30d"] == 0:
            result["signal"] -= 10
            result["details"].append(f"INSIDER_SELLING: {result['sells_30d']} sells, 0 buys in 30d")

        result["direction"] = "bullish" if result["signal"] > 5 else ("bearish" if result["signal"] < -5 else "neutral")

    except Exception as e:
        _log.debug(f"{symbol} insider error: {e}")

    return result


def analyze_analyst_revisions(symbol: str, days: int = 30) -> dict:
    """Analyze recent analyst upgrades/downgrades.

    Clustering of upgrades before earnings = very bullish.
    """
    result = {
        "signal": 0, "direction": "neutral", "details": [],
        "upgrades_30d": 0, "downgrades_30d": 0,
        "avg_target": None, "buy_count": 0, "hold_count": 0, "sell_count": 0,
    }

    if yf is None:
        return result

    try:
        ticker = yf.Ticker(symbol)

        # Current recommendations summary
        recs = ticker.recommendations
        if recs is not None and not recs.empty:
            latest = recs.iloc[0]
            result["buy_count"] = int(latest.get("strongBuy", 0) or 0) + int(latest.get("buy", 0) or 0)
            result["hold_count"] = int(latest.get("hold", 0) or 0)
            result["sell_count"] = int(latest.get("sell", 0) or 0) + int(latest.get("strongSell", 0) or 0)

            total = result["buy_count"] + result["hold_count"] + result["sell_count"]
            if total > 0:
                buy_pct = result["buy_count"] / total
                if buy_pct > 0.8:
                    result["signal"] += 15
                    result["details"].append(f"CONSENSUS_BUY: {buy_pct:.0%} buy ({result['buy_count']}/{total})")
                elif buy_pct < 0.3:
                    result["signal"] -= 10
                    result["details"].append(f"CONSENSUS_SELL: {buy_pct:.0%} buy ({result['buy_count']}/{total})")

        # Recent upgrades/downgrades
        ud = ticker.upgrades_downgrades
        if ud is not None and not ud.empty:
            cutoff = datetime.now() - timedelta(days=days)
            for idx, row in ud.iterrows():
                try:
                    grade_date = pd.Timestamp(idx)
                except Exception:
                    continue
                if grade_date < pd.Timestamp(cutoff):
                    continue
                action = str(row.get("Action", "")).lower()
                if action == "up":
                    result["upgrades_30d"] += 1
                elif action == "down":
                    result["downgrades_30d"] += 1

            if result["upgrades_30d"] >= 3:
                result["signal"] += 20
                result["details"].append(f"UPGRADE_CLUSTER: {result['upgrades_30d']} upgrades in {days}d")
            elif result["upgrades_30d"] >= 1:
                result["signal"] += 5

            if result["downgrades_30d"] >= 3:
                result["signal"] -= 15
                result["details"].append(f"DOWNGRADE_CLUSTER: {result['downgrades_30d']} downgrades in {days}d")

        result["direction"] = "bullish" if result["signal"] > 5 else ("bearish" if result["signal"] < -5 else "neutral")

    except Exception as e:
        _log.debug(f"{symbol} analyst error: {e}")

    return result


def get_all_signals(symbol: str) -> dict:
    """Run all catalyst signal analyses for a symbol.

    Returns combined score and individual breakdowns.
    """
    options = analyze_options_flow(symbol)
    insider = analyze_insider_activity(symbol)
    analyst = analyze_analyst_revisions(symbol)

    # Combined score
    total_signal = options["signal"] + insider["signal"] + analyst["signal"]
    total_signal = max(-100, min(100, total_signal))

    # Combined details
    all_details = options["details"] + insider["details"] + analyst["details"]

    # Direction consensus
    directions = [options["direction"], insider["direction"], analyst["direction"]]
    bullish = sum(1 for d in directions if d == "bullish")
    bearish = sum(1 for d in directions if d == "bearish")

    if bullish >= 2:
        consensus = "bullish"
    elif bearish >= 2:
        consensus = "bearish"
    elif total_signal > 10:
        consensus = "bullish"
    elif total_signal < -10:
        consensus = "bearish"
    else:
        consensus = "neutral"

    return {
        "symbol": symbol,
        "total_signal": total_signal,
        "consensus": consensus,
        "details": all_details,
        "options": options,
        "insider": insider,
        "analyst": analyst,
    }
