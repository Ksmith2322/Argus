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


def _read_broker_equity_and_unrealized() -> tuple[float | None, float]:
    """Returns (NetLiquidation, sum_unrealized_pnl_open_positions).

    NetLiquidation already includes mark-to-market on open positions, so for the
    drift formula we need the matching open-unrealized adjustment on the
    expected side. Without this, profitable open trades trip the drift halt
    even when local state and broker are in perfect agreement
    (false-positive observed 2026-05-08: SPY+GLD+UVXY together produced
    +$1,086 unrealized that wasn't in the realized-only expected formula).
    """
    try:
        from ib_insync import IB
        port = int(os.getenv("IBKR_PORT", "7497"))
        ib = IB()
        ib.connect("127.0.0.1", port, clientId=PROBE_CLIENT_ID, timeout=8)
        try:
            summary = ib.accountSummary()
            netliq_tag = next((t for t in summary if t.tag == "NetLiquidation"), None)
            netliq = float(netliq_tag.value) if netliq_tag else None
            try:
                portfolio = ib.portfolio()
                unrealized = sum(float(p.unrealizedPNL or 0) for p in portfolio)
            except Exception as e:
                print(f"  warn: portfolio read failed (treating unrealized=0): {e}")
                unrealized = 0.0
            return netliq, unrealized
        finally:
            try: ib.disconnect()
            except: pass
    except Exception as e:
        print(f"  warn: broker equity read failed: {e}")
        return None, 0.0


def _read_broker_equity() -> float | None:
    """Backwards-compat shim. Prefer _read_broker_equity_and_unrealized()."""
    netliq, _ = _read_broker_equity_and_unrealized()
    return netliq


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


def _realized_pnl_since(since_ts: datetime) -> float:
    """Sum of closed-trade realized PnL from canonical_fills since ``since_ts``.

    Thin wrapper around ``_expected_equity_from_fills`` with a zero anchor.
    Exists so the epoch-anchor divergence math in ``main`` can read realized
    PnL by itself (the anchor is persistent, not today's day-open).
    """
    expected, _ = _expected_equity_from_fills(0.0, since_ts)
    return expected


def _load_state() -> dict:
    if STATE_PATH.exists():
        try: return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except: pass
    return {}


def _save_state(d: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(d, indent=2, default=str), encoding="utf-8")


def compute_divergence(
    state: dict,
    broker_eq: float,
    open_unrealized: float,
    realized_pnl_since_anchor: float,
    n_trades_counted: int,
    now: datetime,
) -> dict:
    """Pure divergence math, lifted out of ``main`` for unit testing.

    Inputs are the raw numbers; the function decides whether to initialize
    the epoch anchor, then computes ``expected``, ``divergence``, and the
    sustained-minutes breach tracker. Returns a structured outcome with all
    fields the writer needs.

    Mutates ``state`` only to add ``epoch_anchor_usd`` / ``epoch_anchor_ts``
    on first run; everything else is returned in the result dict for the
    caller to merge.
    """
    epoch_anchor = state.get("epoch_anchor_usd")
    epoch_anchor_ts_str = state.get("epoch_anchor_ts")
    epoch_initialized_this_call = False
    if epoch_anchor is None or epoch_anchor_ts_str is None:
        epoch_anchor = float(broker_eq - open_unrealized)
        epoch_anchor_ts = now
        epoch_anchor_ts_str = epoch_anchor_ts.isoformat()
        state["epoch_anchor_usd"] = epoch_anchor
        state["epoch_anchor_ts"] = epoch_anchor_ts_str
        epoch_initialized_this_call = True
    else:
        epoch_anchor = float(epoch_anchor)
        try:
            epoch_anchor_ts = datetime.fromisoformat(epoch_anchor_ts_str.replace("Z", "+00:00"))
            if epoch_anchor_ts.tzinfo is None:
                epoch_anchor_ts = epoch_anchor_ts.replace(tzinfo=timezone.utc)
        except Exception:
            epoch_anchor_ts = now

    expected_realized = epoch_anchor + float(realized_pnl_since_anchor)
    expected = expected_realized + float(open_unrealized)
    divergence_usd = float(broker_eq) - expected
    divergence_pct = (divergence_usd / broker_eq * 100) if broker_eq else 0.0
    abs_pct = abs(divergence_pct)

    if abs_pct > DRIFT_TOLERANCE_PCT:
        if "first_breach_ts" not in state:
            state["first_breach_ts"] = now.isoformat()
        try:
            first_ts = datetime.fromisoformat(state["first_breach_ts"].replace("Z", "+00:00"))
            sustained_min = (now - first_ts).total_seconds() / 60.0
        except Exception:
            sustained_min = 0.0
    else:
        state.pop("first_breach_ts", None)
        sustained_min = 0.0

    return {
        "epoch_anchor_usd": epoch_anchor,
        "epoch_anchor_ts": epoch_anchor_ts_str,
        "epoch_initialized_this_call": epoch_initialized_this_call,
        "expected_realized": expected_realized,
        "expected": expected,
        "divergence_usd": divergence_usd,
        "divergence_pct": divergence_pct,
        "sustained_minutes": sustained_min,
        "tripped": sustained_min >= SUSTAINED_MINUTES,
        "n_trades_counted": n_trades_counted,
    }


def main() -> int:
    now = datetime.now(timezone.utc)
    broker_eq, open_unrealized = _read_broker_equity_and_unrealized()
    if broker_eq is None or broker_eq <= 0:
        print(f"FAIL: broker equity unavailable")
        return 1

    state = _load_state()

    # Bug history (fixed 2026-05-11): pre-fix, this used today's
    # day_open_equity_usd as the anchor and summed realized PnL since today's
    # first_probe_ts. But today's day_open already contains prior-days'
    # realized PnL, so adding open_unrealized double-counted everything from
    # since_ts back to the paper-reset epoch. Symptom: 36-hour stuck drift
    # trip on a healthy fleet with profitable open positions.
    #
    # Current model: a persistent epoch anchor representing book value
    # (cash + cost basis) at the time the detector first ran. Initialized
    # once as broker_NetLiq - open_unrealized; never auto-updated. The math
    # in compute_divergence() is then:
    #
    #   expected = epoch_anchor + realized_since_epoch + open_unrealized_now
    #
    # which equals broker_NetLiq by construction whenever the bot's view of
    # the world is consistent. Divergence only opens on orphan fills,
    # manual deposits, or other state-corruption events — which is the
    # ONLY thing this detector is supposed to alarm on.
    bootstrap_anchor = state.get("epoch_anchor_usd") or float(broker_eq - open_unrealized)
    bootstrap_anchor_ts = state.get("epoch_anchor_ts") or now.isoformat()
    try:
        bootstrap_since_ts = datetime.fromisoformat(str(bootstrap_anchor_ts).replace("Z", "+00:00"))
        if bootstrap_since_ts.tzinfo is None:
            bootstrap_since_ts = bootstrap_since_ts.replace(tzinfo=timezone.utc)
    except Exception:
        bootstrap_since_ts = now
    _, n_counted = _expected_equity_from_fills(float(bootstrap_anchor), bootstrap_since_ts)
    realized_pnl_since = _realized_pnl_since(bootstrap_since_ts)

    result = compute_divergence(
        state=state,
        broker_eq=broker_eq,
        open_unrealized=open_unrealized,
        realized_pnl_since_anchor=realized_pnl_since,
        n_trades_counted=n_counted,
        now=now,
    )
    if result["epoch_initialized_this_call"]:
        print(
            f"  epoch_anchor initialized: ${result['epoch_anchor_usd']:,.2f} "
            f"(broker=${broker_eq:,.2f} - open_unrealized=${open_unrealized:,.2f})"
        )

    expected = result["expected"]
    expected_realized = result["expected_realized"]
    divergence_usd = result["divergence_usd"]
    divergence_pct = result["divergence_pct"]
    abs_pct = abs(divergence_pct)
    sustained_min = result["sustained_minutes"]
    since_ts = bootstrap_since_ts
    epoch_anchor = result["epoch_anchor_usd"]
    epoch_anchor_ts_str = result["epoch_anchor_ts"]

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
        "expected_realized_only_usd": expected_realized,
        "open_unrealized_pnl_usd": round(open_unrealized, 2),
        "epoch_anchor_usd": epoch_anchor,
        "epoch_anchor_ts": epoch_anchor_ts_str,
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
