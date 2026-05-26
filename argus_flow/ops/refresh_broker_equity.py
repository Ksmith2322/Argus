"""Refresh broker_truth.account_equity_usd in risk_oversight_report.json
by querying IBKR directly.

WHY THIS EXISTS
===============
`argus_flow.ops.risk_oversight` populates broker_truth from per-runner
state files (`broker_truth/load_runner_broker_state`). When a runner
hasn't yet connected and written its state (fresh post-Gateway-restart),
those files report account_equity_usd=0.0. helio.fleet_sizing then
sees equity=0, treats it as "broker unavailable", and raises
BrokerEquityUnavailableError on the next sizing call. This was the
2026-05-26 morning failure mode for forge_gld_pm_long.

This module connects to IBKR directly via ib_insync, reads
NetLiquidation from accountSummary, and writes it back to
risk_oversight_report.json -- preserving every other field. Designed
to run AFTER risk_oversight in refresh_managed_truth so the per-runner
view (correlation exposure, fleet open risk, per-pair status) is
preserved while the equity field gets the canonical broker value.

Fail-open: if IBKR isn't reachable, leaves the file untouched and
exits with code 0 -- the caller doesn't want this to mark the whole
truth-refresh chain as failed.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
REPORT_PATH = _REPO / "argus_flow" / "logs" / "risk_oversight_report.json"


def _fetch_once(port: int, client_id: int) -> float | None:
    try:
        from helio import ibkr_execution as ibkr
    except ImportError:
        return None
    try:
        ib = ibkr.connect(client_id=client_id)
    except Exception:
        return None
    try:
        accts = ib.managedAccounts()
        if not accts:
            return None
        summary = ib.accountSummary(account=accts[0])
        for v in summary:
            if v.tag == "NetLiquidation" and v.currency == "USD":
                return float(v.value)
        return None
    except Exception:
        return None
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


def fetch_net_liquidation_usd(*, port: int = 4002, client_id: int = 998,
                              attempts: int = 3, backoff_s: float = 2.0) -> float | None:
    """Connect to IBKR (Gateway 4002 paper by default), fetch NetLiquidation
    in USD, return the value. Retries up to `attempts` times with
    `backoff_s * attempt` linear backoff -- this exists because when called
    from refresh_managed_truth the previous module (position_monitor)
    may not have released its IB connection yet, and Gateway will reject
    rapid back-to-back connects from the same process tree. Returns None
    on every-attempt failure; caller treats None as 'leave cache untouched'."""
    import time
    last = None
    for i in range(1, max(1, attempts) + 1):
        last = _fetch_once(port, client_id)
        if last is not None and last > 0:
            return last
        if i < attempts:
            time.sleep(backoff_s * i)
    return last


def patch_report(equity_usd: float, *, path: Path = REPORT_PATH) -> bool:
    """Patch the broker_truth.account_equity_usd field. Returns True on
    success, False if the report file is missing/unreadable."""
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    data.setdefault("broker_truth", {})
    data["broker_truth"]["account_equity_usd"] = round(equity_usd, 2)
    sources = list(data["broker_truth"].get("sources") or [])
    if "ibkr_direct" not in sources:
        sources = ["ibkr_direct"] + sources
    data["broker_truth"]["sources"] = sources
    data["broker_truth"]["last_ibkr_refresh"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=4002,
                        help="IBKR port (4002=Gateway paper, 7497=TWS paper)")
    parser.add_argument("--client-id", type=int, default=998,
                        help="IBKR client_id reserved for ops")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    eq = fetch_net_liquidation_usd(port=args.port, client_id=args.client_id)
    if eq is None:
        if not args.quiet:
            print("refresh_broker_equity: broker unreachable or NetLiquidation absent; leaving cache untouched")
        return 0
    if eq <= 0:
        if not args.quiet:
            print(f"refresh_broker_equity: got non-positive NetLiquidation={eq}; refusing to patch")
        return 0
    ok = patch_report(eq)
    if not args.quiet:
        if ok:
            print(f"refresh_broker_equity: patched account_equity_usd={eq:.2f}")
        else:
            print(f"refresh_broker_equity: failed to patch {REPORT_PATH}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
