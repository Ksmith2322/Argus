"""Run the replay harness for active strategies and diff against
canonical_fills.

Reads canonical_fills.jsonl, replays the strategy logic for the same
window, and reports mismatches:

  REPLAY_ONLY      — strategy logic says fire, ledger has no fill
  LEDGER_ONLY      — ledger has fill, strategy logic predicts none
  TICKER_DIVERGENT — same time, different instrument
  TIME_DIVERGENT   — same instrument, time delta > tolerance

The audit is honest when canonical_fills is empty (e.g. fresh
post-reset state): it reports the replay events but counts everything
as REPLAY_ONLY pending live evidence.

Exit codes:
  0 — no mismatches (clean) OR ledger empty (no evidence to diff yet)
  1 — REPLAY_ONLY or TIME_DIVERGENT mismatches present (review needed)
  2 — LEDGER_ONLY or TICKER_DIVERGENT mismatches present (real bug
      signal — fills exist that strategy logic doesn't predict)

USAGE
-----
    python -m ops.audit.run_replay_audit
    python -m ops.audit.run_replay_audit --strategy forge_xs_momentum_style
    python -m ops.audit.run_replay_audit --period 60d --json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


# Strategies we know how to replay
REPLAYABLE_STRATEGIES = {
    "forge_xs_momentum":              ("xs_momentum", None),
    "forge_xs_momentum_sectors":      ("xs_momentum", None),
    "forge_xs_momentum_style":        ("xs_momentum", None),
    "forge_xs_momentum_legacy15":     ("xs_momentum", None),
    "forge_xs_momentum_style_top3":   ("xs_momentum", None),
    "forge_gld_pm_long":              ("gld_pm_long", "GLD"),
}


def _run_one(strategy_label: str, *, period: str) -> dict:
    """Replay one strategy + diff against canonical_fills."""
    from helio.replay_harness import (
        replay_xs_momentum,
        replay_gld_pm_long,
        diff_against_canonical_fills,
    )
    family, extra = REPLAYABLE_STRATEGIES.get(
        strategy_label, (None, None)
    )
    if family is None:
        return {"strategy": strategy_label,
                "error": f"no replay implementation for {strategy_label}"}
    try:
        if family == "xs_momentum":
            replay = replay_xs_momentum(strategy_label, period=period)
        elif family == "gld_pm_long":
            replay = replay_gld_pm_long(period=period, ticker=extra or "GLD")
        else:
            return {"strategy": strategy_label,
                    "error": f"unknown family {family}"}
    except Exception as exc:
        return {"strategy": strategy_label,
                "error": f"replay failed: {exc}"}

    diff = diff_against_canonical_fills(replay, strategy=strategy_label)
    diff["family"] = family
    return diff


def _render_markdown(rows: list[dict]) -> str:
    out = [
        "# Replay harness audit",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
    ]
    out.append("| Strategy | Replay events | Ledger fills | Matched | Mismatches |")
    out.append("|---|---|---|---|---|")
    for r in rows:
        if r.get("error"):
            out.append(f"| {r['strategy']} | ERROR: {r['error']} | — | — | — |")
            continue
        out.append(
            f"| {r['strategy']} | "
            f"{r['n_replay_events']} | "
            f"{r['n_ledger_fills']} | "
            f"{r['n_matched']} | "
            f"{r['n_mismatches']} |"
        )
    out.append("")

    for r in rows:
        if r.get("error") or not r.get("n_mismatches"):
            continue
        out.append(f"## {r['strategy']} — {r['n_mismatches']} mismatch(es)")
        out.append("")
        out.append(f"  counts: {r['mismatch_counts']}")
        out.append("")
        for m in r["mismatches"][:20]:  # cap detail to first 20
            out.append(f"- **{m['kind']}** @ {m.get('ts')}: {m['detail'][:120]}")
        if len(r["mismatches"]) > 20:
            out.append(f"  ... +{len(r['mismatches']) - 20} more")
        out.append("")
    return "\n".join(out)


def _aggregate_exit(rows: list[dict]) -> int:
    """Map mismatch kinds to exit codes. Severity:
    0 — clean / empty ledger / only matched
    1 — REPLAY_ONLY or TIME_DIVERGENT (review)
    2 — LEDGER_ONLY or TICKER_DIVERGENT (real bug signal)
    """
    worst = 0
    for r in rows:
        if r.get("error"):
            worst = max(worst, 1)
            continue
        counts = r.get("mismatch_counts", {})
        if counts.get("LEDGER_ONLY", 0) or counts.get("TICKER_DIVERGENT", 0):
            return 2
        if counts.get("REPLAY_ONLY", 0) or counts.get("TIME_DIVERGENT", 0):
            worst = max(worst, 1)
    return worst


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", default=None,
                        help="Replay only this strategy_label (default: all replayable)")
    parser.add_argument("--period", default="60d",
                        help="Lookback window (default: 60d)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.strategy:
        if args.strategy not in REPLAYABLE_STRATEGIES:
            print(f"ERROR: unknown strategy {args.strategy!r}; "
                  f"known: {list(REPLAYABLE_STRATEGIES)}")
            return 2
        strategies = [args.strategy]
    else:
        strategies = list(REPLAYABLE_STRATEGIES)

    rows: list[dict] = []
    for s in strategies:
        print(f"  replaying {s} ({args.period})...", file=sys.stderr,
              end=" ", flush=True)
        r = _run_one(s, period=args.period)
        rows.append(r)
        if r.get("error"):
            print(f"ERR {r['error']}", file=sys.stderr)
        else:
            print(
                f"replay={r['n_replay_events']} ledger={r['n_ledger_fills']} "
                f"mismatches={r['n_mismatches']}",
                file=sys.stderr,
            )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "replay_audit.json").write_text(
        json.dumps(rows, indent=2, default=str), encoding="utf-8")

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
    else:
        print(_render_markdown(rows))

    return _aggregate_exit(rows)


if __name__ == "__main__":
    raise SystemExit(main())
