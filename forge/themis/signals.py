"""Themis — Signal detection engine for congressional trades."""

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from forge.themis.db import get_active_signals, insert_signal, update_signal_returns


def _is_buy(tx_type: str) -> bool:
    return tx_type.lower().startswith("purchase")


def _is_sell(tx_type: str) -> bool:
    return "sale" in tx_type.lower() or "sell" in tx_type.lower()


def _trade_direction(tx_type: str) -> str | None:
    if _is_buy(tx_type):
        return "buy"
    if _is_sell(tx_type):
        return "sell"
    return None


def _cluster_signals(trades: list[dict], direction: str, window_days: int = 14, min_members: int = 3) -> list[dict]:
    """Find clusters where 3+ members trade same ticker within window_days."""
    signal_type = f"CLUSTER_{'BUY' if direction == 'buy' else 'SELL'}"
    check_fn = _is_buy if direction == "buy" else _is_sell

    # Group by ticker
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        if check_fn(t["transaction_type"]):
            by_ticker[t["ticker"]].append(t)

    signals = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    for ticker, ticker_trades in by_ticker.items():
        if len(ticker_trades) < min_members:
            continue

        # Sort by date
        ticker_trades.sort(key=lambda x: x["transaction_date"])

        # Sliding window: check each trade as potential cluster start
        for i, anchor in enumerate(ticker_trades):
            anchor_date = datetime.strptime(anchor["transaction_date"], "%Y-%m-%d")
            window_end = anchor_date + timedelta(days=window_days)

            cluster = []
            seen_members = set()
            for t in ticker_trades[i:]:
                t_date = datetime.strptime(t["transaction_date"], "%Y-%m-%d")
                if t_date > window_end:
                    break
                if t["representative"] not in seen_members:
                    cluster.append(t)
                    seen_members.add(t["representative"])

            if len(seen_members) >= min_members:
                parties = {t.get("party", "?") for t in cluster if t.get("party")}
                cross_party = len(parties & {"D", "R"}) == 2

                members_list = sorted(seen_members)
                avg_low = sum(t.get("amount_low", 0) or 0 for t in cluster) / len(cluster)

                priority = "high" if len(seen_members) >= 5 or cross_party else "normal"
                desc_parts = [f"{len(seen_members)} members {direction} {ticker} within {window_days}d"]
                if cross_party:
                    desc_parts.append("cross-party")
                if priority == "high":
                    desc_parts.append("HIGH PRIORITY")

                signals.append({
                    "signal_type": signal_type,
                    "detected_at": now,
                    "ticker": ticker,
                    "members": json.dumps(members_list),
                    "member_count": len(seen_members),
                    "avg_amount_low": avg_low,
                    "description": " | ".join(desc_parts),
                    "status": "active",
                    "entry_price": None,
                })
                break  # One signal per ticker

    return signals


def _big_trade_signals(trades: list[dict], direction: str, threshold: float) -> list[dict]:
    """Find big individual trades above threshold."""
    signal_type = f"BIG_{'BUY' if direction == 'buy' else 'SELL'}"
    check_fn = _is_buy if direction == "buy" else _is_sell
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    signals = []
    for t in trades:
        if not check_fn(t["transaction_type"]):
            continue
        if (t.get("amount_low") or 0) >= threshold:
            signals.append({
                "signal_type": signal_type,
                "detected_at": now,
                "ticker": t["ticker"],
                "members": json.dumps([t["representative"]]),
                "member_count": 1,
                "avg_amount_low": t.get("amount_low", 0),
                "description": f"{t['representative']} {direction} {t['ticker']} — {t.get('amount_range', '?')}",
                "status": "active",
                "entry_price": None,
            })
    return signals


def _unanimous_signals(trades: list[dict], window_days: int = 30, min_trades: int = 3) -> list[dict]:
    """All trades in a ticker within window are same direction, 3+ trades."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Group by ticker
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        d = _trade_direction(t["transaction_type"])
        if d:
            by_ticker[t["ticker"]].append(t)

    signals = []
    for ticker, ticker_trades in by_ticker.items():
        if len(ticker_trades) < min_trades:
            continue

        # Check recent window
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%d")
        recent = [t for t in ticker_trades if t["transaction_date"] >= cutoff]
        if len(recent) < min_trades:
            continue

        directions = {_trade_direction(t["transaction_type"]) for t in recent}
        if len(directions) == 1:
            direction = directions.pop()
            members = sorted({t["representative"] for t in recent})
            avg_low = sum(t.get("amount_low", 0) or 0 for t in recent) / len(recent)

            signals.append({
                "signal_type": "UNANIMOUS_DIRECTION",
                "detected_at": now,
                "ticker": ticker,
                "members": json.dumps(members),
                "member_count": len(members),
                "avg_amount_low": avg_low,
                "description": f"All {len(recent)} trades in {ticker} are {direction}s over {window_days}d",
                "status": "active",
                "entry_price": None,
            })
    return signals


def detect_signals(conn, lookback_days: int = 30) -> list[dict]:
    """Scan recent trades for all signal types. Return new signals not already in DB."""
    trades = conn.execute(
        "SELECT * FROM trades WHERE transaction_date >= date('now', ?)",
        (f"-{lookback_days} days",),
    ).fetchall()
    trades = [dict(t) for t in trades]

    if not trades:
        return []

    # Existing signals to dedup
    existing = conn.execute(
        "SELECT signal_type, ticker FROM signals WHERE detected_at >= date('now', ?)",
        (f"-{lookback_days} days",),
    ).fetchall()
    existing_keys = {(r["signal_type"], r["ticker"]) for r in existing}

    all_candidates = []
    all_candidates.extend(_cluster_signals(trades, "buy"))
    all_candidates.extend(_cluster_signals(trades, "sell"))
    all_candidates.extend(_big_trade_signals(trades, "buy", threshold=50_000))
    all_candidates.extend(_big_trade_signals(trades, "sell", threshold=100_000))
    all_candidates.extend(_unanimous_signals(trades))

    new_signals = []
    for sig in all_candidates:
        key = (sig["signal_type"], sig["ticker"])
        if key not in existing_keys:
            sig_id = insert_signal(conn, sig)
            sig["id"] = sig_id
            new_signals.append(sig)
            existing_keys.add(key)

    return new_signals


def score_signals(conn) -> dict:
    """Score signals past their 30/60/90 day windows with actual returns."""
    try:
        import yfinance as yf
    except ImportError:
        print("[themis] yfinance not installed — skipping scoring")
        return {"scored": 0, "errors": 0}

    active = get_active_signals(conn)
    scored = 0
    errors = 0
    now = datetime.now(timezone.utc)

    for sig in active:
        detected = datetime.strptime(sig["detected_at"][:10], "%Y-%m-%d")
        ticker = sig["ticker"]

        for days in [30, 60, 90]:
            col = f"alpha_{days}d"
            if sig.get(col) is not None:
                continue  # already scored

            target_date = detected + timedelta(days=days)
            if now.replace(tzinfo=None) < target_date:
                continue  # not mature yet

            try:
                start = detected - timedelta(days=1)
                end = target_date + timedelta(days=3)
                stock = yf.download(ticker, start=start.strftime("%Y-%m-%d"),
                                    end=end.strftime("%Y-%m-%d"), progress=False)
                spy = yf.download("SPY", start=start.strftime("%Y-%m-%d"),
                                  end=end.strftime("%Y-%m-%d"), progress=False)

                if stock.empty or spy.empty or len(stock) < 2 or len(spy) < 2:
                    continue

                stock_ret = (float(stock["Close"].iloc[-1]) / float(stock["Close"].iloc[0])) - 1
                spy_ret = (float(spy["Close"].iloc[-1]) / float(spy["Close"].iloc[0])) - 1

                update_signal_returns(conn, sig["id"], days, stock_ret, spy_ret)
                scored += 1
            except Exception as e:
                print(f"[themis] Score error {ticker} {days}d: {e}")
                errors += 1

    # Update status for fully scored signals
    conn.execute("""
        UPDATE signals SET status = 'scored'
        WHERE alpha_30d IS NOT NULL AND alpha_60d IS NOT NULL AND alpha_90d IS NOT NULL
        AND status = 'active'
    """)
    conn.commit()

    return {"scored": scored, "errors": errors}


def get_top_signals(conn, n: int = 10) -> list[dict]:
    """Most recent high-priority signals."""
    rows = conn.execute(
        "SELECT * FROM signals ORDER BY detected_at DESC LIMIT ?", (n,)
    ).fetchall()
    return [dict(r) for r in rows]


def format_signal_alert(signal: dict) -> str:
    """Format a signal for Discord notification."""
    emoji = {
        "CLUSTER_BUY": "🟢",
        "CLUSTER_SELL": "🔴",
        "BIG_BUY": "💰",
        "BIG_SELL": "💸",
        "UNANIMOUS_DIRECTION": "🎯",
    }.get(signal["signal_type"], "📊")

    members = signal.get("members", "[]")
    if isinstance(members, str):
        try:
            members = json.loads(members)
        except json.JSONDecodeError:
            members = [members]

    lines = [
        f"{emoji} **{signal['signal_type']}** — {signal['ticker']}",
        f"Members ({signal.get('member_count', len(members))}): {', '.join(members[:5])}",
    ]
    if signal.get("avg_amount_low"):
        lines.append(f"Avg amount: ${signal['avg_amount_low']:,.0f}+")
    if signal.get("description"):
        lines.append(signal["description"])
    return "\n".join(lines)
