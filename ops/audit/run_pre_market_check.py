"""Pre-market readiness check.

Run before the US market open (or wired into the
`ArgusPreMarketTwsCheck` scheduled task) to confirm the bot is
ready to trade.

Composes existing audits + adds the things you specifically want to
know BEFORE the bell rings:
  - daily_health_check (6 components)
  - data_feed contracts must be GREEN — refreshes once if RED, then
    re-verifies, before reporting
  - restart_status: every ACTIVE runner is on the HEAD commit
  - heartbeats: every ACTIVE runner has a fresh (<6h) heartbeat
  - roster_state: no drift, ACTIVE matches expectations
  - orphan_phantom: 0 phantoms
  - HALT.flag / FLATTEN_EOD.flag absent
  - First-action calendar: which strategies are scheduled to act
    today + the next 5 trading days (so the operator knows what to
    watch for)

Single verdict:
  READY     — green to trade
  REVIEW    — yellow; fixable items, operator should look before open
  NOT_READY — red; trading would risk lost money or wrong-state behavior

Exit codes 0/1/2. JSON artifact at
ops/reports/system_audit/pre_market_check.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


# ─── Per-strategy "is today an action day" hints ────────────────────

def _trading_days_from(start: datetime, n: int) -> list[datetime]:
    """Return next `n` Mon-Fri dates starting at (or after) `start`.
    Does NOT account for US holidays — operator validates against the
    NYSE calendar visually in the report."""
    days: list[datetime] = []
    cur = start
    while len(days) < n:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def _expected_actions(today: datetime) -> list[dict]:
    """Per-strategy heuristics for "is today an action day?".

    Intentionally simple — the strategies own their own scheduling at
    runtime. This is operator situational awareness, not a gate.
    """
    out: list[dict] = []
    next_5 = _trading_days_from(today, 5)
    dates_str = ", ".join(d.strftime("%a %m-%d") for d in next_5)

    # xs_momentum and its universe variants — monthly rebalance, fires
    # first Mon-Fri ≤ day-7 each month. All four run the same engine on
    # the same cadence; their UNIVERSE differs.
    rebalance_window = [d for d in next_5 if d.day <= 7]
    for label, universe_desc in (
        ("forge_xs_momentum",
         "broad-8 ETFs (SPY/QQQ/IWM/DIA/EFA/EEM/GLD/TLT)"),
        ("forge_xs_momentum_sectors",
         "11 SPDR sectors"),
        ("forge_xs_momentum_style",
         "8 style factors (VTV/VUG/MTUM/QUAL/etc.)"),
        ("forge_xs_momentum_legacy15",
         "15-ticker legacy (10 sectors + 5 country)"),
    ):
        out.append({
            "strategy": label,
            "schedule": f"monthly rebalance (first 7 days of month, weekdays) | {universe_desc}",
            "action_in_next_5_days": bool(rebalance_window),
            "expected_dates": [d.strftime("%Y-%m-%d") for d in rebalance_window],
        })

    # gld_pm_long — daily evaluation
    out.append({
        "strategy": "forge_gld_pm_long",
        "schedule": "daily PM evaluation (every weekday near close)",
        "action_in_next_5_days": True,
        "expected_dates": [d.strftime("%Y-%m-%d") for d in next_5],
    })

    # tom_spy — last ~3-4 trading days of the month + first ~2 of next
    eom_window = []
    for d in next_5:
        # Heuristic: day >= 25 OR day <= 2 → TOM window
        if d.day >= 25 or d.day <= 2:
            eom_window.append(d)
    out.append({
        "strategy": "forge_tom_spy",
        "schedule": "turn-of-month (last 3-4 trading days + first 1-2)",
        "action_in_next_5_days": bool(eom_window),
        "expected_dates": [d.strftime("%Y-%m-%d") for d in eom_window],
        "note": "PENDING_OPT_IN — won't fire unless operator activates",
    })

    # nov_spy — November only
    nov_dates = [d for d in next_5 if d.month == 11]
    out.append({
        "strategy": "forge_nov_spy",
        "schedule": "November only (single-month strategy)",
        "action_in_next_5_days": bool(nov_dates),
        "expected_dates": [d.strftime("%Y-%m-%d") for d in nov_dates],
        "note": "PENDING_OPT_IN — won't fire unless operator activates",
    })

    return out


# ─── Composition layer ───────────────────────────────────────────────

def _daily_health() -> dict:
    try:
        from ops.daily_health_check import evaluate
        return evaluate(post_discord=False)
    except Exception as exc:
        return {"worst_status": "ERROR", "error": str(exc), "reports": []}


def _refresh_and_verify_data_feeds(*, allow_refresh: bool) -> dict:
    """Verify contracts; if RED and allow_refresh, run the refresher
    once then re-verify. Returns the final verdicts + a 'refreshed'
    flag."""
    try:
        from helio.data_feed_contract import verify_all_contracts
    except Exception as exc:
        return {"error": str(exc), "refreshed": False, "contracts": {}}
    verdicts = verify_all_contracts()
    refreshed = False
    has_red = any(v["verdict"] == "RED" for v in verdicts.values())
    if has_red and allow_refresh:
        try:
            from helio.yfinance_data import fetch_universe
            from helio.data_feed_contract import CONTRACTS
            seen: set[str] = set()
            for c in CONTRACTS.values():
                new_tickers = [t for t in c.universe if t not in seen]
                seen.update(new_tickers)
                if new_tickers:
                    fetch_universe(new_tickers, refresh=False)
            refreshed = True
            verdicts = verify_all_contracts()
        except Exception as exc:
            return {
                "error": f"refresh failed: {exc}",
                "refreshed": False,
                "contracts": verdicts,
            }
    return {"refreshed": refreshed, "contracts": verdicts}


def _restart_status_for_active() -> dict:
    """For each ACTIVE strategy, compare its heartbeat git_sha against
    HEAD. Returns rows + a count of runners needing restart."""
    try:
        from argus_flow.tests.test_sunset_roster import ACTIVE_ROSTER
        from helio.strategy_common import git_sha as _git_sha
    except Exception as exc:
        return {"error": str(exc), "rows": [], "n_need_restart": 0}
    head = _git_sha(REPO)
    rows: list[dict] = []
    n_need_restart = 0
    for s in sorted(ACTIVE_ROSTER):
        # heartbeat path: forge/logs/<short>/heartbeat.json
        short = s.replace("forge_", "", 1)
        hb_path = REPO / "forge" / "logs" / short / "heartbeat.json"
        runner_sha: str | None = None
        hb_age_h: float | None = None
        if hb_path.exists():
            try:
                data = json.loads(hb_path.read_text(encoding="utf-8"))
                runner_sha = data.get("git_sha")
            except Exception:
                pass
            try:
                mtime = datetime.fromtimestamp(hb_path.stat().st_mtime, tz=timezone.utc)
                hb_age_h = round((datetime.now(timezone.utc) - mtime).total_seconds() / 3600, 2)
            except Exception:
                pass
        # Three cases trigger restart-needed:
        #   1. heartbeat exists but git_sha is missing (runner predates
        #      the gap-#9 heartbeat-version feature; needs restart on
        #      current code so we can verify)
        #   2. heartbeat exists with git_sha != HEAD
        #   3. heartbeat file doesn't exist at all (handled by the
        #      separate heartbeat_freshness check, not here)
        sha_unknown = (hb_path.exists() and not runner_sha)
        sha_mismatch = (runner_sha is not None and runner_sha != head)
        needs_restart = sha_unknown or sha_mismatch
        if needs_restart:
            n_need_restart += 1
        rows.append({
            "strategy": s,
            "head_sha": head,
            "heartbeat_git_sha": runner_sha,
            "heartbeat_age_hours": hb_age_h,
            "needs_restart": needs_restart,
            "reason": ("sha_unknown" if sha_unknown
                       else "sha_mismatch" if sha_mismatch
                       else None),
            "heartbeat_missing": not hb_path.exists(),
        })
    return {
        "head_sha": head,
        "rows": rows,
        "n_need_restart": n_need_restart,
    }


def _heartbeat_freshness(*, max_age_hours: float = 6.0) -> dict:
    """For each ACTIVE strategy, check heartbeat age."""
    try:
        from argus_flow.tests.test_sunset_roster import ACTIVE_ROSTER
    except Exception as exc:
        return {"error": str(exc), "stale": []}
    stale: list[dict] = []
    for s in sorted(ACTIVE_ROSTER):
        short = s.replace("forge_", "", 1)
        hb_path = REPO / "forge" / "logs" / short / "heartbeat.json"
        if not hb_path.exists():
            stale.append({"strategy": s, "age_hours": None,
                          "status": "MISSING"})
            continue
        try:
            mtime = datetime.fromtimestamp(hb_path.stat().st_mtime, tz=timezone.utc)
            age_h = (datetime.now(timezone.utc) - mtime).total_seconds() / 3600
        except Exception as exc:
            stale.append({"strategy": s, "age_hours": None,
                          "status": f"ERROR:{exc}"})
            continue
        if age_h > max_age_hours:
            stale.append({"strategy": s, "age_hours": round(age_h, 2),
                          "status": "STALE"})
    return {"max_age_hours": max_age_hours, "stale": stale}


def _flag_check() -> dict:
    """HALT.flag / FLATTEN_EOD.flag presence."""
    halt = REPO / "HALT.flag"
    flatten = REPO / "FLATTEN_EOD.flag"
    return {
        "halt_flag_present": halt.exists(),
        "flatten_flag_present": flatten.exists(),
    }


# ─── Verdict aggregation ─────────────────────────────────────────────

def _aggregate_verdict(
    daily: dict,
    feeds: dict,
    restart: dict,
    heartbeats: dict,
    flags: dict,
) -> tuple[str, list[str]]:
    """Combine all signals into READY / REVIEW / NOT_READY + list of
    operator-visible reasons."""
    reasons: list[str] = []

    # NOT_READY conditions (any one trips)
    if flags.get("halt_flag_present"):
        reasons.append("HALT.flag present — trading is paused")
    if daily.get("worst_status") == "ERROR":
        reasons.append("daily_health_check ERROR")
    contracts = (feeds.get("contracts") or {})
    any_red_contract = any(
        v.get("verdict") == "RED" for v in contracts.values()
    )
    if any_red_contract and not feeds.get("refreshed"):
        reasons.append("data_feed_contract RED and refresh failed")
    # Per-component RED check: preflight RED is expected (BLOCKED on
    # real-money allowlist by design) so we DON'T count it as
    # not-ready. Everything else RED is a problem.
    for r in (daily.get("reports") or []):
        if r.get("status") == "RED" and r.get("component") != "preflight":
            reasons.append(
                f"daily_health_check.{r.get('component')} RED: "
                f"{r.get('summary', '')[:80]}"
            )

    if reasons:
        return "NOT_READY", reasons

    # REVIEW conditions (any one trips)
    if restart.get("n_need_restart", 0) > 0:
        names = [r["strategy"] for r in (restart.get("rows") or [])
                 if r.get("needs_restart")]
        reasons.append(f"runners on old SHA: {names}")
    if any_red_contract and feeds.get("refreshed"):
        # Refresh ran but RED persisted
        reasons.append("data_feed_contract RED after refresh — investigate")
    any_yellow_contract = any(
        v.get("verdict") == "YELLOW" for v in contracts.values()
    )
    if any_yellow_contract:
        reasons.append("data_feed_contract YELLOW — some tickers stale")
    if heartbeats.get("stale"):
        names = [r["strategy"] for r in heartbeats["stale"]]
        reasons.append(f"stale or missing heartbeats: {names}")
    if flags.get("flatten_flag_present"):
        reasons.append("FLATTEN_EOD.flag present — exits will be forced today")

    if reasons:
        return "REVIEW", reasons
    return "READY", []


def build_report() -> dict:
    today = datetime.now(timezone.utc)
    daily = _daily_health()
    feeds = _refresh_and_verify_data_feeds(allow_refresh=True)
    restart = _restart_status_for_active()
    heartbeats = _heartbeat_freshness()
    flags = _flag_check()
    actions = _expected_actions(today)
    verdict, reasons = _aggregate_verdict(daily, feeds, restart, heartbeats, flags)
    return {
        "generated_at": today.isoformat(),
        "verdict": verdict,
        "reasons": reasons,
        "daily_health": daily,
        "data_feed_contracts": feeds,
        "restart_status": restart,
        "heartbeat_freshness": heartbeats,
        "flags": flags,
        "expected_actions_next_5_days": actions,
    }


def _exit_code(verdict: str) -> int:
    return {"READY": 0, "REVIEW": 1, "NOT_READY": 2}.get(verdict, 2)


def _render_markdown(report: dict) -> str:
    out = [
        "# Pre-market readiness check",
        "",
        f"Generated: {report['generated_at']}",
        f"Verdict: **{report['verdict']}**",
        "",
    ]
    if report["reasons"]:
        out.append("## Reasons")
        for r in report["reasons"]:
            out.append(f"- {r}")
        out.append("")

    daily = report["daily_health"]
    out.append("## Daily health check")
    out.append(f"worst: {daily.get('worst_status', '?')}")
    for r in daily.get("reports", []):
        out.append(f"  - {r.get('component'):16s} {r.get('status', '?'):6s}  "
                   f"{r.get('summary', '')[:90]}")
    out.append("")

    feeds = report["data_feed_contracts"]
    out.append("## Data-feed contracts")
    if feeds.get("refreshed"):
        out.append("(refreshed once during this check)")
    for name, v in (feeds.get("contracts") or {}).items():
        out.append(f"  - {name}: {v.get('verdict')}  "
                   f"{(v.get('reasons') or ['clean'])[0][:80]}")
    out.append("")

    restart = report["restart_status"]
    out.append(f"## Restart status (HEAD: {restart.get('head_sha', '?')[:8]})")
    out.append(f"  runners needing restart: {restart.get('n_need_restart', 0)}")
    for r in restart.get("rows", []):
        flag = " RESTART" if r.get("needs_restart") else ""
        sha = (r.get("heartbeat_git_sha") or "MISSING")
        sha_short = sha[:8] if sha != "MISSING" else "MISSING"
        out.append(f"  - {r['strategy']}: hb={sha_short}{flag}")
    out.append("")

    out.append("## Heartbeat freshness")
    stale = report["heartbeat_freshness"].get("stale") or []
    if stale:
        for s in stale:
            out.append(f"  - {s['strategy']}: {s.get('status')} "
                       f"({s.get('age_hours')}h)")
    else:
        out.append("  all ACTIVE heartbeats fresh")
    out.append("")

    out.append("## Action calendar (next 5 trading days)")
    for a in report["expected_actions_next_5_days"]:
        marker = " *" if a.get("action_in_next_5_days") else ""
        dates = ", ".join(a.get("expected_dates", [])) or "(none)"
        note = f"  ({a.get('note')})" if a.get("note") else ""
        out.append(f"  - {a['strategy']}{marker}: {a['schedule']} -> "
                   f"{dates}{note}")
    out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON to stdout instead of markdown")
    args = parser.parse_args(argv)

    report = build_report()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUT_DIR / "pre_market_check.json"
    json_path.write_text(
        json.dumps(report, indent=2, default=str),
        encoding="utf-8",
    )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(_render_markdown(report))
        print(f"\nPersisted: {json_path}")
    return _exit_code(report["verdict"])


if __name__ == "__main__":
    sys.exit(main())
