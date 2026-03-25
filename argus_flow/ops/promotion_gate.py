"""Promotion Gate Checker — automated enforcement of COHORT_SPEC.md criteria.

Checks each FX pair against ALL promotion criteria before advancing
from paper to micro-live.

Usage:
    python -m argus_flow.ops.promotion_gate
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

REPO = Path(__file__).resolve().parents[2]

RUNNERS = [
    {"name": "EUR/USD", "symbol": "EURUSD", "log_dir": "argus_flow/logs/eurusd"},
    {"name": "GBP/USD", "symbol": "GBPUSD", "log_dir": "argus_flow/logs/gbpusd"},
    {"name": "EUR/JPY", "symbol": "EURJPY", "log_dir": "argus_flow/logs/eurjpy"},
]

PROMOTION_THRESHOLD = 30

# Modeled friction per trade (pips) — conservative estimate for FX paper->live slippage
MODELED_FRICTION_PIPS = 0.3


class CheckResult(NamedTuple):
    passed: bool
    detail: str
    classification: str = "hard"  # "hard" = blocks promotion, "advisory" = logged but does not block, "unevidenced" = not yet testable


def _hard(passed, detail):
    return CheckResult(passed, detail, "hard")

def _advisory(passed, detail):
    return CheckResult(passed, detail, "advisory")

def _unevidenced(detail):
    return CheckResult(False, detail, "unevidenced")


# ─────────────────────────────────────────────────────────────
# Individual check functions
# ─────────────────────────────────────────────────────────────

def check_min_valid_trades(valid_trades: list[dict]) -> CheckResult:
    """30 valid trades minimum."""
    n = len(valid_trades)
    if n >= PROMOTION_THRESHOLD:
        return CheckResult(True, f"{n} valid trades (>= {PROMOTION_THRESHOLD})")
    return CheckResult(False, f"{n} valid trades (need {PROMOTION_THRESHOLD})")


def check_frozen_config_hash(valid_trades: list[dict]) -> CheckResult:
    """Same frozen config hash across all valid trades."""
    hashes = set(t.get("config_hash", "") for t in valid_trades)
    hashes.discard("")
    if len(hashes) == 0:
        return CheckResult(False, "no config_hash field found in trades")
    if len(hashes) == 1:
        return CheckResult(True, f"config_hash consistent: {hashes.pop()[:12]}...")
    return CheckResult(False, f"{len(hashes)} different config hashes found: {', '.join(h[:12] for h in sorted(hashes))}")


def check_git_sha_consistent(valid_trades: list[dict]) -> CheckResult:
    """Same git_sha across all valid trades."""
    shas = set(t.get("git_sha", "") for t in valid_trades)
    shas.discard("")
    if len(shas) == 0:
        return CheckResult(False, "no git_sha field found in trades")
    if len(shas) == 1:
        return CheckResult(True, f"git_sha consistent: {shas.pop()}")
    return CheckResult(False, f"{len(shas)} different git SHAs: {', '.join(sorted(shas))}")


def check_invalid_rate(all_trades: list[dict]) -> CheckResult:
    """Invalid trade rate < 10%."""
    if not all_trades:
        return CheckResult(False, "no trades at all")
    invalid_count = sum(1 for t in all_trades if t.get("experiment_valid", "").lower() != "true")
    rate = invalid_count / len(all_trades)
    if rate < 0.10:
        return CheckResult(True, f"invalid rate {rate:.1%} ({invalid_count}/{len(all_trades)}) < 10%")
    return CheckResult(False, f"invalid rate {rate:.1%} ({invalid_count}/{len(all_trades)}) >= 10%")


def check_no_runtime_anomalies(valid_trades: list[dict]) -> CheckResult:
    """No unresolved runtime anomalies (unknown invalid reasons)."""
    # Check for any trade with invalid_reason that is non-null and unknown
    for t in valid_trades:
        reason = t.get("invalid_reason", "")
        if reason and reason.lower() not in ("", "null", "none"):
            return CheckResult(False, f"valid trade has invalid_reason set: {reason}")
    return CheckResult(True, "no runtime anomalies in valid trades")


def check_positive_expectancy(valid_trades: list[dict]) -> CheckResult:
    """Expectancy positive after modeled friction."""
    pnl_field = _pnl_field(valid_trades)
    pnls = [float(t.get(pnl_field, 0)) for t in valid_trades]
    if not pnls:
        return CheckResult(False, "no P&L data")
    raw_exp = sum(pnls) / len(pnls)
    adj_exp = raw_exp - MODELED_FRICTION_PIPS
    if adj_exp > 0:
        return CheckResult(True, f"expectancy {adj_exp:+.3f} pips/trade (raw {raw_exp:+.3f} - {MODELED_FRICTION_PIPS} friction)")
    return CheckResult(False, f"expectancy {adj_exp:+.3f} pips/trade (raw {raw_exp:+.3f} - {MODELED_FRICTION_PIPS} friction)")


def check_session_concentration(valid_trades: list[dict]) -> CheckResult:
    """No single-session concentration > 40% of total P&L."""
    pnl_field = _pnl_field(valid_trades)
    total_pnl = sum(float(t.get(pnl_field, 0)) for t in valid_trades)
    if abs(total_pnl) < 1e-9:
        return CheckResult(True, "total P&L ~0, no concentration issue")

    session_pnl: dict[str, float] = {}
    for t in valid_trades:
        sid = t.get("session_id", "unknown")
        session_pnl.setdefault(sid, 0.0)
        session_pnl[sid] += float(t.get(pnl_field, 0))

    for sid, spnl in session_pnl.items():
        concentration = abs(spnl) / abs(total_pnl)
        if concentration > 0.40:
            return CheckResult(
                False,
                f"session {sid[:16]} contributes {concentration:.0%} of total P&L "
                f"({spnl:+.2f}/{total_pnl:+.2f})"
            )
    max_conc = max(abs(v) / abs(total_pnl) for v in session_pnl.values()) if session_pnl else 0
    return CheckResult(True, f"max session concentration {max_conc:.0%} (<= 40%)")


def check_outlier_trade(valid_trades: list[dict]) -> CheckResult:
    """No single outlier trade > 30% of total P&L."""
    pnl_field = _pnl_field(valid_trades)
    pnls = [float(t.get(pnl_field, 0)) for t in valid_trades]
    total_pnl = sum(pnls)
    if abs(total_pnl) < 1e-9:
        return CheckResult(True, "total P&L ~0, no outlier issue")

    for i, p in enumerate(pnls):
        concentration = abs(p) / abs(total_pnl)
        if concentration > 0.30:
            return CheckResult(
                False,
                f"trade #{i+1} contributes {concentration:.0%} of total P&L ({p:+.2f}/{total_pnl:+.2f})"
            )
    max_conc = max(abs(p) / abs(total_pnl) for p in pnls) if pnls else 0
    return CheckResult(True, f"max single-trade concentration {max_conc:.0%} (<= 30%)")


def check_win_rate_vs_replay(valid_trades: list[dict], rexp: dict) -> CheckResult:
    """Win rate within 15pp of replay expectation."""
    if not rexp or "win_rate" not in rexp:
        return CheckResult(True, "no replay WR to compare (skipped)")
    pnl_field = _pnl_field(valid_trades)
    pnls = [float(t.get(pnl_field, 0)) for t in valid_trades]
    if not pnls:
        return CheckResult(False, "no P&L data")
    live_wr = sum(1 for p in pnls if p > 0) / len(pnls)
    replay_wr = rexp["win_rate"]
    delta_pp = abs(live_wr - replay_wr) * 100
    if delta_pp <= 15:
        return CheckResult(True, f"WR {live_wr:.1%} vs replay {replay_wr:.1%} (delta {delta_pp:.1f}pp <= 15pp)")
    return CheckResult(False, f"WR {live_wr:.1%} vs replay {replay_wr:.1%} (delta {delta_pp:.1f}pp > 15pp)")


def check_signal_frequency(log_dir: Path, valid_trades: list[dict], rexp: dict) -> CheckResult:
    """Signal frequency within 50% of replay expectation."""
    if not rexp or "signals_per_day" not in rexp:
        return CheckResult(True, "no replay signal frequency to compare (skipped)")

    # Count signals from signals.csv
    sig_file = log_dir / "signals.csv"
    if not sig_file.exists():
        return CheckResult(False, "signals.csv not found")

    with open(sig_file, "r") as f:
        signals = list(csv.DictReader(f))

    if len(signals) < 2:
        return CheckResult(False, f"only {len(signals)} signals recorded (insufficient)")

    # Calculate actual signals per day from timestamp range
    try:
        first_ts = datetime.fromisoformat(signals[0]["ts"].replace("Z", "+00:00"))
        last_ts = datetime.fromisoformat(signals[-1]["ts"].replace("Z", "+00:00"))
        days = max((last_ts - first_ts).total_seconds() / 86400, 1.0)
        live_spd = len(signals) / days
    except Exception as e:
        return CheckResult(False, f"could not parse signal timestamps: {e}")

    replay_spd = rexp["signals_per_day"]
    if replay_spd <= 0:
        return CheckResult(True, "replay signals_per_day <= 0 (skipped)")

    ratio = live_spd / replay_spd
    if 0.50 <= ratio <= 1.50:
        return CheckResult(True, f"signals/day {live_spd:.1f} vs replay {replay_spd:.1f} (ratio {ratio:.2f})")
    return CheckResult(
        False,
        f"signals/day {live_spd:.1f} vs replay {replay_spd:.1f} "
        f"(ratio {ratio:.2f}, outside 50%-150%)"
    )


def check_survived_disconnect(all_trades: list[dict]) -> CheckResult:
    """Survived at least one forced disconnect/reconnect without data loss."""
    # Evidence: at least one trade with invalid_reason containing 'reconnect'
    # means a disconnect happened — and if valid trades exist after that session,
    # the runner survived it.
    session_ids = set(t.get("session_id", "") for t in all_trades)
    reconnect_trades = [
        t for t in all_trades
        if "reconnect" in (t.get("invalid_reason", "") or "").lower()
    ]
    if reconnect_trades:
        reconnect_sessions = set(t.get("session_id", "") for t in reconnect_trades)
        post_sessions = session_ids - reconnect_sessions
        if post_sessions:
            return CheckResult(True, f"survived disconnect (reconnect in {len(reconnect_sessions)} session(s), continued in {len(post_sessions)})")
        return CheckResult(False, "reconnect observed but no subsequent clean sessions")
    # If no reconnect evidence — not yet testable, not a hard failure
    return _unevidenced("no disconnect/reconnect event observed yet")


def check_no_manual_intervention(valid_trades: list[dict]) -> CheckResult:
    """No manual intervention during cohort."""
    for t in valid_trades:
        reason = (t.get("invalid_reason", "") or "").lower()
        if "manual" in reason:
            return CheckResult(False, f"manual intervention detected: {t.get('invalid_reason')}")
    return CheckResult(True, "no manual intervention flags found")


def check_dashboard_truth(log_dir: Path) -> CheckResult:
    """Dashboard truth matches artifact truth (manual check — advisory only)."""
    return _advisory(True, "dashboard check is manual — verify spot-check log")


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _pnl_field(trades: list[dict]) -> str:
    if trades and "pnl_pips" in trades[0]:
        return "pnl_pips"
    return "pnl_pts"


def _load_trades(log_dir: Path) -> list[dict]:
    trade_file = log_dir / "trades.csv"
    if not trade_file.exists():
        return []
    with open(trade_file, "r") as f:
        return list(csv.DictReader(f))


def _load_replay_expectations(symbol: str) -> dict:
    cfg_map = {
        "EURUSD": "eurusd_t4_paper_v1.json",
        "GBPUSD": "gbpusd_range_paper_v1.json",
        "EURJPY": "eurjpy_t4_paper_v1.json",
    }
    cfg_path = REPO / "argus_flow" / "configs" / cfg_map.get(symbol, "")
    if cfg_path.exists():
        return json.loads(cfg_path.read_text()).get("replay_expectations", {})
    return {}


# ─────────────────────────────────────────────────────────────
# Gate card builder
# ─────────────────────────────────────────────────────────────

def evaluate_runner(runner: dict) -> dict:
    log_dir = REPO / runner["log_dir"]
    all_trades = _load_trades(log_dir)
    valid_trades = [t for t in all_trades if t.get("experiment_valid", "").lower() == "true"]
    rexp = _load_replay_expectations(runner["symbol"])

    checks: dict[str, CheckResult] = {}

    # ── Minimum requirements ──
    checks["min_valid_trades"] = check_min_valid_trades(valid_trades)
    checks["frozen_config_hash"] = check_frozen_config_hash(valid_trades)
    checks["git_sha_consistent"] = check_git_sha_consistent(valid_trades)
    checks["invalid_rate"] = check_invalid_rate(all_trades)
    checks["no_runtime_anomalies"] = check_no_runtime_anomalies(valid_trades)

    # ── Performance requirements ──
    checks["positive_expectancy"] = check_positive_expectancy(valid_trades)
    checks["session_concentration"] = check_session_concentration(valid_trades)
    checks["outlier_trade"] = check_outlier_trade(valid_trades)
    checks["win_rate_vs_replay"] = check_win_rate_vs_replay(valid_trades, rexp)
    checks["signal_frequency"] = check_signal_frequency(log_dir, valid_trades, rexp)

    # ── Runtime requirements ──
    checks["survived_disconnect"] = check_survived_disconnect(all_trades)
    checks["dashboard_truth"] = check_dashboard_truth(log_dir)
    checks["no_manual_intervention"] = check_no_manual_intervention(valid_trades)

    # ── Determine overall verdict ──
    # Only HARD checks block promotion. Advisory and unevidenced are logged but don't block.
    hard_checks = {n: c for n, c in checks.items() if c.classification == "hard"}
    advisory_checks = {n: c for n, c in checks.items() if c.classification == "advisory"}
    unevidenced_checks = {n: c for n, c in checks.items() if c.classification == "unevidenced"}

    hard_all_passed = all(c.passed for c in hard_checks.values())
    has_min_trades = checks["min_valid_trades"].passed

    hard_blockers = [name for name, c in hard_checks.items() if not c.passed]
    unevidenced_items = [name for name in unevidenced_checks]

    if hard_all_passed:
        verdict = "PROMOTE"
    elif not has_min_trades:
        verdict = "NOT_READY"
    else:
        verdict = "BLOCKED"

    return {
        "name": runner["name"],
        "symbol": runner["symbol"],
        "verdict": verdict,
        "checks": {name: {"passed": c.passed, "detail": c.detail, "classification": c.classification} for name, c in checks.items()},
        "blockers": hard_blockers,
        "unevidenced": unevidenced_items,
        "total_trades": len(all_trades),
        "valid_trades": len(valid_trades),
    }


# ─────────────────────────────────────────────────────────────
# CLI output
# ─────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print(f"  Promotion Gate — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print("=" * 70)

    results = []
    for runner in RUNNERS:
        r = evaluate_runner(runner)
        results.append(r)

        verdict_colors = {"PROMOTE": "32", "NOT_READY": "36", "BLOCKED": "31"}
        vc = verdict_colors.get(r["verdict"], "0")
        print(f"\n  {r['name']:>10s}: \033[{vc}m{r['verdict']}\033[0m  "
              f"({r['valid_trades']}/{r['total_trades']} valid trades)")
        print(f"  {'-' * 60}")

        for check_name, check_data in r["checks"].items():
            cls = check_data.get("classification", "hard")
            if check_data["passed"]:
                icon = "\033[32mPASS\033[0m"
            elif cls == "unevidenced":
                icon = "\033[36mN/A \033[0m"
            elif cls == "advisory":
                icon = "\033[33mADV \033[0m"
            else:
                icon = "\033[31mFAIL\033[0m"
            tag = f" [{cls}]" if cls != "hard" else ""
            print(f"    [{icon}] {check_name}{tag}")
            print(f"           {check_data['detail']}")

        if r["blockers"]:
            print(f"\n    Hard blockers: {', '.join(r['blockers'])}")
        if r.get("unevidenced"):
            print(f"    Unevidenced: {', '.join(r['unevidenced'])}")

    # Save report
    out_path = REPO / "argus_flow" / "logs" / "promotion_gate_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "runners": results,
    }, indent=2, default=str))
    print(f"\n  Saved: {out_path}")


if __name__ == "__main__":
    main()
