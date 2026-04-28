"""Pre-RTH probe: verify TWS API is alive AND data farms are healthy.

Designed to catch failure mode #10 (TWS overnight server reset) which manifests
as: TCP socket open + API connect succeeds + accountSummary() times out + every
data farm reports broken. This script distinguishes the "looks fine" state from
the "actually working" state.

Exit codes:
  0 — TWS healthy (data farms alive, account summary returned, NetLiq > 0)
  1 — TWS API unreachable (TWS down or wrong port)
  2 — TWS connected but degraded (data farms dead — the overnight-reset state)
  3 — Unexpected error during probe

Use:
    # Ad-hoc:
    python -m ops.tws_health_probe

    # Hourly via managed_truth_loop (already wired)
    # Pre-market via scheduled task ArgusPreMarketTwsCheck (recommended addition)

Output:
    Writes argus_flow/logs/tws_health.json with timestamp + status + reason.
    Dashboard /api/tws_health reads this file.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_PATH = REPO / "argus_flow" / "logs" / "tws_health.json"

PROBE_CLIENT_ID = 188  # outside the runner range, won't collide
CONNECT_TIMEOUT_S = 10
ACCOUNT_QUERY_TIMEOUT_S = 8


def write_status(status: str, exit_code: int, reason: str, **extra) -> None:
    """Snapshot the probe result for dashboard consumption."""
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,           # "healthy" | "unreachable" | "degraded" | "error"
        "exit_code": exit_code,
        "reason": reason,
        **extra,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> int:
    port = int(os.getenv("IBKR_PORT", "7497"))
    host = os.getenv("IBKR_HOST", "127.0.0.1")

    # Lazy import — ib_insync should always be available, but be defensive
    try:
        from ib_insync import IB
    except Exception as e:
        write_status("error", 3, f"ib_insync import failed: {e}")
        print(f"FAIL: ib_insync import failed: {e}")
        return 3

    ib = IB()

    # ── Step 1: TCP + API connect ──
    try:
        ib.connect(host, port, clientId=PROBE_CLIENT_ID, timeout=CONNECT_TIMEOUT_S)
    except Exception as e:
        msg = f"cannot connect to TWS API at {host}:{port}: {e}"
        write_status("unreachable", 1, msg)
        print(f"FAIL: {msg}")
        return 1

    try:
        # ── Step 2: query account summary (THE data-farms canary) ──
        # accountSummary returns near-instantly when farms are healthy,
        # times out (caught upstream by ib_insync RequestTimeout) when dead.
        try:
            summary = ib.accountSummary()
        except Exception as e:
            msg = f"accountSummary() failed (data farms likely dead): {e}"
            write_status("degraded", 2, msg)
            print(f"FAIL: {msg}")
            return 2

        if not summary:
            write_status("degraded", 2, "accountSummary returned empty (data farms dead)")
            print("FAIL: accountSummary returned empty (data farms dead)")
            return 2

        netliq_tag = next((t for t in summary if t.tag == "NetLiquidation"), None)
        if not netliq_tag:
            write_status("degraded", 2, "no NetLiquidation in summary (data farms dead)")
            print("FAIL: no NetLiquidation in summary (data farms dead)")
            return 2

        try:
            netliq = float(netliq_tag.value)
        except Exception:
            netliq = 0.0

        if netliq <= 0:
            write_status("degraded", 2, f"NetLiquidation={netliq} (data farms dead or account unfunded)",
                         netliq_usd=netliq)
            print(f"FAIL: NetLiquidation=${netliq} (data farms dead or account unfunded)")
            return 2

        # ── Step 3: positions query (secondary data-farm sanity check) ──
        try:
            positions = ib.positions()
            n_positions = len(positions)
        except Exception as e:
            # Connected + summary works but positions failed — partial degrade.
            # Still treat as degraded — half-working is unsafe to trade on.
            msg = f"positions() failed despite NetLiq OK: {e}"
            write_status("degraded", 2, msg, netliq_usd=netliq)
            print(f"FAIL: {msg}")
            return 2

        # ── HEALTHY ──
        write_status(
            "healthy", 0,
            f"NetLiq=${netliq:.2f}, {n_positions} open positions",
            netliq_usd=netliq, positions=n_positions,
        )
        print(f"OK: NetLiq=${netliq:,.2f}, {n_positions} open positions")
        return 0

    except Exception as e:
        write_status("error", 3, f"unexpected probe error: {e}")
        print(f"FAIL: unexpected error: {e}")
        return 3
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
