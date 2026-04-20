"""Apollo execution-path skeleton (research_only mode).

Converts each new Apollo scan with score >= threshold into a "planned trade"
ticket. Matures each planned trade against forward_returns.jsonl to compute
would-have-been PnL. Never actually submits orders.

This is the pre-live scaffolding: when forward-return sample grows to n=100+
and the edge holds, flip `MODE` to `paper` and wire `_submit_order` to a
broker client. Until then this produces falsifiable evidence of what Apollo
would have earned if it had an execution path.

Output files:
  apollo/logs/planned_trades.jsonl    — one line per would-have-traded signal
  apollo/logs/matured_trades.jsonl    — planned trades whose T+N has elapsed,
                                         with realized-counterfactual PnL

Usage:
    python -m apollo.execution.planned_trades           # plan + mature
    python -m apollo.execution.planned_trades --plan    # only plan new tickets
    python -m apollo.execution.planned_trades --mature  # only mature existing
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))

from helio.fleet_sizing import compute_risk_usd, get_sizing_anchor_usd  # noqa: E402

APOLLO_LOGS = _REPO / "apollo" / "logs"
PLANNED_PATH = APOLLO_LOGS / "planned_trades.jsonl"
MATURED_PATH = APOLLO_LOGS / "matured_trades.jsonl"
FORWARD_RETURNS_PATH = APOLLO_LOGS / "forward_returns.jsonl"

MODE = "research_only"  # research_only | paper | live — flip when ready
# SCORE_FLOOR, HORIZON_TRADING_DAYS, MAX_PLANNED_* loaded via registry with
# fallback (Phase 2 pattern). See forge/gld_pm_long/runner.py for the
# template. Phase 1 equivalence test pins registry==defaults; this
# migration is a no-op behavior change today.
_HARDCODED_DEFAULTS = {
    "SCORE_FLOOR": 75,       # match apollo/runner.py post-dedup threshold
    "HORIZON_TRADING_DAYS": 3,  # default T+3; strategy card can override
    "MAX_PLANNED_PER_SYMBOL_PER_EARNINGS": 1,  # dedupe
}


def _load_from_registry() -> dict:
    try:
        from helio.strategy_registry import load_registry
        reg = load_registry().apollo_earnings_drift
        return {
            "SCORE_FLOOR": reg.score_floor,
            "HORIZON_TRADING_DAYS": reg.horizon_trading_days,
            "MAX_PLANNED_PER_SYMBOL_PER_EARNINGS": reg.max_planned_per_symbol_per_earnings,
        }
    except Exception:
        return dict(_HARDCODED_DEFAULTS)


_PARAMS = _load_from_registry()
SCORE_FLOOR = _PARAMS["SCORE_FLOOR"]
HORIZON_TRADING_DAYS = _PARAMS["HORIZON_TRADING_DAYS"]
MAX_PLANNED_PER_SYMBOL_PER_EARNINGS = _PARAMS["MAX_PLANNED_PER_SYMBOL_PER_EARNINGS"]


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _scan_date_from_filename(fname: str):
    stem = Path(fname).stem
    if not stem.startswith("scan_"):
        return None
    try:
        return datetime.strptime(stem[len("scan_"):], "%Y%m%d").date()
    except ValueError:
        return None


def plan_new_tickets() -> list[dict]:
    """Read apollo/logs/scan_*.json, emit a planned-trade ticket for each
    unique (symbol, earnings_date) with score >= SCORE_FLOOR.
    Idempotent — skips tickets already in planned_trades.jsonl.
    """
    existing = _load_jsonl(PLANNED_PATH)
    seen_keys = {(r["symbol"], r.get("earnings_date")) for r in existing}

    scan_files = sorted(glob.glob(str(APOLLO_LOGS / "scan_*.json")))
    if not scan_files:
        return []

    new_tickets = []
    # Only plan tickets from the LATEST scan per day — earlier scans may have
    # weaker data. If multiple scan files exist for same date, use the latest.
    latest_by_date: dict = {}
    for sf in scan_files:
        d = _scan_date_from_filename(Path(sf).name)
        if d is None:
            continue
        # Later mtime wins
        if d not in latest_by_date or Path(sf).stat().st_mtime > Path(latest_by_date[d]).stat().st_mtime:
            latest_by_date[d] = sf

    anchor = get_sizing_anchor_usd()
    for scan_date, sf in sorted(latest_by_date.items()):
        try:
            rows = json.loads(Path(sf).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rows, list):
            continue
        for r in rows:
            if not isinstance(r, dict):
                continue
            symbol = str(r.get("symbol") or "").upper()
            score = r.get("score") or 0
            direction = r.get("direction") or "long"
            earnings_date = r.get("earnings_date") or ""
            if not symbol or score < SCORE_FLOOR:
                continue
            if direction not in ("long", "short"):
                continue
            if (symbol, earnings_date) in seen_keys:
                continue
            ticket = {
                "ticket_id": f"APL-{scan_date.isoformat()}-{symbol}",
                "scan_date": scan_date.isoformat(),
                "mode": MODE,
                "symbol": symbol,
                "direction": direction,
                "score": int(score),
                "earnings_date": earnings_date,
                "days_until_earnings": r.get("days_until"),
                "target_horizon_td": HORIZON_TRADING_DAYS,
                "planned_at": datetime.now(timezone.utc).isoformat(),
                "anchor_at_plan_usd": anchor,
                "risk_budget_usd": None,  # filled at mature time when exit price known
                "status": "planned",
                "post_er_play": bool(r.get("post_er_play")),
            }
            new_tickets.append(ticket)
            seen_keys.add((symbol, earnings_date))

    _append_jsonl(PLANNED_PATH, new_tickets)
    return new_tickets


def mature_tickets() -> list[dict]:
    """For each `planned` ticket, look up forward_returns.jsonl to find its
    realized T+N outcome. Write the matured rows with a counterfactual PnL.
    Marks the original ticket 'matured' so it won't be re-processed.
    """
    planned = _load_jsonl(PLANNED_PATH)
    if not planned:
        return []
    forward_records = _load_jsonl(FORWARD_RETURNS_PATH)
    # Index forward returns by (symbol, scan_date)
    fr_index: dict = {}
    for fr in forward_records:
        key = (fr.get("symbol", "").upper(), fr.get("scan_date", ""))
        fr_index[key] = fr

    matured_existing = _load_jsonl(MATURED_PATH)
    matured_keys = {m["ticket_id"] for m in matured_existing}

    new_matured = []
    # Rewrite planned.jsonl with updated status
    updated_planned = []
    for t in planned:
        if t["status"] != "planned":
            updated_planned.append(t)
            continue
        if t["ticket_id"] in matured_keys:
            t["status"] = "matured"
            updated_planned.append(t)
            continue
        key = (t["symbol"], t["scan_date"])
        fr = fr_index.get(key)
        if fr is None:
            updated_planned.append(t)
            continue
        horizon_key = f"T+{t.get('target_horizon_td', HORIZON_TRADING_DAYS)}"
        horizon_data = fr.get("forward_returns_pct", {}).get(horizon_key)
        if horizon_data is None:
            updated_planned.append(t)
            continue
        # Compute counterfactual PnL at time of planning.
        #
        # HONEST MATH CAVEATS (see RESTRUCTURE_GUIDE §9 — 2026-04-19):
        # This is a fictional PnL until MODE flips to 'paper' and real
        # fills are captured. The assumptions:
        #   1. risk_usd = compute_risk_usd(0.005) — 0.5% of anchor
        #      (unproven-tier sizing; conservative)
        #   2. deployed_usd = risk_usd / assumed_stop_pct
        #      where assumed_stop_pct = 2% (typical PEAD stop distance).
        #      This is a GUESS. Until MODE='paper' measures a real stop
        #      distribution, we can't know whether 2% is representative.
        #   3. counterfactual_pnl_usd = deployed_usd × ret_pct / 100
        #      treats the T+N forward return as the full % move on
        #      deployed capital. No slippage, no commission, no partial
        #      fills. No overnight risk model. No real IBKR behavior.
        #
        # Consumer contract: matured rows carry `counterfactual: true`
        # and assumptions in `math_assumptions` so the dashboard / morning
        # brief can label these numbers HONESTLY as hypothetical.
        #
        # When MODE flips to 'paper': delete this block, capture real
        # fills via canonical_fills dual-write, compute PnL from the
        # actual buys/sells. The counterfactual story ends there.
        anchor_at_plan = float(t.get("anchor_at_plan_usd") or get_sizing_anchor_usd())
        risk_usd = compute_risk_usd(0.005)
        ret_pct = float(horizon_data.get("return_pct") or 0.0)
        assumed_stop_pct = 0.02
        deployed_usd = risk_usd / assumed_stop_pct
        counterfactual_pnl_usd = round(deployed_usd * (ret_pct / 100.0), 2)

        matured = {
            "ticket_id": t["ticket_id"],
            "symbol": t["symbol"],
            "scan_date": t["scan_date"],
            "direction": t["direction"],
            "score": t["score"],
            "anchor_at_plan_usd": anchor_at_plan,
            "risk_budget_usd": risk_usd,
            "deployed_usd_assumed": deployed_usd,
            "horizon": horizon_key,
            "exit_date": horizon_data.get("date"),
            "return_pct": ret_pct,
            "counterfactual_pnl_usd": counterfactual_pnl_usd,
            "counterfactual": True,  # hard label for dashboard / morning brief
            "math_assumptions": {
                "risk_pct_of_anchor":    0.005,
                "assumed_stop_pct":      assumed_stop_pct,
                "slippage_bps_per_side": 0,  # not modeled
                "commission_usd":        0,  # not modeled
                "notes": ("counterfactual PnL = anchor × 0.5% / 2% × ret_pct. "
                           "Not a real fill. Flip MODE to 'paper' to stop "
                           "computing this and start measuring actual PnL."),
            },
            "matured_at": datetime.now(timezone.utc).isoformat(),
            "mode": t.get("mode", MODE),
        }
        new_matured.append(matured)
        t["status"] = "matured"
        t["counterfactual_pnl_usd"] = counterfactual_pnl_usd
        updated_planned.append(t)

    # Rewrite planned.jsonl with updated statuses
    if new_matured:
        PLANNED_PATH.write_text(
            "\n".join(json.dumps(r) for r in updated_planned) + "\n",
            encoding="utf-8",
        )
        _append_jsonl(MATURED_PATH, new_matured)
    return new_matured


def summary() -> dict:
    planned = _load_jsonl(PLANNED_PATH)
    matured = _load_jsonl(MATURED_PATH)
    active = [p for p in planned if p.get("status") == "planned"]
    total_pnl = sum(m.get("counterfactual_pnl_usd") or 0 for m in matured)
    wins = sum(1 for m in matured if (m.get("counterfactual_pnl_usd") or 0) > 0)
    # Surface the counterfactual nature explicitly — every consumer should
    # SEE that this number is hypothetical, not treat it as realised PnL.
    is_counterfactual = MODE == "research_only"
    return {
        "mode": MODE,
        "is_counterfactual": is_counterfactual,
        "pnl_label": ("counterfactual_pnl_usd (hypothetical — MODE=research_only)"
                       if is_counterfactual else "realised_pnl_usd"),
        "planned_active": len(active),
        "planned_total_lifetime": len(planned),
        "matured_count": len(matured),
        "counterfactual_pnl_usd_total": round(total_pnl, 2),
        "matured_win_rate_pct": round(wins / len(matured) * 100, 1) if matured else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true", help="Only plan new tickets")
    ap.add_argument("--mature", action="store_true", help="Only mature existing tickets")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    new_planned = []
    new_matured = []
    if args.plan or not args.mature:
        new_planned = plan_new_tickets()
    if args.mature or not args.plan:
        new_matured = mature_tickets()

    result = {
        "new_planned_tickets": len(new_planned),
        "new_matured_tickets": len(new_matured),
        "summary": summary(),
    }
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        anchor = get_sizing_anchor_usd()
        s = result["summary"]
        cf_pnl = s["counterfactual_pnl_usd_total"]
        # Scale projection at a real $10K prod anchor (anchor-adjusted)
        cf_pnl_at_10k = cf_pnl * (10000 / anchor) if anchor > 0 else 0
        print(f"Apollo execution skeleton (mode: {MODE})")
        print(f"Current anchor: ${anchor:,.0f}  (for scale context)")
        print(f"New planned tickets: {result['new_planned_tickets']}")
        print(f"New matured tickets: {result['new_matured_tickets']}")
        print(f"Active planned: {s['planned_active']}")
        print(f"Matured lifetime: {s['matured_count']}  win_rate: {s['matured_win_rate_pct']}%")
        print(f"Counterfactual PnL (at current anchor, 0.5% risk): ${cf_pnl:,.2f}")
        print(f"Counterfactual PnL (projected to $10K prod anchor): ${cf_pnl_at_10k:,.2f}")
        print(f"Planned path: {PLANNED_PATH}")
        print(f"Matured path: {MATURED_PATH}")
        if MODE == "research_only":
            print("Note: no real orders submitted. Flip MODE to 'paper' once edge evidence matures.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
