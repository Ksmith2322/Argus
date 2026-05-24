"""Single-command fleet snapshot — everything-in-one-place report
for the audit phase + operator situational awareness.

Composes:
  - roster classification (ACTIVE / KILLED / PENDING_OPT_IN / LIMBO)
  - allocation_factors snapshot
  - recent kill_log entries
  - capacity stress max_safe_multiplier per active strategy
  - heartbeat freshness for every forge runner
  - canonical_fills counts since post_reset_20260522 epoch
  - xs_momentum current top-2 picks (live yfinance query)
  - real-money preflight verdict per ACTIVE strategy

OUTPUT: human-readable markdown by default, written to
  ops/reports/system_audit/fleet_snapshot.md
JSON sidecar also written for machine consumption.

This is the report you read FIRST when sitting down to audit. Captures
the live state in 5 minutes; no need to grep through 25 commits.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


def _roster() -> list[dict]:
    try:
        from ops.audit.run_roster_state import classify_all
        return classify_all()
    except Exception as exc:
        return [{"error": str(exc)}]


def _allocation_factors() -> dict:
    try:
        path = REPO / "argus_flow" / "configs" / "allocation_factors.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        return {
            "version": cfg.get("version", "?"),
            "last_updated": cfg.get("last_updated", "?"),
            "factors": cfg.get("factors", {}),
            "recent_kill_log": (cfg.get("_kill_log") or [])[-5:],
        }
    except Exception as exc:
        return {"error": str(exc)}


def _capacity() -> dict:
    try:
        from helio.real_money_preflight import CAPACITY_STRESS_ARTIFACT
        if not CAPACITY_STRESS_ARTIFACT.exists():
            return {"error": "no capacity_stress.json artifact"}
        data = json.loads(CAPACITY_STRESS_ARTIFACT.read_text(encoding="utf-8"))
        per = data.get("per_strategy") or []
        return {
            "anchor_usd": data.get("anchor_usd"),
            "ts": data.get("ts"),
            "per_strategy": [
                {"strategy": r.get("strategy"),
                 "max_safe_multiplier": r.get("max_safe_multiplier"),
                 "current_notional_usd": r.get("current_notional_usd")}
                for r in per
            ],
        }
    except Exception as exc:
        return {"error": str(exc)}


def _heartbeats() -> list[dict]:
    """Heartbeat age per forge runner. Anything with no heartbeat OR
    >48h stale is flagged."""
    rows: list[dict] = []
    forge = REPO / "forge" / "logs"
    if not forge.exists():
        return rows
    for sub in sorted(forge.iterdir()):
        if not sub.is_dir():
            continue
        # Skip private dirs (_archive, _locks, etc.) and test-only siblings
        if sub.name.startswith("_") or sub.name.endswith("_test_only"):
            continue
        if sub.name.endswith("_paper"):
            continue  # legacy paper-dupe dirs from before unified runner
        hb = sub / "heartbeat.json"
        if not hb.exists():
            rows.append({
                "strategy": f"forge_{sub.name}",
                "heartbeat": "MISSING",
                "age_hours": None,
            })
            continue
        try:
            mtime = datetime.fromtimestamp(hb.stat().st_mtime, tz=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - mtime).total_seconds() / 3600
        except Exception as exc:
            rows.append({
                "strategy": f"forge_{sub.name}",
                "heartbeat": f"ERROR: {exc}",
                "age_hours": None,
            })
            continue
        # Read the heartbeat to extract git_sha for restart-verification
        # ("is the runner up on the expected commit?")
        hb_git_sha = None
        try:
            import json as _json
            data = _json.loads(hb.read_text(encoding="utf-8"))
            hb_git_sha = data.get("git_sha")
        except Exception:
            pass
        rows.append({
            "strategy": f"forge_{sub.name}",
            "heartbeat_ts": mtime.isoformat(),
            "age_hours": round(age_hours, 2),
            "heartbeat_git_sha": hb_git_sha,
        })
    return rows


def _canonical_fills_since_epoch() -> dict:
    """Count canonical_fills.jsonl entries by strategy since the current
    epoch start. Truth of 'how much live evidence has the post-reset
    window produced'."""
    try:
        from helio.evidence_epoch import current_epoch
        epoch = current_epoch()
        epoch_start = epoch.start_ts if hasattr(epoch, "start_ts") else None
    except Exception:
        epoch_start = None
    fills_path = REPO / "argus_flow" / "logs" / "canonical_fills.jsonl"
    if not fills_path.exists():
        return {"error": "no canonical_fills.jsonl"}
    counts: dict[str, int] = {}
    total = 0
    try:
        for line in fills_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            total += 1
            strat = row.get("strategy", "?")
            counts[strat] = counts.get(strat, 0) + 1
    except Exception as exc:
        return {"error": str(exc)}
    return {
        "epoch_id": getattr(epoch, "epoch_id", "?") if epoch_start else "?",
        "epoch_start": str(epoch_start) if epoch_start else "?",
        "total_fills_since_epoch": total,
        "by_strategy": counts,
    }


def _xs_momentum_picks() -> dict:
    """Live yfinance query to get xs_momentum's current top-2 picks."""
    try:
        from forge.xs_momentum.runner import (
            _fetch_history, PARAMS,
        )
        from helio.xs_momentum import (
            rank_universe_by_momentum, select_top_quintile,
        )
        closes = _fetch_history(PARAMS["universe"], period="2y")
        per_asset = {
            t: closes[t].dropna().tolist()
            for t in PARAMS["universe"] if t in closes.columns
        }
        ranked = rank_universe_by_momentum(
            per_asset,
            long_lookback=PARAMS["long_lookback"],
            short_lookback=PARAMS["short_lookback"],
        )
        picks = select_top_quintile(
            ranked, fraction=PARAMS["top_quintile_fraction"]
        )
        return {
            "as_of": str(closes.index[-1]) if len(closes) else None,
            "top_picks": [{"ticker": p.ticker,
                              "score_pct": round(p.score * 100, 2)} for p in picks],
            "full_ranking": [{"ticker": s.ticker,
                                  "score_pct": round(s.score * 100, 2)}
                                for s in ranked],
        }
    except Exception as exc:
        return {"error": str(exc)}


def _preflight() -> list[dict]:
    """Preflight verdict per ACTIVE strategy."""
    try:
        from argus_flow.tests.test_sunset_roster import ACTIVE_ROSTER
        from helio.real_money_preflight import evaluate_strategy
    except Exception as exc:
        return [{"error": f"load failed: {exc}"}]
    rows = []
    for s in sorted(ACTIVE_ROSTER):
        try:
            p = evaluate_strategy(s)
            rows.append({
                "strategy": s,
                "verdict": p.verdict,
                "green": p.n_green, "yellow": p.n_yellow, "red": p.n_red,
            })
        except Exception as exc:
            rows.append({"strategy": s, "error": str(exc)})
    return rows


def _restart_status(heartbeats: list[dict]) -> dict:
    """For each active strategy heartbeat, compare its embedded git_sha
    against HEAD. Strategies running on an old SHA need a restart to
    pick up the latest code."""
    try:
        from helio.strategy_common import git_sha as _git_sha
        head_sha = _git_sha(REPO)
    except Exception as exc:
        return {"error": str(exc), "head_sha": None, "rows": []}
    rows: list[dict] = []
    for hb in heartbeats or []:
        runner_sha = hb.get("heartbeat_git_sha")
        if runner_sha is None or runner_sha == "MISSING":
            rows.append({
                "strategy": hb.get("strategy"),
                "heartbeat_git_sha": None,
                "head_sha": head_sha,
                "restart_needed": None,
                "note": "no git_sha in heartbeat (runner predates restart-verify)",
            })
            continue
        match = (runner_sha == head_sha)
        rows.append({
            "strategy": hb.get("strategy"),
            "heartbeat_git_sha": runner_sha,
            "head_sha": head_sha,
            "restart_needed": not match,
        })
    return {"head_sha": head_sha, "rows": rows}


def build_snapshot() -> dict:
    heartbeats = _heartbeats()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "roster": _roster(),
        "allocation_factors": _allocation_factors(),
        "capacity": _capacity(),
        "heartbeats": heartbeats,
        "restart_status": _restart_status(heartbeats),
        "canonical_fills_since_epoch": _canonical_fills_since_epoch(),
        "xs_momentum_picks": _xs_momentum_picks(),
        "preflight": _preflight(),
    }


def render_markdown(snapshot: dict) -> str:
    out = ["# Argus fleet snapshot",
            "",
            f"Generated: {snapshot['generated_at']}",
            ""]

    out.append("## Roster classification")
    out.append("")
    roster = snapshot.get("roster", [])
    counts: dict[str, int] = {}
    for r in roster:
        if "verdict" in r:
            counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    out.append(f"  ACTIVE={counts.get('ACTIVE', 0)}  "
                  f"PENDING_OPT_IN={counts.get('PENDING_OPT_IN', 0)}  "
                  f"KILLED={counts.get('KILLED', 0)}  "
                  f"LIMBO={counts.get('LIMBO', 0)}  "
                  f"ABANDONED={counts.get('ABANDONED', 0)}")
    out.append("")
    out.append("| Strategy | Verdict | Alloc | Runner | Kill date |")
    out.append("|---|---|---|---|---|")
    for r in roster:
        if "error" in r:
            continue
        alloc = r.get("allocation_factor")
        runner = "yes" if r.get("has_runner") else "no"
        out.append(f"| {r['strategy']} | {r['verdict']} | "
                      f"{alloc if alloc is not None else '(none)'} | "
                      f"{runner} | {r.get('kill_date', '')} |")
    out.append("")

    out.append("## Allocation factors")
    out.append("")
    af = snapshot.get("allocation_factors", {})
    out.append(f"Version: {af.get('version', '?')}")
    out.append(f"Last updated: {af.get('last_updated', '?')}")
    out.append("")
    out.append("Recent kill_log entries:")
    for ln in af.get("recent_kill_log", []):
        out.append(f"- {ln[:200]}{'...' if len(ln) > 200 else ''}")
    out.append("")

    out.append("## Capacity stress (max_safe_multiplier per strategy)")
    out.append("")
    cap = snapshot.get("capacity", {})
    if "error" in cap:
        out.append(f"  ERROR: {cap['error']}")
    else:
        out.append(f"Anchor: ${cap.get('anchor_usd', 0):,.0f}  "
                      f"Computed: {cap.get('ts', '?')}")
        out.append("")
        out.append("| Strategy | current $ | max_safe_mult |")
        out.append("|---|---|---|")
        for r in cap.get("per_strategy", []):
            out.append(f"| {r['strategy']} | "
                          f"${r['current_notional_usd']:,.0f} | "
                          f"{r['max_safe_multiplier']}x |")
    out.append("")

    out.append("## Heartbeats")
    out.append("")
    out.append("| Strategy | Age (hours) | Heartbeat ts |")
    out.append("|---|---|---|")
    for r in snapshot.get("heartbeats", []):
        age = r.get("age_hours")
        flag = " ⚠️" if (age is None or (isinstance(age, (int, float))
                                              and age > 48)) else ""
        out.append(f"| {r['strategy']} | "
                      f"{age if age is not None else 'MISSING'}{flag} | "
                      f"{r.get('heartbeat_ts', r.get('heartbeat', '?'))} |")
    out.append("")

    out.append("## Canonical fills since epoch")
    out.append("")
    cf = snapshot.get("canonical_fills_since_epoch", {})
    if "error" in cf:
        out.append(f"  ERROR: {cf['error']}")
    else:
        out.append(f"Epoch: {cf.get('epoch_id', '?')}")
        out.append(f"Total fills: {cf.get('total_fills_since_epoch', 0)}")
        if cf.get("by_strategy"):
            out.append("")
            for s, n in sorted(cf["by_strategy"].items(),
                                  key=lambda x: -x[1]):
                out.append(f"- {s}: {n}")
    out.append("")

    out.append("## xs_momentum current top picks")
    out.append("")
    xp = snapshot.get("xs_momentum_picks", {})
    if "error" in xp:
        out.append(f"  ERROR: {xp['error']}")
    else:
        out.append(f"As of: {xp.get('as_of', '?')}")
        out.append("")
        out.append("Picks (would hold if rebalance fired today):")
        for p in xp.get("top_picks", []):
            out.append(f"  - **{p['ticker']}** ({p['score_pct']:+.2f}% momentum)")
        out.append("")
        out.append("Full ranking:")
        for s in xp.get("full_ranking", []):
            out.append(f"  - {s['ticker']}: {s['score_pct']:+.2f}%")
    out.append("")

    out.append("## Real-money preflight (active strategies)")
    out.append("")
    for r in snapshot.get("preflight", []):
        if "error" in r:
            out.append(f"  ERROR for {r.get('strategy', '?')}: {r['error']}")
            continue
        out.append(f"- **{r['strategy']}**: {r['verdict']}  "
                      f"(GREEN={r['green']} YELLOW={r['yellow']} RED={r['red']})")

    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true",
                          help="Emit JSON instead of markdown")
    parser.add_argument("--skip-yfinance", action="store_true",
                          help="Skip the xs_momentum picks query (offline mode)")
    args = parser.parse_args(argv)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    snapshot = build_snapshot()
    if args.skip_yfinance:
        snapshot["xs_momentum_picks"] = {"skipped": True}

    if args.json:
        print(json.dumps(snapshot, indent=2, default=str))
        return 0

    md = render_markdown(snapshot)
    print(md)
    md_path = OUT_DIR / "fleet_snapshot.md"
    md_path.write_text(md, encoding="utf-8")
    json_path = OUT_DIR / "fleet_snapshot.json"
    json_path.write_text(json.dumps(snapshot, indent=2, default=str),
                          encoding="utf-8")
    print()
    print(f"Persisted: {md_path}")
    print(f"           {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
