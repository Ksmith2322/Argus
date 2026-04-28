"""5/1 Review Ceremony — pre-ceremony data aggregation.

Pulls the 5 data sources listed in project_5_1_review_ceremony_20260501.md into
a single timestamped snapshot. Run this on 4/28 (baseline) and again 5/1
(ceremony day) — the diff between the two runs surfaces what actually moved
during the prep window.

Usage:
    cd c:/Argus/repo
    C:/Argus/.venv/Scripts/python.exe -m ops.run_5_1_prep

Output:
    argus_flow/logs/ceremony_prep/snapshot_<YYYYMMDD_HHMM>.json
    argus_flow/logs/ceremony_prep/snapshot_latest.json   (symlink-style copy)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _safe_load_json(path: Path) -> dict | None:
    """Read + parse JSON; return None on any failure (file missing, parse error)."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_canonical_fills_summary(window_days: int = 30) -> dict:
    """Summarize canonical_fills.jsonl over the trailing window — counts only,
    full data is in the canonical file itself. Avoids bloating the snapshot
    with thousands of fill records.
    """
    fp = REPO / "argus_flow" / "logs" / "canonical_fills.jsonl"
    if not fp.exists():
        return {"status": "missing", "path": str(fp.relative_to(REPO))}
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    by_strategy: dict[str, dict] = {}
    total = 0
    in_window = 0
    earliest_in_window = None
    latest_in_window = None
    try:
        with open(fp, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                total += 1
                strat = r.get("strategy") or "unknown"
                ts_raw = str(r.get("entry_ts") or "")
                if not ts_raw:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if ts < cutoff:
                    continue
                in_window += 1
                d = by_strategy.setdefault(strat, {"trades": 0, "first_ts": None, "last_ts": None})
                d["trades"] += 1
                if d["first_ts"] is None or ts < datetime.fromisoformat(d["first_ts"]):
                    d["first_ts"] = ts.isoformat()
                if d["last_ts"] is None or ts > datetime.fromisoformat(d["last_ts"]):
                    d["last_ts"] = ts.isoformat()
                if earliest_in_window is None or ts < earliest_in_window:
                    earliest_in_window = ts
                if latest_in_window is None or ts > latest_in_window:
                    latest_in_window = ts
    except Exception as e:
        return {"status": "read_error", "error": str(e)}
    return {
        "status": "ok",
        "window_days": window_days,
        "n_total_lines": total,
        "n_in_window": in_window,
        "earliest_in_window": earliest_in_window.isoformat() if earliest_in_window else None,
        "latest_in_window": latest_in_window.isoformat() if latest_in_window else None,
        "by_strategy": by_strategy,
    }


def _load_per_strategy_equity_curves() -> dict:
    """Pull per-strategy daily PnL series via the dashboard endpoint if reachable;
    fall back to file-based summary if dashboard is offline.
    """
    try:
        import urllib.request
        with urllib.request.urlopen("http://localhost:8080/api/strategy_efficiency?window_days=30", timeout=5) as r:
            return {"status": "ok_via_dashboard", "data": json.loads(r.read())}
    except Exception as e:
        return {"status": "dashboard_unreachable", "error": str(e),
                "note": "Snapshot will lack per-strategy equity-curve detail. Start dashboard before re-running."}


def main() -> int:
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d_%H%M")
    out_dir = REPO / "argus_flow" / "logs" / "ceremony_prep"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"snapshot_{stamp}.json"
    latest_path = out_dir / "snapshot_latest.json"

    snapshot = {
        "generated_at_utc": now.isoformat(),
        "stamp": stamp,
        "purpose": "5/1 Review Ceremony pre-aggregated data — diff against ceremony-day snapshot to see what moved",
        "sources": {
            # 1. Canonical fills (summarized — full data lives in canonical_fills.jsonl)
            "canonical_fills_30d": _load_canonical_fills_summary(window_days=30),
            "canonical_fills_60d": _load_canonical_fills_summary(window_days=60),
            "canonical_fills_90d": _load_canonical_fills_summary(window_days=90),
            # 2. Operational maturity (latest)
            "operational_maturity": _safe_load_json(REPO / "argus_flow" / "logs" / "operational_maturity_latest.json"),
            # 3. Fleet performance summary
            "fleet_perf_summary": _safe_load_json(REPO / "argus_flow" / "logs" / "fleet_perf_summary.json"),
            # 4. Promotion readiness
            "promotion_readiness": _safe_load_json(REPO / "argus_flow" / "logs" / "promotion_readiness_report.json"),
            "promotion_gate": _safe_load_json(REPO / "argus_flow" / "logs" / "promotion_gate_report.json"),
            # 5. Per-strategy equity curves (via dashboard)
            "strategy_efficiency": _load_per_strategy_equity_curves(),
        },
    }

    # Quick top-level summary the user can eyeball without parsing the full snapshot
    om = snapshot["sources"]["operational_maturity"] or {}
    fp = snapshot["sources"]["fleet_perf_summary"] or {}
    pr = snapshot["sources"]["promotion_readiness"] or {}
    cf30 = snapshot["sources"]["canonical_fills_30d"] or {}

    snapshot["summary"] = {
        "n_strategies": (om.get("totals") or {}).get("strategies_count"),
        "total_live_trades_post_clamp": (om.get("totals") or {}).get("total_live_trades"),
        "verdict_counts": (om.get("totals") or {}).get("verdict_counts"),
        "fleet_pnl_30d_usd": (om.get("totals") or {}).get("total_live_pnl_usd"),
        "anchor_capital_usd": fp.get("anchor_capital_usd"),
        "fleet_perf_window_days": fp.get("live_window"),
        "n_promotion_gate_passing": sum(
            1 for s in pr.get("strategies", [])
            if s.get("review_gate_passed")
        ) if pr else None,
        "canonical_fills_in_30d": cf30.get("n_in_window"),
        "strategies_with_trades_30d": len(cf30.get("by_strategy", {})) if cf30.get("status") == "ok" else None,
    }

    out_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    latest_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

    s = snapshot["summary"]
    print(f"5/1 prep snapshot written: {out_path.relative_to(REPO)}")
    print(f"Latest copy:               {latest_path.relative_to(REPO)}")
    print()
    print(f"  Strategies:                  {s['n_strategies']}")
    print(f"  Total live trades:           {s['total_live_trades_post_clamp']}")
    print(f"  Fleet PnL 30d (USD):         ${s['fleet_pnl_30d_usd']}")
    print(f"  Anchor capital (USD):        ${s['anchor_capital_usd']}")
    print(f"  Trades in last 30d:          {s['canonical_fills_in_30d']}")
    print(f"  Strategies with trades 30d:  {s['strategies_with_trades_30d']}")
    print(f"  Promotion gate passing:      {s['n_promotion_gate_passing']}")
    if s["verdict_counts"]:
        print(f"  Verdicts: {s['verdict_counts']}")

    eff = snapshot["sources"]["strategy_efficiency"]
    if eff and eff.get("status") != "ok_via_dashboard":
        print()
        print(f"  WARN: dashboard unreachable — equity-curve detail is missing from snapshot")
        print(f"        ({eff.get('error', 'unknown error')})")
        print(f"        Restart dashboard with `python ops/dashboard.py --port 8080` and re-run.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
