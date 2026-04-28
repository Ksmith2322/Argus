"""Fleet-wide broker equity drift detector.

Compares broker NetLiquidation against the fleet's internal expected equity
(anchor + sum of closed-trade PnL since last reset). If they diverge by >N%
for >M consecutive minutes, alerts and optionally sets HALT.flag.

This generalizes the per-pair RECON_DRIFT we already have in argus to the
fleet level. Catches the case where a runner closed a trade locally without
the broker actually processing it (or vice versa).

Wired into managed_truth_loop hourly. State in
argus_flow/logs/_risk/broker_drift_state.json.

Per project_risk_guardrails_real_money_20260501.md:
   tolerance: 1% divergence
   sustained: 60 minutes
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATE_PATH = REPO / "argus_flow" / "logs" / "_risk" / "broker_drift_state.json"
HALT_PATH = REPO / "argus_flow" / "logs" / "HALT.flag"

DRIFT_TOLERANCE_PCT = 1.0    # >1% divergence is concerning
SUSTAINED_MINUTES = 60       # ...for 60 min triggers action

PROBE_CLIENT_ID = 184


def _read_broker_equity() -> float | None:
    try:
        from ib_insync import IB
        port = int(os.getenv("IBKR_PORT", "7497"))
        ib = IB()
        ib.connect("127.0.0.1", port, clientId=PROBE_CLIENT_ID, timeout=8)
        try:
            summary = ib.accountSummary()
            netliq_tag = next((t for t in summary if t.tag == "NetLiquidation"), None)
            return float(netliq_tag.value) if netliq_tag else None
        finally:
            try: ib.disconnect()
            except: pass
    except Exception as e:
        print(f"  warn: broker equity read failed: {e}")
        return None


def _expected_equity_from_fills(reset_anchor: float, since_ts: datetime) -> tuple[float, int]:
    """Sum closed-trade PnL since last reset.
    Returns (expected_equity, n_trades_counted)."""
    fills = REPO / "argus_flow" / "logs" / "canonical_fills.jsonl"
    if not fills.exists():
        return reset_anchor, 0
    pnl_sum = 0.0
    n = 0
    try:
        with fills.open(encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("side") != "EXIT" or r.get("source") == "backfill_from_trade_csv":
                    continue
                try:
                    ts = datetime.fromisoformat(str(r.get("exit_ts","")).replace("Z","+00:00"))
                    if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
                    if ts < since_ts: continue
                    pnl_sum += float(r.get("pnl_usd") or 0)
                    n += 1
                except Exception:
                    continue
    except Exception as e:
        print(f"  warn: fills read failed: {e}")
    return reset_anchor + pnl_sum, n


def _load_state() -> dict:
    if STATE_PATH.exists():
        try: return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except: pass
    return {}


def _save_state(d: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(d, indent=2, default=str), encoding="utf-8")


def main() -> int:
    now = datetime.now(timezone.utc)
    broker_eq = _read_broker_equity()
    if broker_eq is None or broker_eq <= 0:
        print(f"FAIL: broker equity unavailable")
        return 1

    state = _load_state()

    # Reset-anchor: use the value from circuit_breaker_state's day_open if available
    cb_state_path = REPO / "argus_flow" / "logs" / "_risk" / "circuit_breaker_state.json"
    reset_anchor = broker_eq  # fallback
    since_ts = now - timedelta(days=1)  # fallback
    if cb_state_path.exists():
        try:
            cb = json.loads(cb_state_path.read_text(encoding="utf-8"))
            reset_anchor = float(cb.get("day_open_equity_usd", broker_eq))
            since_ts = datetime.fromisoformat(cb["first_probe_ts"].replace("Z","+00:00"))
        except Exception:
            pass

    expected, n_counted = _expected_equity_from_fills(reset_anchor, since_ts)
    divergence_usd = broker_eq - expected
    divergence_pct = (divergence_usd / broker_eq * 100) if broker_eq else 0
    abs_pct = abs(divergence_pct)

    # Track when the divergence first crossed the threshold
    if abs_pct > DRIFT_TOLERANCE_PCT:
        if "first_breach_ts" not in state:
            state["first_breach_ts"] = now.isoformat()
        try:
            first_ts = datetime.fromisoformat(state["first_breach_ts"].replace("Z","+00:00"))
            sustained_min = (now - first_ts).total_seconds() / 60.0
        except Exception:
            sustained_min = 0
    else:
        # Cleared — reset
        state.pop("first_breach_ts", None)
        sustained_min = 0

    # Action: if sustained > threshold AND HALT not already set, set it
    actions = []
    if sustained_min >= SUSTAINED_MINUTES:
        if not HALT_PATH.exists():
            HALT_PATH.write_text(
                f"auto-halt: broker drift {divergence_pct:+.2f}% sustained {sustained_min:.0f} min "
                f"(broker ${broker_eq:.2f} vs expected ${expected:.2f}, {n_counted} trades counted)",
                encoding="utf-8",
            )
            actions.append("HALT.flag set (drift sustained)")
            # Discord alert
            try:
                sys.path.insert(0, str(REPO))
                from ops._alert_helper import post_discord, load_cooldown_state, should_alert, mark_alerted, save_cooldown_state
                cd = load_cooldown_state("broker_drift_cooldown")
                if should_alert(cd, "drift_breach", 240):  # 4hr cooldown
                    post_discord(
                        "BROKER DRIFT ALERT",
                        f"Divergence: **{divergence_pct:+.2f}%** sustained {sustained_min:.0f} min\n"
                        f"Broker NetLiq: ${broker_eq:,.2f}\n"
                        f"Expected (anchor + closed PnL): ${expected:,.2f}\n"
                        f"Diff: ${divergence_usd:+,.2f} over {n_counted} trades since {since_ts.isoformat()}\n"
                        f"HALT.flag set automatically.",
                        color=0xFF4444,
                    )
                    mark_alerted(cd, "drift_breach")
                    save_cooldown_state("broker_drift_cooldown", cd)
            except Exception as e:
                print(f"  warn: Discord alert failed: {e}", file=sys.stderr)

    state.update({
        "ts": now.isoformat(),
        "broker_equity_usd": broker_eq,
        "expected_equity_usd": expected,
        "reset_anchor_usd": reset_anchor,
        "since_ts": since_ts.isoformat(),
        "n_trades_counted": n_counted,
        "divergence_usd": round(divergence_usd, 2),
        "divergence_pct": round(divergence_pct, 3),
        "tolerance_pct": DRIFT_TOLERANCE_PCT,
        "sustained_minutes": round(sustained_min, 1),
        "tripped": sustained_min >= SUSTAINED_MINUTES,
        "last_actions": actions,
    })
    _save_state(state)

    status = "OK"
    if abs_pct > DRIFT_TOLERANCE_PCT:
        status = "BREACH" if sustained_min < SUSTAINED_MINUTES else "TRIPPED"
    print(f"OK: broker=${broker_eq:.2f} expected=${expected:.2f} diff={divergence_pct:+.3f}% sustained={sustained_min:.0f}min status={status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
