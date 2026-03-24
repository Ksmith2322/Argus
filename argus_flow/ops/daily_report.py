"""Cohort Compliance Report — validates unified runner against COHORT_SPEC.md.

Reads trades.csv (with experiment_valid, config_hash, session_id, git_sha)
and produces per-pair + fleet-level governance metrics.

Usage:
    python -m argus_flow.ops.daily_report
"""
from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# -- Active Cohort (Class A) — must match runner_unified.py and dashboard.py --
COHORT_RUNNERS = [
    {"name": "GBP/USD", "symbol": "GBPUSD", "log_dir": "argus_flow/logs/gbpusd", "config": "argus_flow/configs/gbpusd_range_paper_v1.json", "unit": "pips"},
    {"name": "EUR/USD", "symbol": "EURUSD", "log_dir": "argus_flow/logs/eurusd", "config": "argus_flow/configs/eurusd_t4_paper_v1.json", "unit": "pips"},
    {"name": "EUR/JPY", "symbol": "EURJPY", "log_dir": "argus_flow/logs/eurjpy", "config": "argus_flow/configs/eurjpy_t4_paper_v1.json", "unit": "pips"},
]

PROMOTION_THRESHOLD = 30   # valid trades needed
INVALIDITY_RATE_MAX = 0.10  # 10% max invalid rate for promotion
INVALIDITY_WINDOW = 10      # sliding window for quarantine check
INVALIDITY_WINDOW_MAX = 0.20  # 20% max over any 10-trade window


def _load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, "r") as f:
        return list(csv.DictReader(f))


def _config_hash(path: Path) -> str:
    """SHA256 hash (first 16 chars) of a config file."""
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, text=True, cwd=str(REPO)
        ).strip()
    except Exception:
        return "unknown"


def _compute_metrics(trades: list[dict], pnl_field: str) -> dict:
    """Compute WR, PF, expectancy, drawdown from a trade list."""
    if not trades:
        return {"count": 0, "win_rate": 0, "profit_factor": 0, "expectancy": 0, "max_drawdown": 0, "total_pnl": 0}

    pnls = [float(t.get(pnl_field, 0)) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    wr = len(wins) / len(pnls) if pnls else 0
    pf = round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else 0
    exp = round(sum(pnls) / len(pnls), 3) if pnls else 0

    cum = 0
    peak = 0
    max_dd = 0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    return {
        "count": len(trades),
        "win_rate": round(wr, 3),
        "profit_factor": pf,
        "expectancy": exp,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
        "max_drawdown": round(max_dd, 2),
        "total_pnl": round(sum(pnls), 2),
    }


def _invalidity_window_check(trades: list[dict]) -> dict:
    """Check any sliding 10-trade window for >20% invalid rate."""
    if len(trades) < INVALIDITY_WINDOW:
        return {"triggered": False, "worst_rate": 0, "window_size": len(trades)}

    worst_rate = 0
    worst_start = 0
    for i in range(len(trades) - INVALIDITY_WINDOW + 1):
        window = trades[i:i + INVALIDITY_WINDOW]
        inv_count = sum(1 for t in window if t.get("experiment_valid", "true").lower() != "true")
        rate = inv_count / INVALIDITY_WINDOW
        if rate > worst_rate:
            worst_rate = rate
            worst_start = i

    return {
        "triggered": worst_rate > INVALIDITY_WINDOW_MAX,
        "worst_rate": round(worst_rate, 2),
        "worst_window_start": worst_start,
    }


def generate_report() -> dict:
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    current_git_sha = _git_sha()

    report = {
        "date": today,
        "timestamp": now.isoformat(),
        "report_type": "cohort_compliance",
        "git_sha": current_git_sha,
        "runners": [],
        "cohort_summary": {},
    }

    fleet_valid = 0
    fleet_invalid = 0
    fleet_total = 0
    has_unknown_reason = False
    config_hashes_seen = set()
    git_shas_seen = set()
    any_quarantine_triggered = False

    for runner in COHORT_RUNNERS:
        log_dir = REPO / runner["log_dir"]
        cfg_path = REPO / runner["config"]
        pnl_field = "pnl_pips" if runner["unit"] == "pips" else "pnl_pts"

        r = {
            "name": runner["name"],
            "symbol": runner["symbol"],
            "unit": runner["unit"],
        }

        # Config hash (current on disk)
        r["config_hash_current"] = _config_hash(cfg_path)
        config_hashes_seen.add(r["config_hash_current"])

        # Replay expectations
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text())
            r["strategy"] = cfg.get("strategy", "")
            r["replay_exp"] = cfg.get("replay_expectations", {})
        else:
            r["strategy"] = ""
            r["replay_exp"] = {}

        # Load all trades
        all_trades = _load_csv(log_dir / "trades.csv")
        r["trades_total"] = len(all_trades)

        # Split valid vs invalid
        valid_trades = [t for t in all_trades if t.get("experiment_valid", "true").lower() == "true"]
        invalid_trades = [t for t in all_trades if t.get("experiment_valid", "true").lower() != "true"]

        r["valid_trades"] = len(valid_trades)
        r["invalid_trades"] = len(invalid_trades)
        r["invalid_rate"] = round(len(invalid_trades) / len(all_trades), 3) if all_trades else 0

        # Invalid reason breakdown
        reasons = Counter(t.get("invalid_reason", "UNKNOWN") for t in invalid_trades)
        r["invalid_reasons"] = dict(reasons)
        if "UNKNOWN" in reasons or "" in reasons:
            has_unknown_reason = True

        # Config hash consistency (all trades should have same hash)
        trade_hashes = set(t.get("config_hash", "") for t in all_trades if t.get("config_hash"))
        r["config_hash_consistent"] = len(trade_hashes) <= 1
        r["config_hashes_in_trades"] = list(trade_hashes)
        config_hashes_seen.update(trade_hashes)

        # Git sha consistency
        trade_shas = set(t.get("git_sha", "") for t in all_trades if t.get("git_sha"))
        r["git_sha_consistent"] = len(trade_shas) <= 1
        r["git_shas_in_trades"] = list(trade_shas)
        git_shas_seen.update(trade_shas)

        # Session grouping
        sessions = set(t.get("session_id", "") for t in all_trades if t.get("session_id"))
        r["session_count"] = len(sessions)

        # Valid-only metrics (this is what matters for promotion)
        r["valid_metrics"] = _compute_metrics(valid_trades, pnl_field)

        # All-trade metrics (for reference only, not for governance)
        r["all_metrics"] = _compute_metrics(all_trades, pnl_field)

        # Invalidity window check (quarantine trigger)
        window_check = _invalidity_window_check(all_trades)
        r["invalidity_window"] = window_check
        if window_check["triggered"]:
            any_quarantine_triggered = True

        # Replay divergence (valid trades only)
        signals = _load_csv(log_dir / "signals.csv")
        r["signals_total"] = len(signals)
        replay_spd = r["replay_exp"].get("signals_per_day", 0)
        replay_wr = r["replay_exp"].get("win_rate", 0)
        if signals and len(signals) > 10:
            try:
                first = datetime.fromisoformat(signals[0]["ts"].replace("Z", "+00:00"))
                last = datetime.fromisoformat(signals[-1]["ts"].replace("Z", "+00:00"))
                days = max((last - first).total_seconds() / 86400, 0.001)
                entries = sum(1 for s in signals if s.get("action") == "ENTRY")
                live_spd = entries / days
                r["live_signals_per_day"] = round(live_spd, 1)
                if replay_spd > 0:
                    r["signal_freq_ratio"] = round(live_spd / replay_spd, 2)
            except Exception:
                pass

        # Replay WR deviation (valid only)
        if valid_trades and replay_wr > 0:
            wr_delta = abs(r["valid_metrics"]["win_rate"] - replay_wr)
            r["wr_delta_vs_replay"] = round(wr_delta, 3)
            r["wr_within_15pp"] = wr_delta <= 0.15

        # Promotion eligibility
        r["promotion_eligible"] = (
            r["valid_trades"] >= PROMOTION_THRESHOLD
            and r["invalid_rate"] <= INVALIDITY_RATE_MAX
            and r["config_hash_consistent"]
            and r["git_sha_consistent"]
            and r["valid_metrics"]["expectancy"] > 0
            and not window_check["triggered"]
        )
        if r["valid_trades"] < PROMOTION_THRESHOLD:
            r["promotion_blocker"] = f"need {PROMOTION_THRESHOLD - r['valid_trades']} more valid trades"
        elif r["invalid_rate"] > INVALIDITY_RATE_MAX:
            r["promotion_blocker"] = f"invalid rate {r['invalid_rate']:.1%} > {INVALIDITY_RATE_MAX:.0%}"
        elif not r["config_hash_consistent"]:
            r["promotion_blocker"] = "config hash changed mid-cohort"
        elif r["valid_metrics"]["expectancy"] <= 0:
            r["promotion_blocker"] = f"negative expectancy ({r['valid_metrics']['expectancy']})"
        else:
            r["promotion_blocker"] = None

        fleet_valid += r["valid_trades"]
        fleet_invalid += r["invalid_trades"]
        fleet_total += r["trades_total"]
        report["runners"].append(r)

    # Fleet / cohort summary
    report["cohort_summary"] = {
        "total_trades": fleet_total,
        "valid_trades": fleet_valid,
        "invalid_trades": fleet_invalid,
        "invalid_rate": round(fleet_invalid / fleet_total, 3) if fleet_total else 0,
        "progress_toward_30": f"{fleet_valid}/{PROMOTION_THRESHOLD}",
        "has_unknown_invalid_reason": has_unknown_reason,
        "config_hashes_seen": list(config_hashes_seen),
        "config_consistent": len(config_hashes_seen - {""}) <= len(COHORT_RUNNERS),
        "git_shas_seen": list(git_shas_seen),
        "git_sha_consistent": len(git_shas_seen - {""}) <= 1,
        "quarantine_triggered": any_quarantine_triggered,
        "cohort_paused": has_unknown_reason or any_quarantine_triggered,
        "pause_reason": (
            "UNKNOWN invalid reason detected" if has_unknown_reason
            else "invalidity window >20% triggered" if any_quarantine_triggered
            else None
        ),
    }

    return report


def print_report(report: dict):
    print(f"\n{'='*70}")
    print(f"  COHORT COMPLIANCE REPORT — {report['date']}")
    print(f"  git: {report['git_sha']}  |  generated: {report['timestamp'][:19]}Z")
    print(f"{'='*70}")

    cs = report["cohort_summary"]
    status = "PAUSED" if cs["cohort_paused"] else "ACTIVE"
    status_color = "\033[31m" if cs["cohort_paused"] else "\033[32m"
    print(f"\n  Cohort Status: {status_color}{status}\033[0m")
    if cs["pause_reason"]:
        print(f"  Pause Reason:  {cs['pause_reason']}")
    print(f"  Progress:      {cs['valid_trades']}/{PROMOTION_THRESHOLD} valid trades")
    print(f"  Invalid Rate:  {cs['invalid_rate']:.1%} ({cs['invalid_trades']} of {cs['total_trades']})")
    print(f"  Config Consistent: {'YES' if cs['config_consistent'] else 'NO — COHORT RESET NEEDED'}")
    print(f"  Git SHA Consistent: {'YES' if cs['git_sha_consistent'] else 'NO — COHORT RESET NEEDED'}")

    for r in report["runners"]:
        vm = r["valid_metrics"]
        print(f"\n  {'-'*60}")
        print(f"  {r['name']} ({r['strategy']})")
        print(f"    Valid: {r['valid_trades']}  Invalid: {r['invalid_trades']}  Rate: {r['invalid_rate']:.1%}")

        if r["invalid_reasons"]:
            reasons_str = ", ".join(f"{k}={v}" for k, v in r["invalid_reasons"].items())
            print(f"    Invalid Reasons: {reasons_str}")

        if vm["count"] > 0:
            print(f"    Valid-Only Metrics:")
            print(f"      WR={vm['win_rate']:.1%}  PF={vm['profit_factor']}  Exp={vm['expectancy']:+.2f} {r['unit']}")
            print(f"      avg_W={vm['avg_win']:+.1f}  avg_L={vm['avg_loss']:+.1f}  maxDD={vm['max_drawdown']:.1f}")
            print(f"      PnL={vm['total_pnl']:+.1f} {r['unit']}")

        if r.get("wr_delta_vs_replay") is not None:
            flag = "OK" if r["wr_within_15pp"] else "WATCH (>15pp)"
            print(f"    Replay WR Delta: {r['wr_delta_vs_replay']:.1%} — {flag}")

        if r.get("signal_freq_ratio"):
            ratio = r["signal_freq_ratio"]
            flag = " WATCH" if ratio < 0.5 or ratio > 2.0 else ""
            print(f"    Signal Freq: {r.get('live_signals_per_day', 0):.1f}/day (ratio={ratio:.2f}){flag}")

        promo = r.get("promotion_eligible", False)
        blocker = r.get("promotion_blocker")
        if promo:
            print(f"    \033[32mPROMOTION ELIGIBLE\033[0m")
        elif blocker:
            print(f"    Promotion: {blocker}")

        # Window check
        wc = r.get("invalidity_window", {})
        if wc.get("triggered"):
            print(f"    \033[31mQUARANTINE: {wc['worst_rate']:.0%} invalid in 10-trade window\033[0m")

    print(f"\n{'='*70}")
    print(f"  Report complete")
    print(f"{'='*70}\n")


def main():
    report = generate_report()
    print_report(report)

    # Save JSON
    date_str = report["date"].replace("-", "")
    out_json = REPO / "argus_flow" / "logs" / f"cohort_report_{date_str}.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2, default=str))
    print(f"  Saved: {out_json}")


if __name__ == "__main__":
    main()