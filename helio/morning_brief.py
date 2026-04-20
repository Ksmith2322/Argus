"""Morning brief — single consolidated digest of overnight fleet changes.

Aggregates canonical reports into ONE text file + Discord post so the operator
doesn't have to read 8 JSON files every morning. Highlights:
  - Overnight new live trades (from canonical_fills since last brief)
  - Gateway events (auto-clears, PAUSE_ENTRIES age spikes)
  - Tier changes / kill watchdog alerts
  - Drift severity shifts
  - Apollo forward-return + counterfactual updates
  - Promotion-readiness next-action changes

Output:
  argus_flow/logs/morning_brief.txt         (latest)
  argus_flow/logs/morning_brief_history.jsonl  (time series)

Run:
    python -m helio.morning_brief
    python -m helio.morning_brief --no-discord
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
OUT_TXT = _REPO / "argus_flow" / "logs" / "morning_brief.txt"
HISTORY_PATH = _REPO / "argus_flow" / "logs" / "morning_brief_history.jsonl"
STATE_PATH = _REPO / "argus_flow" / "logs" / "morning_brief_state.json"


def _load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return out


def _load_state() -> dict:
    """Persistent state between brief runs. Tracks last-brief timestamp so we
    can compute 'since last brief' diffs."""
    return _load_json(STATE_PATH) or {"last_brief_ts": None}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def _since(last_ts_iso: str | None) -> datetime:
    """Return the cutoff datetime for 'new since last brief'."""
    if last_ts_iso:
        try:
            dt = datetime.fromisoformat(last_ts_iso.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
    # Default: 24 hours ago
    return datetime.now(timezone.utc) - timedelta(hours=24)


def _parse_ts(s: str):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def build_brief() -> dict:
    """Compose the morning brief data structure from canonical reports."""
    state = _load_state()
    cutoff = _since(state.get("last_brief_ts"))
    now = datetime.now(timezone.utc)

    brief: dict = {
        "generated_at": now.isoformat(),
        "since": cutoff.isoformat(),
        "sections": {},
    }

    # 1. Fleet health snapshot
    fs = _load_json(_REPO / "argus_flow" / "logs" / "fleet_status.json") or {}
    systems = fs.get("systems", {})
    non_ok = [(n, s.get("status")) for n, s in systems.items() if s.get("status") not in ("OK", None)]
    brief["sections"]["fleet_health"] = {
        "total_systems": len(systems),
        "non_ok": non_ok,
        "control_files": fs.get("control_files", {}),
        "risk_disagreement": fs.get("risk_state", {}).get("disagreement"),
    }

    # 2. Overnight new trades from canonical_fills.
    # Second consumer of helio.domain.Fill (after reconciliation.py). Uses
    # read_fills() which already globs rotated archives, so this section is
    # rotation-safe without any change needed if canonical rotation starts.
    from helio.canonical_fills import read_fills
    from helio.domain import Fill

    raw_fills = read_fills()
    new_fills: list[Fill] = []
    for r in raw_fills:
        ts = _parse_ts(r.get("ts", ""))
        if ts and ts >= cutoff:
            new_fills.append(Fill.from_canonical_row(r))
    total_new_pnl = sum((f.pnl_usd or 0.0) for f in new_fills)
    brief["sections"]["new_trades"] = {
        "count": len(new_fills),
        "total_pnl_usd": round(total_new_pnl, 2),
        "by_strategy": {},
    }
    for f in new_fills:
        by = brief["sections"]["new_trades"]["by_strategy"].setdefault(
            f.strategy or "unknown", {"count": 0, "pnl_usd": 0.0}
        )
        by["count"] += 1
        by["pnl_usd"] += (f.pnl_usd or 0.0)
    for strat, v in brief["sections"]["new_trades"]["by_strategy"].items():
        v["pnl_usd"] = round(v["pnl_usd"], 2)

    # 3. Kill watchdog hits
    kw = _load_json(_REPO / "argus_flow" / "logs" / "kill_watchdog_report.json") or {}
    kw_summary = kw.get("summary", {})
    brief["sections"]["kill_watchdog"] = {
        "kill_candidates": kw_summary.get("kill_candidates", []),
        "drift_warnings": kw_summary.get("drift_warnings", []),
    }

    # 4. Signal-frequency drift
    sf = _load_json(_REPO / "argus_flow" / "logs" / "signal_frequency_report.json") or {}
    sf_summary = sf.get("summary", {})
    brief["sections"]["drift"] = {
        "severe": sf_summary.get("severe_drift", []),
        "warning": sf_summary.get("warning_drift", []),
    }

    # 5. Promotion readiness — any strategy that moved to a new action
    pr = _load_json(_REPO / "argus_flow" / "logs" / "promotion_readiness_report.json") or {}
    pr_summary = pr.get("summary", {})
    brief["sections"]["promotion"] = {
        "promotion_eligible": pr_summary.get("promotion_eligible", []),
        "review_gate_passed": pr_summary.get("review_gate_passed", []),
        "kill_candidates": pr_summary.get("kill_candidates", []),
    }

    # 6. Apollo — forward return growth + matured counterfactual
    fr = _load_jsonl(_REPO / "apollo" / "logs" / "forward_returns.jsonl")
    new_fr = [r for r in fr if (ts := _parse_ts(r.get("scan_date", ""))) and ts >= cutoff]
    matured = _load_jsonl(_REPO / "apollo" / "logs" / "matured_trades.jsonl")
    new_matured = [m for m in matured if (ts := _parse_ts(m.get("matured_at", ""))) and ts >= cutoff]
    new_cf_pnl = sum(float(m.get("counterfactual_pnl_usd") or 0) for m in new_matured)
    brief["sections"]["apollo"] = {
        "new_forward_records": len(new_fr),
        "total_forward_records": len(fr),
        "new_matured": len(new_matured),
        "total_matured": len(matured),
        "new_counterfactual_pnl_usd": round(new_cf_pnl, 2),
    }

    # 7. Gateway + broker equity summary
    be = _load_jsonl(_REPO / "argus_flow" / "logs" / "broker_equity_history.jsonl")
    latest_eq = be[-1].get("broker_equity_usd") if be else None
    brief["sections"]["broker"] = {
        "latest_equity_usd": latest_eq,
        "samples_collected": len(be),
    }

    return brief


def render_text(brief: dict) -> str:
    lines = []
    lines.append(f"=== FLEET MORNING BRIEF — {brief['generated_at']} ===")
    lines.append(f"Since: {brief['since']}")
    lines.append("")

    fh = brief["sections"]["fleet_health"]
    status_bullet = "OK" if not fh["non_ok"] and not fh.get("risk_disagreement") else "ATTENTION"
    lines.append(f"FLEET HEALTH: {status_bullet}  ({fh['total_systems']} systems)")
    if fh["non_ok"]:
        lines.append(f"  Non-OK: {fh['non_ok']}")
    if fh.get("risk_disagreement"):
        lines.append(f"  Risk drift: {fh['risk_disagreement']}")
    ctrl = fh.get("control_files", {})
    if ctrl.get("PAUSE_ENTRIES", {}).get("present"):
        lines.append(f"  PAUSE_ENTRIES active ({ctrl['PAUSE_ENTRIES']['age_s']}s)")
    lines.append("")

    nt = brief["sections"]["new_trades"]
    lines.append(f"NEW TRADES since last brief: {nt['count']} "
                 f"(total PnL ${nt['total_pnl_usd']:+.2f})")
    for strat, v in nt["by_strategy"].items():
        lines.append(f"  {strat}: {v['count']} trades, PnL ${v['pnl_usd']:+.2f}")
    lines.append("")

    kw = brief["sections"]["kill_watchdog"]
    if kw["kill_candidates"] or kw["drift_warnings"]:
        lines.append("KILL WATCHDOG:")
        if kw["kill_candidates"]:
            lines.append(f"  KILL_CANDIDATES: {', '.join(kw['kill_candidates'])}")
        if kw["drift_warnings"]:
            lines.append(f"  DRIFT_WARNINGS: {', '.join(kw['drift_warnings'])}")
    else:
        lines.append("KILL WATCHDOG: all strategies pass")
    lines.append("")

    d = brief["sections"]["drift"]
    if d["severe"] or d["warning"]:
        lines.append("SIGNAL-FREQUENCY DRIFT:")
        if d["severe"]:
            lines.append(f"  severe (<20% of replay): {', '.join(d['severe'])}")
        if d["warning"]:
            lines.append(f"  warning (<50% of replay): {', '.join(d['warning'])}")
    else:
        lines.append("SIGNAL-FREQUENCY DRIFT: none")
    lines.append("")

    p = brief["sections"]["promotion"]
    lines.append("PROMOTION READINESS:")
    if p["promotion_eligible"]:
        lines.append(f"  PROMOTION_ELIGIBLE: {', '.join(p['promotion_eligible'])}")
    elif p["review_gate_passed"]:
        lines.append(f"  REVIEW_GATE_PASSED (freeze new 30d): {', '.join(p['review_gate_passed'])}")
    elif p["kill_candidates"]:
        lines.append(f"  KILL_CANDIDATES: {', '.join(p['kill_candidates'])}")
    else:
        lines.append("  no strategies at any gate — observe more")
    lines.append("")

    a = brief["sections"]["apollo"]
    lines.append(f"APOLLO: {a['new_forward_records']} new forward-returns (total {a['total_forward_records']}), "
                 f"{a['new_matured']} new matured (total {a['total_matured']}), "
                 f"counterfactual PnL ${a['new_counterfactual_pnl_usd']:+.2f} overnight")
    lines.append("")

    b = brief["sections"]["broker"]
    if b.get("latest_equity_usd"):
        lines.append(f"BROKER EQUITY: ${b['latest_equity_usd']:,.2f}  "
                     f"({b['samples_collected']} samples lifetime)")
    lines.append("")

    # Monitor-mode checklist — the Mon–Fri glance view. All-OK means the
    # operator can move on. Anything else surfaces with a link/action.
    lines.append("=== MONITOR-MODE CHECKLIST ===")
    cl = _build_checklist(brief)
    for item in cl["items"]:
        marker = "[OK]" if item["ok"] else "[!!]"
        lines.append(f"  {marker} {item['label']}")
        if not item["ok"] and item.get("detail"):
            lines.append(f"        -> {item['detail']}")
    lines.append("")
    lines.append(f"  Overall: {cl['overall']}")

    return "\n".join(lines)


def _build_checklist(brief: dict) -> dict:
    """Monitor-mode summary: 7 yes/no questions the operator should
    answer each morning. Returns {items: [...], overall: OK|ATTENTION}.

    Each item has:
      - label:  short description
      - ok:     boolean
      - detail: if not ok, what to look at
    """
    items = []
    fh = brief["sections"]["fleet_health"]
    kw = brief["sections"]["kill_watchdog"]
    d  = brief["sections"]["drift"]
    p  = brief["sections"]["promotion"]
    ctrl = fh.get("control_files", {})

    # 1. All systems heartbeat fresh
    fleet_ok = not fh["non_ok"]
    items.append({
        "label": "All systems healthy (heartbeats fresh, processes alive)",
        "ok": fleet_ok,
        "detail": f"non-OK systems: {fh['non_ok']}" if not fleet_ok else "",
    })

    # 2. No risk disagreement
    no_risk_drift = not fh.get("risk_disagreement")
    items.append({
        "label": "No broker-vs-paper risk drift",
        "ok": no_risk_drift,
        "detail": f"disagreement: {fh.get('risk_disagreement')}" if not no_risk_drift else "",
    })

    # 3. No PAUSE_ENTRIES blocking new trades
    no_pause = not (ctrl.get("PAUSE_ENTRIES", {}).get("present"))
    items.append({
        "label": "Entries not paused (PAUSE_ENTRIES absent)",
        "ok": no_pause,
        "detail": (f"age {ctrl.get('PAUSE_ENTRIES', {}).get('age_s', 0)}s "
                   "— auto-clear triggers if gateway supervision created it"
                   if not no_pause else ""),
    })

    # 4. Kill-watchdog: no kill candidates
    no_kill = not kw["kill_candidates"]
    items.append({
        "label": "No kill candidates flagged by watchdog",
        "ok": no_kill,
        "detail": f"candidates: {', '.join(kw['kill_candidates'])}" if not no_kill else "",
    })

    # 5. No severe drift
    no_severe = not d["severe"]
    items.append({
        "label": "No severe signal-frequency drift",
        "ok": no_severe,
        "detail": f"severe: {', '.join(d['severe'])}" if not no_severe else "",
    })

    # 6. Apollo: forward-returns backfill running (new records appearing)
    a = brief["sections"]["apollo"]
    apollo_flowing = a.get("total_forward_records", 0) > 0
    items.append({
        "label": "Apollo forward-returns pipeline producing data",
        "ok": apollo_flowing,
        "detail": "no forward returns captured — check apollo.ops.backfill_forward_returns"
                  if not apollo_flowing else "",
    })

    # 7. Broker equity was captured overnight
    b = brief["sections"]["broker"]
    has_equity = bool(b.get("latest_equity_usd"))
    items.append({
        "label": "Broker equity snapshot captured",
        "ok": has_equity,
        "detail": "no equity samples — fleet_monitor may not be running"
                  if not has_equity else "",
    })

    overall = "OK" if all(i["ok"] for i in items) else "ATTENTION"
    return {"items": items, "overall": overall}


def _discord_brief(text: str) -> None:
    """Post a compact summary to Discord (NOT the full brief — too long)."""
    try:
        from helio.fleet_monitor import send_discord
        # First 2 lines + KILL WATCHDOG + NEW TRADES summary
        lines = text.splitlines()
        summary = []
        summary.append(lines[0])  # heading
        # Pick the first 12 lines that have content
        for line in lines[1:]:
            if line.strip():
                summary.append(line)
            if len(summary) >= 14:
                break
        msg = "\n".join(summary)
        send_discord(f"**MORNING BRIEF**\n```\n{msg}\n```", system="morning_brief")
    except Exception:
        pass


def _append_history(brief: dict) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": brief["generated_at"],
        "new_trade_count": brief["sections"]["new_trades"]["count"],
        "new_trade_pnl_usd": brief["sections"]["new_trades"]["total_pnl_usd"],
        "kill_candidates": brief["sections"]["kill_watchdog"]["kill_candidates"],
        "drift_severe": brief["sections"]["drift"]["severe"],
        "fleet_non_ok": brief["sections"]["fleet_health"]["non_ok"],
    }
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-discord", action="store_true")
    ap.add_argument("--no-history", action="store_true")
    ap.add_argument("--no-state-update", action="store_true",
                    help="Don't update last_brief_ts (useful for dry runs)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    brief = build_brief()
    text = render_text(brief)

    OUT_TXT.parent.mkdir(parents=True, exist_ok=True)
    OUT_TXT.write_text(text, encoding="utf-8")

    if args.json:
        print(json.dumps(brief, indent=2))
    else:
        print(text)
        print(f"\nWrote: {OUT_TXT}")

    if not args.no_history:
        _append_history(brief)
    if not args.no_discord:
        _discord_brief(text)
    if not args.no_state_update:
        _save_state({"last_brief_ts": brief["generated_at"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
