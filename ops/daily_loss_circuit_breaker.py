"""Daily loss circuit breaker — automated kill-switch driven by intraday PnL.

Runs every N minutes via managed_truth_loop. Reads broker equity, computes the
day's PnL %, and triggers HALT/FLATTEN_EOD automatically when thresholds breach.

Tiers (per project_risk_guardrails_real_money_20260501.md):
   -1% intraday : WARN only (Discord alert, no action)
   -2% intraday : touch HALT.flag (refuses new entries; existing positions
                  exit normally via their brackets)
   -4% intraday : touch HALT.flag + FLATTEN_EOD.flag (refuses entries +
                  force-close all positions at next eval boundary)

The day_open_equity baseline resets every UTC day at first probe after midnight.
Stored in argus_flow/logs/_risk/circuit_breaker_state.json.

Recovery: once daily PnL recovers above the threshold (e.g. drops back from
-2.5% to -1.5%), the breaker WILL release HALT.flag automatically — but
FLATTEN_EOD does NOT auto-clear (requires manual review per real-money policy).

Exit codes:
   0 — probe ran, breaker state recorded (whether tripped or not)
   1 — broker unreachable or equity not available
   2 — unexpected error
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATE_PATH = REPO / "argus_flow" / "logs" / "_risk" / "circuit_breaker_state.json"
HALT_PATH = REPO / "argus_flow" / "logs" / "HALT.flag"
FLATTEN_PATH = REPO / "argus_flow" / "logs" / "FLATTEN_EOD.flag"

# Tier thresholds (% of day-open equity, NEGATIVE values).
# 2026-05-07 audit: added hysteresis to prevent oscillation. On 5/7 the breaker
# transitioned 11 times in 6 hours as PnL bounced around the -2% boundary
# (HALT.flag set/cleared/set/cleared cascading). Hysteresis = stricter exit
# threshold than entry. Minimum dwell time = once a tier fires, hold for at
# least N minutes regardless of recovery, so a transient 0.1% rebound can't
# trigger a tier downgrade.
TIER_WARN     = -1.0   # entry: enter WARN at -1%
TIER_PAUSE    = -2.0   # entry: enter PAUSE at -2%
TIER_FLATTEN  = -4.0   # entry: enter FLATTEN at -4%

# Hysteresis: must recover this far above tier entry to demote
TIER_PAUSE_EXIT   = -1.5   # exit PAUSE only when pnl > -1.5% (0.5pp gap from entry)
TIER_FLATTEN_EXIT = -3.0   # exit FLATTEN only when pnl > -3% (1pp gap from entry)

# Minimum dwell time once a tier fires (seconds)
TIER_PAUSE_MIN_DWELL_S    = 30 * 60       # 30 min
TIER_FLATTEN_MIN_DWELL_S  = 4 * 60 * 60   # 4 hours

PROBE_CLIENT_ID = 187


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(d: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(d, indent=2, default=str), encoding="utf-8")


def _read_broker_equity() -> float | None:
    """Pull NetLiquidation from TWS. Returns None if unreachable/unhealthy."""
    try:
        from ib_insync import IB
    except Exception as e:
        print(f"FAIL: ib_insync import: {e}")
        return None
    port = int(os.getenv("IBKR_PORT", "7497"))
    ib = IB()
    try:
        ib.connect("127.0.0.1", port, clientId=PROBE_CLIENT_ID, timeout=8)
    except Exception as e:
        print(f"FAIL: cannot connect to TWS: {e}")
        return None
    try:
        summary = ib.accountSummary()
        netliq_tag = next((t for t in summary if t.tag == "NetLiquidation"), None)
        if not netliq_tag:
            return None
        return float(netliq_tag.value)
    except Exception as e:
        print(f"FAIL: accountSummary: {e}")
        return None
    finally:
        try: ib.disconnect()
        except Exception: pass


def main() -> int:
    today = date.today().isoformat()
    now_utc = datetime.now(timezone.utc).isoformat()

    equity = _read_broker_equity()
    if equity is None or equity <= 0:
        print(f"FAIL: equity unavailable ({equity})")
        return 1

    state = _load_state()
    # Reset baseline on day rollover
    if state.get("day") != today:
        state = {
            "day": today,
            "day_open_equity_usd": equity,
            "first_probe_ts": now_utc,
            "tier_history": [],
        }

    open_eq = state["day_open_equity_usd"]
    pnl_pct = (equity - open_eq) / open_eq * 100.0

    prev_tier = state.get("current_tier", "OK")
    last_tier_change_iso = state.get("last_tier_change_ts", state.get("first_probe_ts", now_utc))
    try:
        last_change = datetime.fromisoformat(str(last_tier_change_iso).replace("Z", "+00:00"))
        if last_change.tzinfo is None:
            last_change = last_change.replace(tzinfo=timezone.utc)
        dwell_s = (datetime.now(timezone.utc) - last_change).total_seconds()
    except Exception:
        dwell_s = 0.0

    # Determine NATURAL tier (what we'd be in given pnl_pct alone)
    if pnl_pct <= TIER_FLATTEN:
        natural_tier = "FLATTEN"
    elif pnl_pct <= TIER_PAUSE:
        natural_tier = "PAUSE"
    elif pnl_pct <= TIER_WARN:
        natural_tier = "WARN"
    else:
        natural_tier = "OK"

    # Apply hysteresis + minimum dwell on DEMOTIONS (escalations always fire immediately)
    tier_rank = {"OK": 0, "WARN": 1, "PAUSE": 2, "FLATTEN": 3}
    is_escalation = tier_rank[natural_tier] > tier_rank[prev_tier]
    is_demotion = tier_rank[natural_tier] < tier_rank[prev_tier]

    if is_demotion:
        # Apply hysteresis exit thresholds
        if prev_tier == "FLATTEN":
            # Require pnl > TIER_FLATTEN_EXIT AND minimum dwell
            if pnl_pct <= TIER_FLATTEN_EXIT or dwell_s < TIER_FLATTEN_MIN_DWELL_S:
                tier = "FLATTEN"  # hold
            else:
                tier = natural_tier
        elif prev_tier == "PAUSE":
            if pnl_pct <= TIER_PAUSE_EXIT or dwell_s < TIER_PAUSE_MIN_DWELL_S:
                tier = "PAUSE"  # hold
            else:
                tier = natural_tier
        else:
            tier = natural_tier
    else:
        tier = natural_tier

    if tier != prev_tier:
        state["last_tier_change_ts"] = now_utc
    state["current_tier"] = tier
    state["latest_equity_usd"] = equity
    state["latest_pnl_pct"] = round(pnl_pct, 3)
    state["latest_ts"] = now_utc

    # Action: touch / clear flags as appropriate
    actions: list[str] = []

    # FLATTEN tier — touch both flags. FLATTEN_EOD never auto-clears.
    if tier == "FLATTEN":
        if not HALT_PATH.exists():
            HALT_PATH.write_text(f"auto-halt: daily loss {pnl_pct:.2f}% (FLATTEN tier)", encoding="utf-8")
            actions.append("HALT.flag set")
        if not FLATTEN_PATH.exists():
            FLATTEN_PATH.write_text(f"auto-flatten: daily loss {pnl_pct:.2f}% (FLATTEN tier)", encoding="utf-8")
            actions.append("FLATTEN_EOD.flag set")

    # PAUSE tier — touch HALT.flag only
    elif tier == "PAUSE":
        if not HALT_PATH.exists():
            HALT_PATH.write_text(f"auto-halt: daily loss {pnl_pct:.2f}% (PAUSE tier)", encoding="utf-8")
            actions.append("HALT.flag set")

    # OK / WARN tier — release HALT.flag IF it was set by us (and not by user)
    else:
        if HALT_PATH.exists():
            try:
                content = HALT_PATH.read_text(encoding="utf-8")
                if content.startswith("auto-halt:"):
                    HALT_PATH.unlink()
                    actions.append("HALT.flag auto-cleared (recovery)")
            except Exception:
                pass
        # FLATTEN_EOD is NEVER auto-cleared — requires manual review

    # Track tier transitions + Discord alert
    if tier != prev_tier:
        state["tier_history"].append({
            "ts": now_utc,
            "from": prev_tier,
            "to": tier,
            "pnl_pct": round(pnl_pct, 3),
            "equity_usd": equity,
        })
        # Keep only last 20 transitions
        state["tier_history"] = state["tier_history"][-20:]

        # Fire Discord alert on tier transitions (with cooldown via dedicated state file)
        try:
            sys.path.insert(0, str(REPO))
            from ops._alert_helper import post_discord, load_cooldown_state, should_alert, mark_alerted, save_cooldown_state
            COOLDOWN_KEY = f"circuit_breaker_{tier}"
            COOLDOWN_MIN = 60  # don't re-alert same tier within 1h
            cd_state = load_cooldown_state("circuit_breaker_cooldown")
            if should_alert(cd_state, COOLDOWN_KEY, COOLDOWN_MIN):
                colors = {"WARN": 0xFFC107, "PAUSE": 0xFF8800, "FLATTEN": 0xFF4444, "OK": 0x00FF88}
                color = colors.get(tier, 0x9DA8C7)
                title = f"CIRCUIT BREAKER: {prev_tier} -> {tier}"
                desc = (
                    f"Daily PnL: **{pnl_pct:+.2f}%**\n"
                    f"Equity: ${equity:,.2f} (day open ${open_eq:,.2f})\n"
                    f"Actions: {', '.join(actions) if actions else 'none'}"
                )
                if post_discord(title, desc, color=color):
                    mark_alerted(cd_state, COOLDOWN_KEY, reason=f"tier {prev_tier}->{tier}")
                    save_cooldown_state("circuit_breaker_cooldown", cd_state)
        except Exception as e:
            print(f"  warn: Discord alert failed (non-fatal): {e}", file=sys.stderr)

    state["last_actions"] = actions
    _save_state(state)

    print(f"OK: equity=${equity:,.2f} pnl_pct={pnl_pct:+.2f}% tier={tier}{' actions=' + str(actions) if actions else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
