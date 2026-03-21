"""Kraken API connectivity test.

Validates all endpoints work correctly.

Usage:
    python -m argus_flow.tests.test_kraken_connectivity
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from argus_flow.adapters.kraken_client import KrakenClient, KrakenAPIError


def _ok(label: str) -> None:
    print(f"  [OK] {label}")


def _fail(label: str, err: str) -> None:
    print(f"  [FAIL] {label}: {err}")


def main() -> None:
    client = KrakenClient()
    passed = 0
    failed = 0

    print("=" * 60)
    print("KRAKEN CONNECTIVITY TEST")
    print("=" * 60)

    # ── Public endpoints ────────────────────────────────────
    print("\n--- Public Endpoints ---")

    try:
        status = client.get_system_status()
        assert status["status"] == "online", f"status={status['status']}"
        _ok(f"SystemStatus: {status['status']}")
        passed += 1
    except Exception as e:
        _fail("SystemStatus", str(e))
        failed += 1

    try:
        ticker = client.get_ticker("XBTUSD")
        pair_key = list(ticker.keys())[0]
        last_px = ticker[pair_key]["c"][0]
        _ok(f"Ticker XBTUSD: ${float(last_px):,.2f}")
        passed += 1
    except Exception as e:
        _fail("Ticker", str(e))
        failed += 1

    try:
        ticker_eth = client.get_ticker("ETHUSD")
        pair_key = list(ticker_eth.keys())[0]
        last_px = ticker_eth[pair_key]["c"][0]
        _ok(f"Ticker ETHUSD: ${float(last_px):,.2f}")
        passed += 1
    except Exception as e:
        _fail("Ticker ETHUSD", str(e))
        failed += 1

    try:
        trades, cursor = client.get_trades("XBTUSD", count=5)
        assert len(trades) > 0, "no trades returned"
        assert len(trades[0]) >= 7, f"unexpected trade schema: {len(trades[0])} fields"
        side = "buy" if trades[0][3] == "b" else "sell"
        _ok(f"Trades XBTUSD: {len(trades)} trades, cursor={cursor[:20]}...")
        _ok(f"  Trade schema: px={trades[0][0]} qty={trades[0][1]} side={side}")
        passed += 1
    except Exception as e:
        _fail("Trades", str(e))
        failed += 1

    try:
        ohlc = client.get_ohlc("XBTUSD", interval=1)
        assert len(ohlc) > 0, "no candles"
        _ok(f"OHLC XBTUSD 1m: {len(ohlc)} candles")
        passed += 1
    except Exception as e:
        _fail("OHLC", str(e))
        failed += 1

    try:
        pairs = client.get_asset_pairs("XBTUSD,ETHUSD")
        for name, info in pairs.items():
            _ok(f"AssetPair {name}: min={info.get('ordermin','?')} fee={info['fees'][0]}")
        passed += 1
    except Exception as e:
        _fail("AssetPairs", str(e))
        failed += 1

    # ── Private endpoints ───────────────────────────────────
    print("\n--- Private Endpoints (authenticated) ---")

    if not client.api_key or not client.api_secret:
        print("  [SKIP] No API key/secret configured")
    else:
        try:
            balance = client.get_balance()
            usd = float(balance.get("ZUSD", 0))
            _ok(f"Balance: ${usd:.2f} USD")
            for asset, amount in balance.items():
                if asset != "ZUSD" and float(amount) > 0:
                    _ok(f"  {asset}: {amount}")
            passed += 1
        except Exception as e:
            _fail("Balance", str(e))
            failed += 1

        try:
            orders = client.get_open_orders()
            open_count = len(orders.get("open", {}))
            _ok(f"OpenOrders: {open_count} open")
            passed += 1
        except Exception as e:
            _fail("OpenOrders", str(e))
            failed += 1

        try:
            history = client.get_trade_history()
            trade_count = history.get("count", 0)
            _ok(f"TradeHistory: {trade_count} historical trades")
            passed += 1
        except Exception as e:
            _fail("TradeHistory", str(e))
            failed += 1

    # ── Summary ─────────────────────────────────────────────
    print("\n" + "=" * 60)
    total = passed + failed
    print(f"RESULT: {passed}/{total} passed, {failed} failed")
    if failed == 0:
        print("ALL TESTS PASSED — Kraken integration ready")
    else:
        print("SOME TESTS FAILED — check errors above")
    print("=" * 60)

    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()