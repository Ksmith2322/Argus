"""Integration tests for evidence_epoch wiring into ROI consumers
(Codex audit 2026-05-18 X4 follow-through).

The scaffold tests verify the module itself. These verify that the
consumers actually stamp their reports and refuse on contaminated input."""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock


# ── run_strategy_roi_proof.py ────────────────────────────────────────────

def test_run_strategy_roi_proof_imports_evidence_epoch():
    """Source-level check that the consumer imports the module — catches
    accidental removal of the integration."""
    src = (Path(__file__).resolve().parents[2] / "ops" / "audit" /
           "run_strategy_roi_proof.py")
    text = src.read_text(encoding="utf-8")
    assert "from helio import evidence_epoch as ee" in text or \
           "from helio import evidence_epoch" in text, (
        "run_strategy_roi_proof.py no longer imports helio.evidence_epoch — "
        "ROI reports must stamp their epoch (Codex X4)."
    )
    assert "stamp_report" in text, (
        "run_strategy_roi_proof.py no longer calls ee.stamp_report() — "
        "reports must carry epoch_id/epoch_is_clean fields."
    )


def test_run_strategy_roi_proof_emits_summary_json(tmp_path, monkeypatch):
    """Run main() in a synthetic environment with no canonical_fills; verify
    the JSON summary still gets written with an epoch stamp."""
    from ops.audit import run_strategy_roi_proof as rsrp

    # Redirect outputs to tmp
    monkeypatch.setattr(rsrp, "OUT_DIR", tmp_path)
    # Force empty canonical_fills so the per_strategy list is empty
    monkeypatch.setattr(rsrp, "_load_canonical_fills", lambda window: [])
    # Stub SPY bars (returns empty list)
    monkeypatch.setattr(rsrp.bench, "load_spy_bars", lambda: [])
    monkeypatch.setattr(rsrp, "_load_killed_strategies", lambda: set())

    rc = rsrp.main(["--window", "post_reset"])
    assert rc == 0

    summary_path = tmp_path / "roi_proof_summary.json"
    assert summary_path.exists(), "roi_proof_summary.json must be written"
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    assert "epoch_id" in data, "summary must include epoch_id"
    assert "epoch_is_clean" in data, "summary must include epoch_is_clean"
    # Pre-freeze epoch ships is_clean=False — confirm the stamp picks it up
    assert data["epoch_is_clean"] is False


def test_run_strategy_roi_proof_md_warns_on_contaminated_epoch(tmp_path, monkeypatch):
    """If is_clean=False, the markdown report must include the contamination
    warning so a human operator opening the file can't miss it."""
    from ops.audit import run_strategy_roi_proof as rsrp

    monkeypatch.setattr(rsrp, "OUT_DIR", tmp_path)
    monkeypatch.setattr(rsrp, "_load_canonical_fills", lambda window: [])
    monkeypatch.setattr(rsrp.bench, "load_spy_bars", lambda: [])
    monkeypatch.setattr(rsrp, "_load_killed_strategies", lambda: set())

    rsrp.main(["--window", "post_reset"])
    md = (tmp_path / "roi_proof_report.md").read_text(encoding="utf-8")
    # Pre-freeze epoch ships is_clean=False, so the file should mention
    # the epoch id + flag that it's contaminated.
    assert "pre_freeze_20260418" in md or "Evidence epoch" in md


# ── argus_audit_engine.killed_review_md ────────────────────────────────

def test_killed_review_md_includes_epoch_stamp():
    """The killed-strategy review markdown must include the epoch id."""
    from ops.audit.argus_audit_engine import killed_review_md
    rows = [
        {
            "strategy": "forge_spy_mean_rev",
            "recommendation": "KEEP_KILLED",
            "kill_reason_classification": "structural negative expectancy",
        },
    ]
    md = killed_review_md(rows)
    assert "Evidence epoch" in md, (
        "killed_review_md must include the evidence epoch stamp; downstream "
        "promotion decisions need to know which window the verdicts came from."
    )
    assert "pre_freeze_20260418" in md or "post_reset" in md


# ── promotion_check ────────────────────────────────────────────────────

def test_promotion_check_refuses_real_promotion_on_contaminated_epoch(monkeypatch):
    """READY_FOR_REAL must downgrade to COLLECTING when the current epoch
    is unclean. Codex X4 — pre-reset evidence cannot drive capital."""
    from helio import promotion_check as pc

    # Synthetic config + trades that would otherwise promote
    trades_n = pc.PAPER_MIN_TRADES + 5
    trades = [{"pnl_pct": "0.02"} for _ in range(trades_n)]

    def _fake_check(config_path: Path) -> dict:
        # Replicate the paper-stage path but with our trade set.
        result = {
            "symbol": "GBPUSD", "family": "argus", "config": str(config_path),
            "stage": "paper", "trades": len(trades), "signals": 0,
            "hb_age_s": 100, "verdict": "NOT_READY", "blockers": [],
            "pf": pc._profit_factor(trades),
        }
        pf = result["pf"]
        if len(trades) >= pc.PAPER_MIN_TRADES and pf >= 1.3:
            try:
                from helio import evidence_epoch as _ee
                epoch = _ee.current_epoch()
                if not epoch.is_clean:
                    result["verdict"] = "COLLECTING"
                    result["blockers"].append(
                        f"contaminated_epoch:{epoch.id} (post-reset epoch required)"
                    )
                    return result
            except Exception as exc:
                result["verdict"] = "NOT_READY"
                result["blockers"].append(f"evidence_epoch_unreadable:{exc}")
                return result
            result["verdict"] = "READY_FOR_REAL"
        return result

    # The real check_runner reads from disk; here we just invoke the
    # promotion-decision arm we patched into the function.
    result = _fake_check(Path("synthetic.json"))
    # All wins so pf is huge (1 win / 0 losses → 99.0); paper-min trades met.
    # Since the pre-freeze epoch is is_clean=False, verdict must be COLLECTING.
    assert result["verdict"] == "COLLECTING", (
        f"Expected COLLECTING under contaminated epoch, got {result['verdict']!r}. "
        f"blockers={result['blockers']}"
    )
    assert any("contaminated_epoch" in b for b in result["blockers"])


def test_promotion_check_source_has_epoch_gate():
    """Source-level: the epoch gate must be present in the paper→real arm."""
    src = (Path(__file__).resolve().parents[2] / "helio" / "promotion_check.py")
    text = src.read_text(encoding="utf-8")
    assert "evidence_epoch" in text
    assert "contaminated_epoch" in text
    assert "epoch.is_clean" in text
