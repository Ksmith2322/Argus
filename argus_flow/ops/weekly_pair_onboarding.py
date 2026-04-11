"""Weekly managed FX pair onboarding.

Runs a conservative discovery pass for new FX pairs using the current model
template. Candidates must:
- not already exist in the repo / managed rotation
- have fresh enough historical data (downloaded from IBKR if needed)
- pass the existing onboarding payoff contract
- pass walk-forward validation

Only the top N passing candidates are materialized as managed watcher configs.

Usage:
    python -m argus_flow.ops.weekly_pair_onboarding
    python -m argus_flow.ops.weekly_pair_onboarding --dry-run --no-download
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from ops.process_lock import ProcessLock, ProcessLockError

from argus_flow.ops.download_ibkr_bars import download_bars
from argus_flow.ops.fleet_registry import STAGE_DISCOVERY, STAGE_WATCHER, discover_managed_runners
from argus_flow.ops.onboard_pair import (
    _apply_managed_defaults,
    _create_log_dir,
    _ensure_hash,
    _evaluate_results,
    _run_backtest,
)
from argus_flow.ops.refresh_managed_truth import refresh_managed_truth
from argus_flow.ops.walkforward_validation import build_report, write_report

REPO = Path(__file__).resolve().parents[2]
CONFIGS_DIR = REPO / "argus_flow" / "configs"
DATA_DIR = REPO / "argus_flow" / "data"
LOGS_DIR = REPO / "argus_flow" / "logs"
DEFAULT_DISCOVERY_SPEC_PATH = CONFIGS_DIR / "discovery_fx_universe.json"
LOCK_NAME = "weekly_pair_onboarding"
LATEST_REPORT_PATH = LOGS_DIR / "weekly_pair_onboarding_latest.json"

DEFAULT_DISCOVERY_SPEC = {
    "template_config": "argus_flow/configs/cadjpy_mtf_paper_v1.json",
    "entry_stage": STAGE_WATCHER,
    "history_days": 45,
    "max_new_pairs": 5,
    "max_data_age_days": 7.0,
    "candidate_symbols": [
        "USDCHF",
        "USDCAD",
        "NZDUSD",
        "EURGBP",
        "EURCHF",
        "EURAUD",
        "EURNZD",
        "EURCAD",
        "GBPCHF",
        "GBPAUD",
        "GBPCAD",
        "AUDCAD",
        "AUDCHF",
        "AUDNZD",
        "NZDJPY",
        "NZDCHF",
        "NZDCAD",
        "CHFJPY",
        "CADCHF",
    ],
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return None


def _merge_spec(user_spec: dict | None) -> dict:
    spec = json.loads(json.dumps(DEFAULT_DISCOVERY_SPEC))
    if isinstance(user_spec, dict):
        for key, value in user_spec.items():
            spec[key] = value
    spec["entry_stage"] = str(spec.get("entry_stage", STAGE_WATCHER)).strip().lower()
    if spec["entry_stage"] not in {STAGE_WATCHER, STAGE_DISCOVERY}:
        spec["entry_stage"] = STAGE_WATCHER
    spec["history_days"] = max(int(spec.get("history_days", DEFAULT_DISCOVERY_SPEC["history_days"]) or 0), 14)
    spec["max_new_pairs"] = max(int(spec.get("max_new_pairs", DEFAULT_DISCOVERY_SPEC["max_new_pairs"]) or 0), 0)
    spec["max_data_age_days"] = max(
        float(spec.get("max_data_age_days", DEFAULT_DISCOVERY_SPEC["max_data_age_days"]) or 0.0),
        0.0,
    )
    spec["candidate_symbols"] = _normalize_candidate_symbols(spec.get("candidate_symbols", []))
    return spec


def load_discovery_spec(path: Path | None = None) -> dict:
    target = path or DEFAULT_DISCOVERY_SPEC_PATH
    return _merge_spec(_load_json(target))


def _normalize_candidate_symbols(values: list[object]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        symbol = "".join(ch for ch in str(raw or "").upper() if ch.isalpha())
        if len(symbol) != 6:
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        normalized.append(symbol)
    return normalized


def _template_suffix(template_path: Path) -> str:
    parts = template_path.stem.split("_")
    if parts and len(parts[0]) == 6 and parts[0].isalpha():
        return "_".join(parts[1:]) or "paper_v1"
    return template_path.stem


def candidate_config_name(symbol: str, template_path: Path) -> str:
    return f"{symbol.lower()}_{_template_suffix(template_path)}.json"


def _data_path_for_symbol(symbol: str) -> Path:
    return DATA_DIR / f"ibkr_{symbol.lower()}_1m.csv"


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_ratio(value: object) -> float:
    ratio = _safe_float(value, 0.0)
    if ratio > 1.0:
        ratio /= 100.0
    return ratio


def _deterministic_client_id(symbol: str) -> int:
    total = 0
    for idx, ch in enumerate(symbol.upper(), start=1):
        total += idx * ord(ch)
    return 1000 + (total % 7000)


def _deepcopy_payload(payload: dict) -> dict:
    return json.loads(json.dumps(payload))


def _collect_existing_forex_symbols() -> set[str]:
    symbols: set[str] = set()
    for runner in discover_managed_runners():
        if str(runner.get("instrument_type", "")).lower() == "forex":
            symbols.add(str(runner.get("symbol", "")).upper())
    for path in CONFIGS_DIR.glob("*.json"):
        if path.name == "hashes.json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if str(payload.get("instrument_type", "")).lower() != "forex":
            continue
        symbol = "".join(ch for ch in str(payload.get("symbol", "")).upper() if ch.isalpha())
        if len(symbol) == 6:
            symbols.add(symbol)
    return symbols


def build_candidate_plan(candidate_symbols: list[str], existing_symbols: set[str]) -> tuple[list[str], list[str]]:
    new_candidates: list[str] = []
    skipped_existing: list[str] = []
    for symbol in _normalize_candidate_symbols(candidate_symbols):
        if symbol in existing_symbols:
            skipped_existing.append(symbol)
            continue
        new_candidates.append(symbol)
    return new_candidates, skipped_existing


def _data_age_days(path: Path) -> float | None:
    if not path.exists():
        return None
    age_seconds = (datetime.now(timezone.utc) - datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)).total_seconds()
    return round(age_seconds / 86400.0, 2)


def _coverage_days(path: Path) -> float:
    first_ts = ""
    last_ts = ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                ts = str(row.get("ts", "") or "").strip()
                if not ts:
                    continue
                if not first_ts:
                    first_ts = ts
                last_ts = ts
    except OSError:
        return 1.0
    if not first_ts or not last_ts:
        return 1.0
    try:
        start = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
        end = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
    except ValueError:
        return 1.0
    return max(round((end - start).total_seconds() / 86400.0, 2), 1.0)


def _write_bars(path: Path, bars: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ts", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(bars)


def _ensure_candidate_data(
    *,
    symbol: str,
    history_days: int,
    max_data_age_days: float,
    no_download: bool,
) -> tuple[Path | None, dict]:
    data_path = _data_path_for_symbol(symbol)
    age_days = _data_age_days(data_path)
    if data_path.exists() and age_days is not None and age_days <= max_data_age_days:
        return data_path, {"action": "reuse", "data_path": str(data_path.relative_to(REPO)), "age_days": age_days}

    if no_download:
        if data_path.exists():
            return None, {
                "action": "stale",
                "reason": f"data is {age_days:.1f}d old and downloads disabled",
                "data_path": str(data_path.relative_to(REPO)),
            }
        return None, {"action": "missing", "reason": "historical data missing and downloads disabled"}

    try:
        bars = download_bars(symbol, history_days)
    except Exception as exc:  # pragma: no cover - network / broker path
        if data_path.exists():
            return None, {
                "action": "download_failed",
                "reason": f"download failed with stale local data present: {exc}",
                "data_path": str(data_path.relative_to(REPO)),
            }
        return None, {"action": "download_failed", "reason": str(exc)}

    if not bars:
        return None, {"action": "download_failed", "reason": "no bars returned from IBKR"}

    _write_bars(data_path, bars)
    return data_path, {
        "action": "downloaded",
        "bars": len(bars),
        "data_path": str(data_path.relative_to(REPO)),
        "age_days": _data_age_days(data_path),
    }


def _build_eval_config(template_cfg: dict, symbol: str) -> dict:
    cfg = _deepcopy_payload(template_cfg)
    cfg["symbol"] = symbol.upper()
    cfg["ibkr_client_id"] = _deterministic_client_id(symbol)
    return cfg


def _build_replay_expectations(results: dict, data_path: Path) -> dict:
    signals_per_day = round(_safe_float(results.get("total_entries", 0.0)) / max(_coverage_days(data_path), 1.0), 2)
    low_band = round(max(0.5, signals_per_day * 0.5), 2)
    high_band = round(max(signals_per_day + 1.0, signals_per_day * 1.5), 2)
    win_rate = round(_normalize_ratio(results.get("win_rate", 0.0)), 3)

    expectations = {
        "signals_per_day": signals_per_day,
        "signals_per_day_range": [low_band, high_band],
        "win_rate": win_rate,
        "win_rate_range": [round(max(0.0, win_rate - 0.12), 3), round(min(1.0, win_rate + 0.12), 3)],
        "exp_pips_per_trade": round(_safe_float(results.get("expectancy", 0.0)), 3),
    }

    for key in ("stop_rate", "target_rate", "timeout_rate"):
        value = _normalize_ratio(results.get(key, 0.0))
        if value > 0.0:
            expectations[key] = round(value, 3)

    return expectations


def build_final_candidate_config(
    *,
    template_cfg: dict,
    template_path: Path,
    symbol: str,
    stage: str,
    results: dict,
) -> tuple[str, dict]:
    normalized_stage = stage if stage in {STAGE_WATCHER, STAGE_DISCOVERY} else STAGE_WATCHER
    cfg = _build_eval_config(template_cfg, symbol)
    cfg["stage"] = normalized_stage
    cfg["replay_expectations"] = _build_replay_expectations(results, _data_path_for_symbol(symbol))

    discovery = cfg.get("discovery", {}) if isinstance(cfg.get("discovery", {}), dict) else {}
    discovery.update(
        {
            "source": "weekly_pair_onboarding",
            "template_config": str(template_path.relative_to(REPO)).replace("\\", "/"),
            "selected_at": _now_iso(),
        }
    )
    cfg["discovery"] = discovery

    config_name = candidate_config_name(symbol, template_path)
    cfg, _ = _apply_managed_defaults(cfg, CONFIGS_DIR / config_name, normalized_stage)
    return config_name, cfg


def select_top_candidates(candidates: list[dict], max_new_pairs: int) -> list[dict]:
    passing = [candidate for candidate in candidates if candidate.get("status") == "PASS"]
    passing.sort(
        key=lambda item: (
            -_safe_float(item.get("score", 0.0)),
            -_safe_float(item.get("walkforward_summary", {}).get("mean_expectancy", 0.0)),
            -_safe_float(item.get("backtest", {}).get("expectancy", 0.0)),
            item.get("symbol", ""),
        )
    )
    return passing[:max_new_pairs]


def _candidate_score(backtest: dict, walkforward_report: dict) -> float:
    summary = walkforward_report.get("summary", {}) if isinstance(walkforward_report.get("summary", {}), dict) else {}
    return round(
        _safe_float(summary.get("mean_expectancy", 0.0)) * 100.0
        + _safe_float(summary.get("positive_ratio", 0.0)) * 50.0
        + min(_safe_float(summary.get("median_profit_factor", 0.0)), 3.0) * 10.0
        + min(_safe_float(backtest.get("trades", 0.0)), 120.0) * 0.1
        - _safe_float(summary.get("max_fold_drawdown", 0.0)) * 0.5,
        4,
    )


def _write_onboarding_artifact(symbol: str, payload: dict) -> None:
    log_dir = _create_log_dir(symbol)
    out_path = log_dir / "onboarding_report.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _finalize_candidate(candidate: dict) -> dict:
    symbol = candidate["symbol"]
    config_name = candidate["config_name"]
    config_path = CONFIGS_DIR / config_name
    config_payload = candidate["config_payload"]

    config_path.write_text(json.dumps(config_payload, indent=4) + "\n", encoding="utf-8")
    cfg_hash, _ = _ensure_hash(config_path)
    _create_log_dir(symbol)
    write_report(candidate["walkforward_report"])

    onboarding_artifact = {
        "timestamp": _now_iso(),
        "symbol": symbol,
        "status": "ADDED_TO_WATCHER",
        "config_file": config_name,
        "config_hash": cfg_hash,
        "score": candidate["score"],
        "data_path": candidate["data_path"],
        "data_status": candidate["data_status"],
        "backtest": candidate["backtest"],
        "walkforward_summary": candidate["walkforward_summary"],
        "advisories": candidate["advisories"],
    }
    _write_onboarding_artifact(symbol, onboarding_artifact)
    return onboarding_artifact


def evaluate_candidate(
    *,
    symbol: str,
    template_cfg: dict,
    template_path: Path,
    spec: dict,
    no_download: bool,
) -> dict:
    data_path, data_status = _ensure_candidate_data(
        symbol=symbol,
        history_days=int(spec["history_days"]),
        max_data_age_days=float(spec["max_data_age_days"]),
        no_download=no_download,
    )
    if data_path is None:
        return {
            "symbol": symbol,
            "status": "SKIP",
            "reason": data_status.get("reason", data_status.get("action", "missing data")),
            "data_status": data_status,
        }

    scratch_dir = LOGS_DIR / "_scratch_weekly_onboarding"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    temp_config_path = scratch_dir / candidate_config_name(symbol, template_path)
    try:
        eval_cfg = _build_eval_config(template_cfg, symbol)
        temp_config_path.write_text(json.dumps(eval_cfg, indent=4) + "\n", encoding="utf-8")

        backtest = _run_backtest(temp_config_path, data_path)
        if backtest is None:
            return {
                "symbol": symbol,
                "status": "FAIL",
                "reason": "backtest returned no results",
                "data_status": data_status,
            }

        bt_passed, kills, advisories = _evaluate_results(backtest)
        if not bt_passed:
            return {
                "symbol": symbol,
                "status": "FAIL",
                "reason": "; ".join(kills) or "payoff contract failed",
                "data_path": str(data_path.relative_to(REPO)),
                "data_status": data_status,
                "backtest": backtest,
                "kills": kills,
                "advisories": advisories,
            }

        walkforward_report = build_report(config_path=temp_config_path, data_path=data_path)
        wf_status = str(walkforward_report.get("status", "UNKNOWN")).upper()
        wf_summary = walkforward_report.get("summary", {}) if isinstance(walkforward_report.get("summary", {}), dict) else {}
        if wf_status != "PASS":
            return {
                "symbol": symbol,
                "status": "FAIL",
                "reason": f"walk-forward {wf_status}: {wf_summary.get('rationale', wf_status)}",
                "data_path": str(data_path.relative_to(REPO)),
                "data_status": data_status,
                "backtest": backtest,
                "advisories": advisories,
                "walkforward_summary": wf_summary,
            }

        config_name, config_payload = build_final_candidate_config(
            template_cfg=template_cfg,
            template_path=template_path,
            symbol=symbol,
            stage=str(spec["entry_stage"]),
            results=backtest,
        )
        return {
            "symbol": symbol,
            "status": "PASS",
            "score": _candidate_score(backtest, walkforward_report),
            "data_path": str(data_path.relative_to(REPO)),
            "data_status": data_status,
            "config_name": config_name,
            "config_payload": config_payload,
            "backtest": backtest,
            "walkforward_report": walkforward_report,
            "walkforward_summary": wf_summary,
            "advisories": advisories,
        }
    finally:
        try:
            temp_config_path.unlink()
        except OSError:
            pass


def _write_summary_report(report: dict) -> Path:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = LOGS_DIR / f"weekly_pair_onboarding_{timestamp}.json"
    payload = json.dumps(report, indent=2)
    LATEST_REPORT_PATH.write_text(payload, encoding="utf-8")
    archive_path.write_text(payload, encoding="utf-8")
    return archive_path


def run_weekly_pair_onboarding(
    *,
    spec_path: Path,
    no_download: bool,
    dry_run: bool,
    max_new_pairs_override: int | None,
) -> int:
    lock = ProcessLock(LOCK_NAME)
    try:
        lock.acquire(metadata={"kind": "weekly_pair_onboarding"})
    except ProcessLockError:
        print("weekly_pair_onboarding already running; exiting.")
        return 2

    try:
        spec = load_discovery_spec(spec_path)
        if max_new_pairs_override is not None:
            spec["max_new_pairs"] = max(max_new_pairs_override, 0)

        template_path = (REPO / spec["template_config"]).resolve() if not Path(spec["template_config"]).is_absolute() else Path(spec["template_config"])
        if not template_path.exists():
            raise FileNotFoundError(f"Template config not found: {template_path}")
        template_cfg = json.loads(template_path.read_text(encoding="utf-8"))

        existing_symbols = _collect_existing_forex_symbols()
        candidates, skipped_existing = build_candidate_plan(spec["candidate_symbols"], existing_symbols)

        evaluated: list[dict] = []
        for symbol in candidates:
            evaluated.append(
                evaluate_candidate(
                    symbol=symbol,
                    template_cfg=template_cfg,
                    template_path=template_path,
                    spec=spec,
                    no_download=no_download,
                )
            )

        selected = select_top_candidates(evaluated, int(spec["max_new_pairs"]))
        added: list[dict] = []
        if not dry_run:
            for candidate in selected:
                added.append(_finalize_candidate(candidate))

        refresh_rc = 0
        if added and not dry_run:
            refresh_rc = refresh_managed_truth(include_summary=False, accept_existing_age_s=900)

        report = {
            "timestamp": _now_iso(),
            "status": "OK" if refresh_rc == 0 else "WARN",
            "dry_run": dry_run,
            "spec_path": str(spec_path.relative_to(REPO)) if spec_path.is_absolute() else str(spec_path).replace("\\", "/"),
            "template_config": str(template_path.relative_to(REPO)).replace("\\", "/"),
            "entry_stage": spec["entry_stage"],
            "history_days": spec["history_days"],
            "max_new_pairs": spec["max_new_pairs"],
            "max_data_age_days": spec["max_data_age_days"],
            "existing_forex_symbol_count": len(existing_symbols),
            "skipped_existing_symbols": skipped_existing,
            "candidate_symbols_considered": candidates,
            "evaluated": evaluated,
            "selected_symbols": [candidate["symbol"] for candidate in selected],
            "added": added,
            "refresh_return_code": refresh_rc,
        }
        archive_path = _write_summary_report(report)

        print("=" * 72)
        print("  Weekly Pair Onboarding")
        print("=" * 72)
        print(f"  Template:          {template_path.name}")
        print(f"  Candidates:        {len(candidates)} new / {len(skipped_existing)} skipped existing")
        print(f"  Passing:           {len([c for c in evaluated if c.get('status') == 'PASS'])}")
        print(f"  Selected top N:    {len(selected)}")
        print(f"  Added to watcher:  {len(added)}")
        print(f"  Report:            {archive_path}")
        if skipped_existing:
            print(f"  Existing skip:     {', '.join(skipped_existing[:8])}")
        for candidate in selected:
            wf_summary = candidate.get("walkforward_summary", {})
            print(
                f"    - {candidate['symbol']}: score={candidate['score']:.2f} "
                f"| wf_exp={_safe_float(wf_summary.get('mean_expectancy', 0.0)):+.3f} "
                f"| bt_exp={_safe_float(candidate.get('backtest', {}).get('expectancy', 0.0)):+.3f}"
            )
        return 0 if refresh_rc == 0 else 1
    finally:
        lock.release()


def main() -> None:
    parser = argparse.ArgumentParser(description="Weekly discovery + onboarding for new FX pairs")
    parser.add_argument(
        "--spec",
        default=str(DEFAULT_DISCOVERY_SPEC_PATH.relative_to(REPO)).replace("\\", "/"),
        help="Discovery universe JSON (default: argus_flow/configs/discovery_fx_universe.json)",
    )
    parser.add_argument("--no-download", action="store_true", help="Do not attempt IBKR downloads for missing/stale data")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate candidates without writing configs")
    parser.add_argument("--max-new-pairs", type=int, default=None, help="Override max_new_pairs from the discovery spec")
    args = parser.parse_args()

    spec_path = Path(args.spec)
    if not spec_path.is_absolute():
        spec_path = (REPO / spec_path).resolve()
    raise SystemExit(
        run_weekly_pair_onboarding(
            spec_path=spec_path,
            no_download=bool(args.no_download),
            dry_run=bool(args.dry_run),
            max_new_pairs_override=args.max_new_pairs,
        )
    )


if __name__ == "__main__":
    main()
