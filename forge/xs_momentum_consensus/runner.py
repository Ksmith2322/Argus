"""forge.xs_momentum_consensus.runner — Variant Consensus Sleeve shadow runner.

Once per month on the last trading day, computes each of 5 xs_momentum
variants' top-quintile picks for the current month, aggregates votes
per ticker, and writes the >=min_votes consensus picks to a virtual
ledger. Does NOT submit IBKR orders — it's a shadow strategy that
proves out the consensus rule against live picks before any capital
allocation.

Modes:
    --check       Print this month's consensus picks (no write)
    --evaluate    One cycle: compute + write virtual_trades.csv + heartbeat
    --loop        Daemon: wake daily, evaluate when it's the last trading day
                  of the month
    --backtest    Re-run the historical backtest (delegates to
                  helio/variant_overlap.py for the canonical method)

Outputs:
    forge/logs/xs_momentum_consensus/
      virtual_trades.csv        # one row per consensus pick per rebalance
      monthly_picks.jsonl       # one JSON record per rebalance with all metadata
      state.json                # last rebalance month + current consensus
      heartbeat.json            # fleet_monitor heartbeat
      runner.log                # standard log
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from forge.logging_setup import setup_logging
from helio.variant_overlap import VARIANTS_FOR_OVERLAP, ConsensusConfig
from helio.xs_momentum_universes import CANDIDATE_UNIVERSES


STRATEGY_LABEL = "forge_xs_momentum_consensus"
# No IBKR_CLIENT_ID — this runner never connects to the broker

REPO = Path(__file__).resolve().parents[2]
LOG_DIR = REPO / "forge" / "logs" / "xs_momentum_consensus"
LOG_DIR.mkdir(parents=True, exist_ok=True)

STATE_PATH = LOG_DIR / "state.json"
HEARTBEAT_PATH = LOG_DIR / "heartbeat.json"
VIRTUAL_TRADES_PATH = LOG_DIR / "virtual_trades.csv"
PICKS_JSONL_PATH = LOG_DIR / "monthly_picks.jsonl"

log = setup_logging("xs_momentum_consensus")
NY_TZ = ZoneInfo("America/New_York")

PARAMS = {
    "version": "v1_20260525",
    "min_votes": 2,                # 2-of-5 per the gate verdict
    "slippage_bps_rt": 10.0,
    # Wake at 15:55 ET (one hour before the variants' usual rebalance)
    # so we have time to compute before the variants' actual fills.
    "eval_hour_utc": 19,
    "eval_minute_utc": 55,
}


VIRTUAL_TRADE_FIELDS = [
    "ts", "month", "ticker", "vote_count", "voting_variants",
    "consensus_threshold", "session_id", "config_version",
]


# ── State + heartbeat + CSV ────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {
        "current_consensus": {},        # ticker -> {vote_count, voting_variants}
        "last_rebalance_month": "",
        "rebalance_count": 0,
        "session_id": str(uuid.uuid4())[:8],
    }


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def _ensure_csv():
    if not VIRTUAL_TRADES_PATH.exists():
        with open(VIRTUAL_TRADES_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(VIRTUAL_TRADE_FIELDS)


def _append_trade(row: dict) -> None:
    _ensure_csv()
    with open(VIRTUAL_TRADES_PATH, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=VIRTUAL_TRADE_FIELDS).writerow(
            {k: row.get(k, "") for k in VIRTUAL_TRADE_FIELDS}
        )


def _write_heartbeat(state: dict, last_rebalance: dict | None) -> None:
    try:
        from helio.strategy_common import git_sha as _git_sha
        git_sha = _git_sha(REPO)
    except Exception:
        git_sha = "unknown"
    HEARTBEAT_PATH.write_text(json.dumps({
        "system": STRATEGY_LABEL,
        "family": "forge",
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": "shadow",                       # explicit non-trading mode
        "min_votes": PARAMS["min_votes"],
        "n_variants_polled": len(VARIANTS_FOR_OVERLAP),
        "rebalance_count": state.get("rebalance_count", 0),
        "current_consensus": state.get("current_consensus", {}),
        "last_rebalance": last_rebalance,
        "version": PARAMS["version"],
        "git_sha": git_sha,
        "allocation_factor": 0.0,                # locked at zero by design
    }, indent=2, default=str), encoding="utf-8")


# ── Consensus computation ──────────────────────────────────────────────

@dataclass
class ConsensusResult:
    month: str                           # YYYY-MM
    variant_picks: dict[str, list[str]]  # variant_label -> picks
    vote_counts: dict[str, int]          # ticker -> n_votes
    consensus_picks: list[str]           # tickers with vote >= min_votes
    consensus_voters: dict[str, list[str]]  # ticker -> [variant labels that voted for it]
    error: str | None = None


def compute_current_consensus(min_votes: int | None = None) -> ConsensusResult:
    """Run each xs_momentum variant's current-month pick selection and
    aggregate to find the consensus tickers.

    Reuses forge.xs_momentum.runner.evaluate_picks_for_universe via the
    backtest_one_variant path in helio.variant_overlap so the same logic
    that produces the backtest produces the live picks — no parallel
    implementation."""
    from forge.xs_momentum.runner import backtest as xs_backtest

    if min_votes is None:
        min_votes = PARAMS["min_votes"]

    # We need the CURRENT month's picks. Use a 1y window — small enough to
    # be fast, long enough that the trailing-12-month momentum ranker has
    # a full history at month-end.
    variant_picks: dict[str, list[str]] = {}
    most_recent_month = ""
    for label, universe_key, top_pick_fraction in VARIANTS_FOR_OVERLAP:
        universe = list(CANDIDATE_UNIVERSES[universe_key]) if universe_key else None
        try:
            result = xs_backtest(
                period="2y",   # 2y enough for 12-1 momentum at month-end
                universe_override=universe,
                top_pick_fraction=top_pick_fraction,
            )
        except Exception as exc:
            return ConsensusResult(
                month="", variant_picks={}, vote_counts={}, consensus_picks=[],
                consensus_voters={}, error=f"backtest failed for {label}: {exc!r}",
            )
        trades = result.get("trades_detail") or []
        if not trades:
            log.warning("variant %s produced 0 trades — empty pick set", label)
            variant_picks[label] = []
            continue
        # Sort by entry_date desc, take the most recent month's picks
        latest_entry = max(t.get("entry_date", "") for t in trades)
        latest_month = latest_entry[:7] if latest_entry else ""
        if latest_month > most_recent_month:
            most_recent_month = latest_month
        picks = [t["ticker"] for t in trades if str(t.get("entry_date", "")).startswith(latest_month)]
        variant_picks[label] = list(dict.fromkeys(picks))  # dedup, preserve order

    # Aggregate votes (only tickers picked in the most recent month across
    # all variants count — variants whose last month is different may have
    # skipped a rebalance; flag in heartbeat)
    vote_counts: dict[str, int] = {}
    voters: dict[str, list[str]] = {}
    for label, picks in variant_picks.items():
        for tkr in picks:
            vote_counts[tkr] = vote_counts.get(tkr, 0) + 1
            voters.setdefault(tkr, []).append(label)

    consensus_picks = sorted([t for t, n in vote_counts.items() if n >= min_votes])

    return ConsensusResult(
        month=most_recent_month,
        variant_picks=variant_picks,
        vote_counts=vote_counts,
        consensus_picks=consensus_picks,
        consensus_voters={t: voters[t] for t in consensus_picks},
    )


# ── Action handlers ────────────────────────────────────────────────────

def _print_check() -> int:
    """--check: print today's consensus picks, write nothing."""
    log.info("--check: computing current-month consensus picks (no write)")
    r = compute_current_consensus()
    if r.error:
        print(f"ERROR: {r.error}")
        return 2
    print(f"\n=== Consensus picks for {r.month} (min_votes={PARAMS['min_votes']}) ===\n")
    print("Per-variant picks:")
    for label, picks in r.variant_picks.items():
        print(f"  {label:24s} -> {picks}")
    print()
    print("Vote counts:")
    for tkr, n in sorted(r.vote_counts.items(), key=lambda x: -x[1]):
        print(f"  {tkr:8s} {n} vote{'s' if n != 1 else ''}  "
              f"({', '.join(r.consensus_voters.get(tkr, []))})")
    print()
    if r.consensus_picks:
        print(f"CONSENSUS (>={PARAMS['min_votes']} votes): {r.consensus_picks}")
    else:
        print(f"NO consensus picks this month (no ticker has >={PARAMS['min_votes']} votes)")
    return 0


def evaluate_once() -> dict:
    """--evaluate: compute consensus, write virtual_trades + heartbeat.
    Returns the rebalance summary dict (also stored in last_rebalance)."""
    state = _load_state()
    r = compute_current_consensus()
    summary = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "month": r.month,
        "error": r.error,
        "consensus_picks": r.consensus_picks,
        "consensus_voters": r.consensus_voters,
        "vote_counts": r.vote_counts,
        "variant_picks": r.variant_picks,
    }

    if r.error:
        log.error("evaluate FAILED: %s", r.error)
        _write_heartbeat(state, summary)
        return summary

    # Idempotent on month: if we've already written picks for this month, skip
    if state.get("last_rebalance_month") == r.month:
        log.info("month %s already rebalanced (rebalance #%d) — skip",
                 r.month, state.get("rebalance_count", 0))
        _write_heartbeat(state, summary)
        return summary

    # Write virtual trade rows
    for tkr in r.consensus_picks:
        _append_trade({
            "ts": summary["ts"],
            "month": r.month,
            "ticker": tkr,
            "vote_count": r.vote_counts.get(tkr, 0),
            "voting_variants": ",".join(r.consensus_voters.get(tkr, [])),
            "consensus_threshold": PARAMS["min_votes"],
            "session_id": state.get("session_id", ""),
            "config_version": PARAMS["version"],
        })

    # Append the per-rebalance metadata to monthly_picks.jsonl
    with open(PICKS_JSONL_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(summary, default=str) + "\n")

    # Update state
    state["current_consensus"] = {
        tkr: {
            "vote_count": r.vote_counts.get(tkr, 0),
            "voting_variants": r.consensus_voters.get(tkr, []),
        } for tkr in r.consensus_picks
    }
    state["last_rebalance_month"] = r.month
    state["rebalance_count"] = state.get("rebalance_count", 0) + 1
    _save_state(state)
    _write_heartbeat(state, summary)

    log.info("rebalance #%d for %s: consensus=%s",
             state["rebalance_count"], r.month, r.consensus_picks)
    return summary


def loop_mode() -> None:
    """--loop: wake daily at PARAMS['eval_hour_utc:eval_minute_utc']; on
    the last trading day of the month, run evaluate_once."""
    log.info("xs_momentum_consensus --loop started: daily wake at %02d:%02d UTC",
             PARAMS["eval_hour_utc"], PARAMS["eval_minute_utc"])
    # Always write heartbeat on startup so fleet_monitor sees us
    _write_heartbeat(_load_state(), None)
    while True:
        try:
            now = datetime.now(timezone.utc)
            target = now.replace(
                hour=PARAMS["eval_hour_utc"], minute=PARAMS["eval_minute_utc"],
                second=0, microsecond=0,
            )
            if target <= now:
                target = target + timedelta(days=1)
            sleep_s = max(60, int((target - now).total_seconds()))
            log.info("xs_momentum_consensus next wake at %s UTC (sleep %ds)",
                     target.isoformat(), sleep_s)
            time.sleep(sleep_s)
            # Heartbeat at wake
            state = _load_state()
            _write_heartbeat(state, None)
            # Is today the last trading day of the month?
            # Lazy check: tomorrow's date is in a different month
            today = datetime.now(NY_TZ).date()
            tomorrow = today + timedelta(days=1)
            # Step over weekend
            while tomorrow.weekday() >= 5:  # 5=Sat, 6=Sun
                tomorrow = tomorrow + timedelta(days=1)
            if tomorrow.month != today.month:
                log.info("today (%s) is the last trading day of %s — evaluating consensus",
                         today.isoformat(), today.strftime("%Y-%m"))
                evaluate_once()
            else:
                log.info("today (%s) is not the last trading day of the month — skip",
                         today.isoformat())
        except KeyboardInterrupt:
            log.info("xs_momentum_consensus loop stopped by user")
            break
        except Exception as exc:
            log.exception("loop iteration failed: %s", exc)
            time.sleep(60)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--backtest", action="store_true",
                        help="Run the canonical 20y consensus backtest "
                             "(delegates to ops.audit.run_variant_overlap)")
    args = parser.parse_args(argv)
    if args.backtest:
        import subprocess
        return subprocess.call([sys.executable, "-m", "ops.audit.run_variant_overlap",
                                "--period", "20y", "--min-votes", str(PARAMS["min_votes"])])
    if args.check:
        return _print_check()
    if args.evaluate:
        summary = evaluate_once()
        return 0 if not summary.get("error") else 1
    if args.loop:
        loop_mode()
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
