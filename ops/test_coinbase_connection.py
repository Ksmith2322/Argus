#!/usr/bin/env python3
# ops/test_coinbase_connection.py -- Coinbase Advanced Trade API connectivity validator.
#
# Tests real API connectivity WITHOUT placing any orders.
# Supports both coinbase.com Advanced Trade keys (HMAC) and CDP portal keys (JWT).
#
# Setup for coinbase.com keys (recommended -- create at coinbase.com/settings/api):
#   Create C:/Argus/repo/.env.coinbase with:
#     CB_API_KEY=your-api-key-id
#     CB_API_SECRET=your-api-secret
#
# Setup for CDP portal keys (portal.cdp.coinbase.com):
#   Create C:/Argus/repo/.env.coinbase with:
#     CB_API_KEY_NAME=organizations/{org_id}/apiKeys/{key_id}
#     CB_API_PRIVATE_KEY=-----BEGIN EC PRIVATE KEY-----\n...\n-----END EC PRIVATE KEY-----\n
#
# Run: python ops/test_coinbase_connection.py
#
# Exit codes: 0 = all checks passed, 1 = failure

from __future__ import annotations

import hashlib
import hmac
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENV_FILE = REPO / ".env.coinbase"
CB_REST_BASE = "https://api.coinbase.com"


# ---------------------------------------------------------------------------
# Env loader
# ---------------------------------------------------------------------------

def _load_env_file(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def _get_creds() -> dict:
    ev = _load_env_file(ENV_FILE)

    # HMAC style (coinbase.com Advanced Trade keys)
    api_key = os.environ.get("CB_API_KEY") or ev.get("CB_API_KEY", "")
    api_secret = os.environ.get("CB_API_SECRET") or ev.get("CB_API_SECRET", "")

    # CDP JWT style (portal.cdp.coinbase.com keys)
    key_name = os.environ.get("CB_API_KEY_NAME") or ev.get("CB_API_KEY_NAME", "")
    private_key = os.environ.get("CB_API_PRIVATE_KEY") or ev.get("CB_API_PRIVATE_KEY", "")
    if private_key:
        private_key = private_key.replace("\\n", "\n")

    if api_key and api_secret:
        return {"mode": "hmac", "api_key": api_key, "api_secret": api_secret}
    if key_name and private_key:
        return {"mode": "jwt", "key_name": key_name, "private_key": private_key}

    print("[ERROR] No credentials found in .env.coinbase")
    print()
    print("  For coinbase.com keys, add:")
    print("    CB_API_KEY=your-key-id")
    print("    CB_API_SECRET=your-secret")
    print()
    print("  For CDP portal keys, add:")
    print("    CB_API_KEY_NAME=organizations/.../apiKeys/...")
    print("    CB_API_PRIVATE_KEY=-----BEGIN EC PRIVATE KEY-----\\n...\\n-----END EC PRIVATE KEY-----\\n")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Auth headers
# ---------------------------------------------------------------------------

def _hmac_headers(api_key: str, api_secret: str, method: str, path: str, body: str = "") -> dict:
    ts = str(int(time.time()))
    msg = ts + method.upper() + path + body
    sig = hmac.new(api_secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "CB-ACCESS-KEY": api_key,
        "CB-ACCESS-SIGN": sig,
        "CB-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }


def _jwt_headers(key_name: str, private_key_pem: str, method: str, path: str) -> dict:
    try:
        import jwt as pyjwt
        import secrets
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
    except ImportError:
        print("[ERROR] pip install PyJWT cryptography")
        sys.exit(1)

    uri = f"{method} {CB_REST_BASE.replace('https://', '')}{path}"
    pk = load_pem_private_key(private_key_pem.encode(), password=None)
    algorithm = "EdDSA" if "Ed25519" in type(pk).__name__ else "ES256"
    token = pyjwt.encode(
        {"sub": key_name, "iss": "cdp", "nbf": int(time.time()), "exp": int(time.time()) + 120, "uri": uri},
        pk, algorithm=algorithm,
        headers={"kid": key_name, "nonce": secrets.token_hex(16)},
    )
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _get(path: str, creds: dict) -> dict:
    try:
        import requests
    except ImportError:
        print("[ERROR] pip install requests")
        sys.exit(1)

    if creds["mode"] == "hmac":
        headers = _hmac_headers(creds["api_key"], creds["api_secret"], "GET", path)
    else:
        headers = _jwt_headers(creds["key_name"], creds["private_key"], "GET", path)

    resp = requests.get(f"{CB_REST_BASE}{path}", headers=headers, timeout=10)
    return {"status": resp.status_code, "body": resp.json() if resp.text else {}}


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_accounts(creds: dict) -> bool:
    print("  Fetching account balances...")
    result = _get("/api/v3/brokerage/accounts", creds)
    if result["status"] != 200:
        print(f"  [FAIL] HTTP {result['status']}: {result['body']}")
        return False

    accounts = result["body"].get("accounts", [])
    if not accounts:
        print("  [WARN] No accounts returned (account may be new/empty)")
        return True

    print(f"  [OK] {len(accounts)} account(s) found:")
    total_usd = 0.0
    for acct in accounts:
        currency = acct.get("currency", "?")
        available = float(acct.get("available_balance", {}).get("value", "0"))
        hold = float(acct.get("hold", {}).get("value", "0"))
        if available > 0 or hold > 0:
            print(f"       {currency:<8} available={available:.4f}  hold={hold:.4f}")
        if currency == "USD":
            total_usd = available + hold

    if total_usd > 0:
        print(f"  USD total (avail+hold): ${total_usd:.2f}")
    return True


def check_product(product_id: str, creds: dict) -> bool:
    print(f"  Fetching {product_id} quote...")
    result = _get(f"/api/v3/brokerage/products/{product_id}", creds)
    if result["status"] != 200:
        print(f"  [FAIL] HTTP {result['status']}: {result['body']}")
        return False

    body = result["body"]
    price = body.get("price", "N/A")
    bid = body.get("best_bid", "N/A")
    ask = body.get("best_ask", "N/A")
    status = body.get("status", "?")
    disabled = body.get("trading_disabled", None)
    print(f"  [OK] {product_id}: price={price}  bid={bid}  ask={ask}  status={status}  trading_disabled={disabled}")
    if disabled:
        print(f"  [WARN] {product_id} trading is disabled on this account!")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    sep = "=" * 55
    print(sep)
    print("  Argus -- Coinbase Advanced Trade Connectivity Test")
    print(sep)
    print()

    if not ENV_FILE.exists():
        print(f"[INFO] No credentials file found at:")
        print(f"         {ENV_FILE}")
        print()
        print("  Create it with your coinbase.com Advanced Trade API key:")
        print("    CB_API_KEY=your-key-id")
        print("    CB_API_SECRET=your-secret")
        print()
        print("  Get these from: coinbase.com -> Profile -> API -> Create API Key")
        print("  Permissions needed: View + Trade")
        print()
        return 1

    creds = _get_creds()
    mode_label = "HMAC (coinbase.com)" if creds["mode"] == "hmac" else "JWT (CDP portal)"
    key_display = creds.get("api_key", creds.get("key_name", ""))[:40]
    print(f"  Auth mode: {mode_label}")
    print(f"  Key: {key_display}...")
    print()

    all_ok = True

    print("[1/3] Account balances")
    if not check_accounts(creds):
        all_ok = False
    print()

    print("[2/3] ETH-USD product info")
    if not check_product("ETH-USD", creds):
        all_ok = False
    print()

    print("[3/3] BTC-USD product info")
    if not check_product("BTC-USD", creds):
        all_ok = False
    print()

    print(sep)
    if all_ok:
        print("  RESULT: ALL CHECKS PASSED -- API connectivity confirmed")
        print()
        print("  Next steps for live trading (Phase 21):")
        print("    1. Verify USD balance >= $200 in account")
        print("    2. Key has 'trade' permission -- ready for Phase 21 adapter build")
    else:
        print("  RESULT: ONE OR MORE CHECKS FAILED -- see errors above")
    print(sep)
    print()

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())