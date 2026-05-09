"""Real-money mismatch detector.

Polls broker positions and recent executions, then flags any real-account
position that cannot be tied to an allowed Argus real-money order tag. The
default CLI mode is dry-run/read-only; use ``--halt-on-violation`` to write
``HALT.flag`` on a violation.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from helio.real_money import (
    Allowlist,
    REAL_PORT,
    load_allowlist,
)

REPO = Path(__file__).resolve().parents[1]
STATE_PATH = REPO / "argus_flow" / "logs" / "_risk" / "real_money_mismatch_state.json"
HALT_PATH = REPO / "argus_flow" / "logs" / "HALT.flag"
DEFAULT_CLIENT_ID = 188


@dataclass
class PositionSnapshot:
    account: str
    symbol: str
    quantity: float
    avg_cost: float
    order_ref: str = ""
    strategy: str = ""


@dataclass
class MismatchViolation:
    code: str
    severity: str
    account: str
    symbol: str
    quantity: float
    order_ref: str
    detail: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strategy_from_order_ref(order_ref: str, allowlist: Allowlist | None = None) -> str:
    prefix = "argus-real-"
    if not order_ref.startswith(prefix):
        return ""
    if allowlist is not None:
        for strategy in sorted(allowlist.strategies, key=len, reverse=True):
            if order_ref.startswith(f"{prefix}{strategy}-"):
                return strategy
    rest = order_ref[len(prefix):]
    if "-" not in rest:
        return rest
    return rest.rsplit("-", 1)[0]


def _recent_order_refs_by_symbol(ib) -> dict[str, str]:
    """Best-effort symbol -> latest orderRef map from current-session trades.

    IBKR positions do not carry orderRef, so this is necessarily a
    current-session helper. If there is a real position and no tag can be found,
    the detector fails closed with a TAG_MISSING violation.
    """
    out: dict[str, str] = {}
    try:
        trades = list(ib.trades() or [])
    except Exception:
        trades = []
    for trade in trades:
        contract = getattr(trade, "contract", None)
        order = getattr(trade, "order", None)
        symbol = str(getattr(contract, "symbol", "") or "")
        order_ref = str(getattr(order, "orderRef", "") or "")
        if symbol and order_ref:
            out[symbol] = order_ref
    return out


def scan_ibkr(ib, allowlist: Allowlist) -> tuple[list[PositionSnapshot], list[MismatchViolation]]:
    order_refs = _recent_order_refs_by_symbol(ib)
    positions: list[PositionSnapshot] = []
    violations: list[MismatchViolation] = []
    try:
        broker_positions = list(ib.positions() or [])
    except Exception as exc:
        return [], [
            MismatchViolation(
                code="BROKER_POSITION_READ_FAILED",
                severity="BLOCKER",
                account="",
                symbol="",
                quantity=0,
                order_ref="",
                detail=f"Could not read broker positions: {exc}",
            )
        ]

    for pos in broker_positions:
        qty = float(getattr(pos, "position", 0) or 0)
        if qty == 0:
            continue
        contract = getattr(pos, "contract", None)
        symbol = str(getattr(contract, "symbol", "") or "")
        account = str(getattr(pos, "account", "") or "")
        order_ref = order_refs.get(symbol, "")
        strategy = _strategy_from_order_ref(order_ref, allowlist)
        snap = PositionSnapshot(
            account=account,
            symbol=symbol,
            quantity=qty,
            avg_cost=float(getattr(pos, "avgCost", 0) or 0),
            order_ref=order_ref,
            strategy=strategy,
        )
        positions.append(snap)

        if not order_ref:
            violations.append(MismatchViolation(
                code="REAL_POSITION_TAG_MISSING",
                severity="BLOCKER",
                account=account,
                symbol=symbol,
                quantity=qty,
                order_ref=order_ref,
                detail="Open broker position has no current-session Argus real-money orderRef tag.",
            ))
            continue
        if not order_ref.startswith("argus-real-"):
            violations.append(MismatchViolation(
                code="REAL_POSITION_TAG_MALFORMED",
                severity="BLOCKER",
                account=account,
                symbol=symbol,
                quantity=qty,
                order_ref=order_ref,
                detail="Open broker position orderRef is not an Argus real-money tag.",
            ))
            continue
        if not allowlist.is_real_money_strategy(strategy):
            violations.append(MismatchViolation(
                code="REAL_POSITION_STRATEGY_NOT_ALLOWLISTED",
                severity="BLOCKER",
                account=account,
                symbol=symbol,
                quantity=qty,
                order_ref=order_ref,
                detail=f"Strategy {strategy!r} is not currently real-money allowlisted.",
            ))

    return positions, violations


def write_state(payload: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def maybe_halt(violations: list[MismatchViolation], *, enabled: bool) -> bool:
    if not enabled or not violations:
        return False
    text = (
        f"auto-halt: real-money mismatch detector found {len(violations)} violation(s) "
        f"at {_now()}: " + "; ".join(f"{v.code}:{v.symbol}:{v.quantity}" for v in violations[:5])
    )
    HALT_PATH.write_text(text, encoding="utf-8")
    return True


def run_once(*, host: str, port: int, client_id: int, halt_on_violation: bool) -> dict[str, Any]:
    allowlist = load_allowlist()
    try:
        from ib_insync import IB
    except Exception as exc:
        payload = {
            "ts": _now(),
            "status": "ERROR",
            "error": f"ib_insync import failed: {exc}",
            "positions": [],
            "violations": [],
            "halt_written": False,
        }
        write_state(payload)
        return payload

    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=10)
        positions, violations = scan_ibkr(ib, allowlist)
    except Exception as exc:
        positions = []
        violations = [
            MismatchViolation(
                code="BROKER_CONNECT_OR_SCAN_FAILED",
                severity="BLOCKER",
                account="",
                symbol="",
                quantity=0,
                order_ref="",
                detail=str(exc),
            )
        ]
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass

    halt_written = maybe_halt(violations, enabled=halt_on_violation)
    payload = {
        "ts": _now(),
        "status": "VIOLATION" if violations else "OK",
        "dry_run": not halt_on_violation,
        "host": host,
        "port": port,
        "client_id": client_id,
        "positions": [asdict(p) for p in positions],
        "violations": [asdict(v) for v in violations],
        "halt_written": halt_written,
    }
    write_state(payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Real-money broker-position mismatch detector.")
    parser.add_argument("--host", default=os.getenv("IBKR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("IBKR_REAL_PORT", str(REAL_PORT))))
    parser.add_argument("--client-id", type=int, default=DEFAULT_CLIENT_ID)
    parser.add_argument("--interval-s", type=int, default=300)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--halt-on-violation", action="store_true")
    args = parser.parse_args(argv)

    while True:
        payload = run_once(
            host=args.host,
            port=args.port,
            client_id=args.client_id,
            halt_on_violation=args.halt_on_violation,
        )
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        if args.once:
            return 2 if payload["violations"] else 0
        time.sleep(args.interval_s)


if __name__ == "__main__":
    raise SystemExit(main())
