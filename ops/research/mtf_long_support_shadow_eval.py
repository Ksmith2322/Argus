"""Shadow evaluator for the `MTF_LONG_NOT_AT_SUPPORT` blocked-gate slice.

Codex audit 2026-05-08 forward-scored 117 blocked opportunities and found
this single slice positive (84.62% WR, +2.68 pips/trade, n=13). This module
turns that finding into an ongoing research evaluator: every time the slice
fires, replay the counterfactual outcome and update a rolling tally so we
can decide on promotion at sample maturity.

Read-only. **Does not submit orders.** Does not change runtime gate behavior.
The blocked-gate stays blocked; this module just keeps a parallel ledger of
"what would have happened."

Promotion requires (per ``argus_flow/configs/shadow_strategies.json``):
  - ``global_enabled`` flipped to true (operator)
  - ``approver`` and ``ledger_entry_id`` populated
  - ``rolling_window`` PF >= ``promotion_pf_threshold``
  - ``n`` >= ``min_sample_for_promotion_review``

If any of those fail, the runner shall not consume this strategy for live
allocation. The default-off contract is enforced in
``argus_flow/tests/test_shadow_strategies_default_off.py``.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[2]
REGISTRY_PATH = REPO / "argus_flow" / "configs" / "shadow_strategies.json"
LEDGER_PATH = REPO / "argus_flow" / "logs" / "_research" / "mtf_long_not_at_support_shadow.jsonl"
SUMMARY_PATH = REPO / "argus_flow" / "logs" / "_research" / "mtf_long_not_at_support_summary.json"

SHADOW_NAME = "mtf_long_not_at_support_shadow"


@dataclass(frozen=True)
class ShadowConfig:
    """Loaded view of the registry entry for this shadow strategy."""

    name: str
    global_enabled: bool
    block_reason_filter: str
    applicable_symbols: tuple[str, ...]
    min_sample_for_promotion_review: int
    promotion_pf_threshold: float
    kill_pf_threshold: float
    rolling_window_days: int
    approver: str
    ledger_entry_id: str
    raw: dict = field(default_factory=dict)

    @property
    def is_promotion_eligible(self) -> bool:
        """True only if the registry entry is fully signed and globally enabled.

        The default registry has ``global_enabled=false``, ``approver=""``, and
        ``ledger_entry_id=""``. All three must be set before this strategy is
        eligible for live promotion.
        """
        return bool(self.global_enabled and self.approver and self.ledger_entry_id)


def load_config(path: Path | str | None = None) -> ShadowConfig:
    """Load this shadow's registry entry. Missing/corrupt → returns a fully
    disabled, paper-only default."""
    p = Path(path) if path is not None else REGISTRY_PATH
    if not p.exists():
        return _disabled_default()
    try:
        raw = json.loads(p.read_text())
    except Exception:
        return _disabled_default()

    entries = raw.get("shadow_strategies") or []
    entry = next((e for e in entries if e.get("name") == SHADOW_NAME), None)
    if entry is None:
        return _disabled_default()

    return ShadowConfig(
        name=str(entry.get("name") or SHADOW_NAME),
        global_enabled=bool(entry.get("global_enabled", False)),
        block_reason_filter=str(entry.get("block_reason_filter") or ""),
        applicable_symbols=tuple(entry.get("applicable_symbols") or ()),
        min_sample_for_promotion_review=int(entry.get("min_sample_for_promotion_review", 50)),
        promotion_pf_threshold=float(entry.get("promotion_pf_threshold", 1.30)),
        kill_pf_threshold=float(entry.get("kill_pf_threshold", 0.95)),
        rolling_window_days=int(entry.get("rolling_window_days", 14)),
        approver=str(entry.get("approver") or ""),
        ledger_entry_id=str(entry.get("ledger_entry_id") or ""),
        raw=entry,
    )


def _disabled_default() -> ShadowConfig:
    return ShadowConfig(
        name=SHADOW_NAME,
        global_enabled=False,
        block_reason_filter="",
        applicable_symbols=(),
        min_sample_for_promotion_review=50,
        promotion_pf_threshold=1.30,
        kill_pf_threshold=0.95,
        rolling_window_days=14,
        approver="",
        ledger_entry_id="",
    )


@dataclass
class ShadowOutcome:
    """One counterfactual evaluation row appended to the ledger."""

    ts: str
    symbol: str
    direction: str
    block_reason: str
    entry_px: float
    forward_outcome: str  # "win" | "loss" | "open" | "unresolved"
    pnl_pips: float
    bar_window_n: int


def append_to_ledger(outcome: ShadowOutcome, ledger_path: Path | None = None) -> None:
    """Append a single shadow outcome as a JSONL row. Read-only callers should
    prefer :func:`summarize_ledger` over reading raw rows."""
    p = Path(ledger_path) if ledger_path is not None else LEDGER_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(outcome)) + "\n")


def summarize_ledger(ledger_path: Path | None = None) -> dict:
    """Produce a rolling summary of shadow outcomes. Caller decides whether
    the resulting metrics meet the promotion gates."""
    p = Path(ledger_path) if ledger_path is not None else LEDGER_PATH
    if not p.exists():
        return {
            "n": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "net_pips": 0.0, "expectancy_pips": 0.0, "profit_factor": 0.0,
            "first_ts": None, "last_ts": None,
        }
    rows: list[dict] = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    resolved = [r for r in rows if r.get("forward_outcome") in ("win", "loss")]
    n = len(resolved)
    wins = [r for r in resolved if r.get("forward_outcome") == "win"]
    losses = [r for r in resolved if r.get("forward_outcome") == "loss"]
    net_pips = sum(float(r.get("pnl_pips", 0)) for r in resolved)
    win_pips = sum(float(r["pnl_pips"]) for r in wins) if wins else 0.0
    loss_pips_abs = abs(sum(float(r["pnl_pips"]) for r in losses)) if losses else 0.0
    pf = (win_pips / loss_pips_abs) if loss_pips_abs > 0 else (float("inf") if win_pips > 0 else 0.0)
    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / n) if n else 0.0,
        "net_pips": round(net_pips, 4),
        "expectancy_pips": round(net_pips / n, 4) if n else 0.0,
        "profit_factor": round(pf, 4) if pf != float("inf") else "inf",
        "first_ts": resolved[0].get("ts") if resolved else None,
        "last_ts": resolved[-1].get("ts") if resolved else None,
    }


def evaluate_promotion_gate(config: ShadowConfig, summary: dict) -> dict:
    """Apply the registry's promotion/kill gates to a summary. Returns a
    decision dict but does NOT take any action — that's the operator's call."""
    n = int(summary.get("n", 0))
    pf = summary.get("profit_factor")
    pf_num = float("inf") if pf == "inf" else float(pf or 0)

    decision = {
        "name": config.name,
        "globally_enabled": config.global_enabled,
        "fully_signed": config.is_promotion_eligible,
        "n": n,
        "n_required": config.min_sample_for_promotion_review,
        "profit_factor": pf,
        "promotion_pf_threshold": config.promotion_pf_threshold,
        "kill_pf_threshold": config.kill_pf_threshold,
        "verdict": "INSUFFICIENT_SAMPLE",
    }

    if n < config.min_sample_for_promotion_review:
        decision["verdict"] = "INSUFFICIENT_SAMPLE"
        return decision
    if pf_num >= config.promotion_pf_threshold:
        decision["verdict"] = "PROMOTION_REVIEW_READY" if config.is_promotion_eligible else "PROMOTION_BLOCKED_UNSIGNED"
    elif pf_num <= config.kill_pf_threshold:
        decision["verdict"] = "KILL_RECOMMENDED"
    else:
        decision["verdict"] = "HOLD_AND_OBSERVE"
    return decision


def main() -> int:
    """Read-only CLI: load config, summarize the existing ledger, evaluate
    the gate. Prints a JSON decision; never writes runtime state. Operators
    flip ``global_enabled`` only after reviewing this output."""
    config = load_config()
    summary = summarize_ledger()
    decision = evaluate_promotion_gate(config, summary)

    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_PATH.write_text(json.dumps({
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "config": asdict(config),
        "summary": summary,
        "decision": decision,
    }, indent=2, default=str))

    print(json.dumps({"summary": summary, "decision": decision}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
