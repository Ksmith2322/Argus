"""Verify all production data-feed contracts (Codex gap #1).

Each daily-bar strategy declares an explicit contract in
`helio.data_feed_contract.CONTRACTS` covering primary source, fallback
cache location, required universe, and freshness budget. This CLI runs
the offline check (cache only, no network) and reports per-strategy
GREEN/YELLOW/RED.

Exit codes:
  0 — all contracts GREEN
  1 — at least one YELLOW (degraded, but at least partly usable)
  2 — at least one RED (live outage would leave us blind)

Output:
  - markdown summary to stdout
  - JSON sidecar to ops/reports/system_audit/data_feed_contracts.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


def _render_markdown(results: dict[str, dict]) -> str:
    out = ["# Data-feed contract verification",
           "",
           f"Generated: {datetime.now(timezone.utc).isoformat()}",
           ""]
    counts = {"GREEN": 0, "YELLOW": 0, "RED": 0}
    for r in results.values():
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    out.append(
        f"GREEN={counts['GREEN']}  "
        f"YELLOW={counts['YELLOW']}  "
        f"RED={counts['RED']}"
    )
    out.append("")
    out.append("| Strategy | Verdict | Present | Missing | Stale | Reasons |")
    out.append("|---|---|---|---|---|---|")
    for name, r in results.items():
        out.append(
            f"| {name} | {r['verdict']} | "
            f"{len(r['universe_present'])}/{len(r['universe_required'])} | "
            f"{len(r['universe_missing'])} | "
            f"{len(r['stale_tickers'])} | "
            f"{'; '.join(r['reasons']) or '(clean)'} |"
        )
    out.append("")
    for name, r in results.items():
        if r["universe_missing"] or r["stale_tickers"]:
            out.append(f"### {name}")
            if r["universe_missing"]:
                out.append(f"- Missing: {', '.join(r['universe_missing'])}")
            if r["stale_tickers"]:
                stale_str = ", ".join(
                    f"{x['ticker']} ({x['age_days']}d)"
                    for x in r["stale_tickers"]
                )
                out.append(
                    f"- Stale (> {r['max_age_days']}d): {stale_str}"
                )
            out.append("")
    return "\n".join(out)


def _exit_code(results: dict[str, dict]) -> int:
    has_red = any(r["verdict"] == "RED" for r in results.values())
    has_yellow = any(r["verdict"] == "YELLOW" for r in results.values())
    if has_red:
        return 2
    if has_yellow:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON to stdout instead of markdown")
    args = parser.parse_args(argv)

    from helio.data_feed_contract import verify_all_contracts
    results = verify_all_contracts()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUT_DIR / "data_feed_contracts.json"
    json_path.write_text(
        json.dumps(results, indent=2, default=str),
        encoding="utf-8",
    )

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print(_render_markdown(results))
        print(f"\nPersisted: {json_path}")

    return _exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
