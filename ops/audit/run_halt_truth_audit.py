"""Emit a read-only halt truth reconciliation report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from helio.halt_state import get_halt_state

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit reconciled halt truth")
    parser.parse_args()
    state = get_halt_state()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = state.to_dict()
    (OUT_DIR / "halt_truth_reconciliation.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Halt Truth Reconciliation",
        "",
        f"- Halted: {state.halted}",
        f"- Sources: {', '.join(state.sources) if state.sources else 'none'}",
        f"- Reason: {state.reason or 'none'}",
        f"- HALT.flag present: {state.halt_flag_present}",
        f"- FLATTEN_EOD.flag present: {state.flatten_flag_present}",
        f"- Broker drift tripped: {state.broker_drift_tripped}",
        f"- Broker drift state timestamp: {state.broker_drift_state_ts or 'missing'}",
        "",
        "Runtime execution now reads the reconciled halt state through `helio.halt_state`.",
        "If any source is tripped, new entries fail closed.",
    ]
    (OUT_DIR / "halt_truth_reconciliation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"halted": state.halted, "sources": list(state.sources)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
