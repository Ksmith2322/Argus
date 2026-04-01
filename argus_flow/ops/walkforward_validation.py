"""Walk-forward validation for argus_flow strategy configs.

Runs sequential out-of-sample folds against historical bar data using the same
offline backtest harness that powers single-run replays. The goal is not to
optimize parameters here, but to measure whether a fixed config stays positive
across multiple contiguous time slices.

Usage:
    python -m argus_flow.ops.walkforward_validation --config argus_flow/configs/eurusd_t4_paper_v1.json
    python -m argus_flow.ops.walkforward_validation --symbol EURUSD
    python -m argus_flow.ops.walkforward_validation --all-active
"""
from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

from argus_flow.ops.fx_backtest import BacktestRunner, load_bars
from argus_flow.ops.fleet_registry import STAGE_PAPER, STAGE_WATCHER, discover_managed_runners

REPO = Path(__file__).resolve().parents[2]
CONFIGS_DIR = REPO / "argus_flow" / "configs"
DATA_DIR = REPO / "argus_flow" / "data"
LOGS_DIR = REPO / "argus_flow" / "logs"

DEFAULT_FOLDS = 6
DEFAULT_WARMUP_BARS = 300
DEFAULT_MIN_TRADES_PER_FOLD = 3

FUTURES_DATA_MAP = {
    "MES": "ibkr_MES_sp500_micro_1m.csv",
    "MNQ": "ibkr_MNQ_nasdaq_micro_1m.csv",
    "MYM": "ibkr_MYM_dow_micro_1m.csv",
}


def infer_data_path(config: dict) -> Path:
    """Infer the historical bar CSV for a config."""
    symbol = str(config.get("symbol", "")).upper()
    instrument_type = str(config.get("instrument_type", "forex")).lower()

    if instrument_type == "forex":
        return DATA_DIR / f"ibkr_{symbol.lower()}_1m.csv"

    if instrument_type == "future":
        mapped = FUTURES_DATA_MAP.get(symbol)
        if mapped:
            return DATA_DIR / mapped
        exact = DATA_DIR / f"ibkr_{symbol}_1m.csv"
        if exact.exists():
            return exact
        wildcard_matches = sorted(DATA_DIR.glob(f"ibkr_{symbol}_*_1m.csv"))
        if wildcard_matches:
            return wildcard_matches[0]

    return DATA_DIR / f"ibkr_{symbol.lower()}_1m.csv"


def resolve_config_path(symbol: str | None = None, config_path: str | None = None) -> Path:
    """Resolve a config path by explicit path or symbol."""
    if config_path:
        return (REPO / config_path).resolve() if not Path(config_path).is_absolute() else Path(config_path)

    if not symbol:
        raise ValueError("Need either symbol or config path")

    symbol = symbol.upper()
    for runner in discover_managed_runners():
        if runner["symbol"] == symbol and not runner["live"]:
            return REPO / runner["config_path"]

    for candidate in sorted(CONFIGS_DIR.glob("*_paper_v1.json")):
        try:
            cfg = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(cfg.get("symbol", "")).upper() == symbol:
            return candidate

    raise FileNotFoundError(f"No config found for symbol {symbol}")


def build_fold_ranges(total_bars: int, folds: int, warmup_bars: int) -> list[tuple[int, int]]:
    """Split the dataset into sequential evaluation folds after warmup."""
    usable = total_bars - warmup_bars
    if usable <= 0 or folds <= 0:
        return []

    fold_size = usable // folds
    if fold_size <= 0:
        return []

    ranges: list[tuple[int, int]] = []
    cursor = warmup_bars
    for fold_idx in range(folds):
        start = cursor
        end = start + fold_size
        if fold_idx == folds - 1:
            end = total_bars
        ranges.append((start, min(end, total_bars)))
        cursor = end
    return [(start, end) for start, end in ranges if end > start]


def run_fold(
    config: dict,
    bars: list[dict],
    start_idx: int,
    end_idx: int,
    warmup_bars: int,
    enable_pyramid: bool,
    min_trades_per_fold: int,
    fold_number: int,
) -> dict:
    """Run one out-of-sample fold with a preloaded warmup buffer only."""
    bt = BacktestRunner(config, enable_pyramid=enable_pyramid)

    warmup_start = max(0, start_idx - warmup_bars)
    for bar in bars[warmup_start:start_idx]:
        bt.buf.add(bar)

    for bar in bars[start_idx:end_idx]:
        bt.process_bar(bar)

    summary = bt.summary()
    trades = int(summary.get("trades", 0))
    expectancy = float(summary.get("expectancy", 0.0) or 0.0) if trades else 0.0
    profit_factor = float(summary.get("profit_factor", 0.0) or 0.0) if trades else 0.0
    max_drawdown = float(summary.get("max_drawdown", 0.0) or 0.0) if trades else 0.0

    if trades < min_trades_per_fold:
        status = "INSUFFICIENT_DATA"
    elif expectancy > 0:
        status = "POSITIVE"
    else:
        status = "NEGATIVE"

    return {
        "fold": fold_number,
        "start_ts": bars[start_idx]["ts"],
        "end_ts": bars[end_idx - 1]["ts"],
        "warmup_bars": start_idx - warmup_start,
        "test_bars": end_idx - start_idx,
        "trades": trades,
        "win_rate": summary.get("win_rate", 0.0),
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "total_pnl": float(summary.get("total_pnl", 0.0) or 0.0) if trades else 0.0,
        "max_drawdown": max_drawdown,
        "unit": summary.get("unit", "pips"),
        "status": status,
    }


def summarize_folds(
    fold_results: list[dict],
    min_trades_per_fold: int,
    baseline_expectancy: float | None = None,
) -> dict:
    """Aggregate fold outcomes into a single walk-forward verdict."""
    scored = [f for f in fold_results if f["trades"] >= min_trades_per_fold]
    positive = [f for f in scored if f["expectancy"] > 0]
    negative = [f for f in scored if f["expectancy"] <= 0]

    expectancies = [f["expectancy"] for f in scored]
    max_drawdowns = [f["max_drawdown"] for f in scored]
    profit_factors = [f["profit_factor"] for f in scored]

    if not scored:
        status = "INSUFFICIENT_DATA"
        rationale = f"0/{len(fold_results)} folds had at least {min_trades_per_fold} trades"
    else:
        positive_ratio = len(positive) / len(scored)
        if positive_ratio >= 0.60 and len(positive) >= max(1, (len(scored) + 1) // 2):
            status = "PASS"
            rationale = f"{len(positive)}/{len(scored)} scored folds positive"
        elif len(positive) == 0:
            status = "FAIL"
            rationale = f"0/{len(scored)} scored folds positive"
        else:
            status = "WATCH"
            rationale = f"{len(positive)}/{len(scored)} scored folds positive"

    summary = {
        "status": status,
        "rationale": rationale,
        "folds_total": len(fold_results),
        "folds_scored": len(scored),
        "positive_folds": len(positive),
        "negative_folds": len(negative),
        "positive_ratio": round(len(positive) / len(scored), 4) if scored else 0.0,
        "total_trades": sum(f["trades"] for f in fold_results),
        "mean_expectancy": round(statistics.mean(expectancies), 4) if expectancies else 0.0,
        "median_expectancy": round(statistics.median(expectancies), 4) if expectancies else 0.0,
        "worst_fold_expectancy": round(min(expectancies), 4) if expectancies else 0.0,
        "best_fold_expectancy": round(max(expectancies), 4) if expectancies else 0.0,
        "median_profit_factor": round(statistics.median(profit_factors), 4) if profit_factors else 0.0,
        "max_fold_drawdown": round(max(max_drawdowns), 4) if max_drawdowns else 0.0,
        "aggregate_total_pnl": round(sum(f["total_pnl"] for f in fold_results), 4),
    }

    if baseline_expectancy and baseline_expectancy > 0 and summary["mean_expectancy"]:
        summary["baseline_expectancy"] = baseline_expectancy
        summary["expectancy_vs_baseline_ratio"] = round(summary["mean_expectancy"] / baseline_expectancy, 4)

    return summary


def build_report(
    config_path: Path,
    data_path: Path | None = None,
    folds: int = DEFAULT_FOLDS,
    warmup_bars: int = DEFAULT_WARMUP_BARS,
    min_trades_per_fold: int = DEFAULT_MIN_TRADES_PER_FOLD,
    enable_pyramid: bool = False,
) -> dict:
    """Build a walk-forward validation report for one config."""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    data_path = data_path or infer_data_path(config)
    if not data_path.exists():
        raise FileNotFoundError(f"Data file not found: {data_path}")

    bars = load_bars(data_path)
    ranges = build_fold_ranges(len(bars), folds=folds, warmup_bars=warmup_bars)
    if not ranges:
        raise ValueError(f"Not enough data for {folds} folds with {warmup_bars} warmup bars")

    replay_expectations = config.get("replay_expectations", {})
    baseline_expectancy = replay_expectations.get("exp_pips_per_trade")

    fold_results = []
    for idx, (start_idx, end_idx) in enumerate(ranges, start=1):
        fold_results.append(
            run_fold(
                config,
                bars,
                start_idx,
                end_idx,
                warmup_bars,
                enable_pyramid=enable_pyramid,
                min_trades_per_fold=min_trades_per_fold,
                fold_number=idx,
            )
        )

    summary = summarize_folds(
        fold_results,
        min_trades_per_fold=min_trades_per_fold,
        baseline_expectancy=baseline_expectancy,
    )

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": config.get("symbol", ""),
        "strategy": config.get("strategy", ""),
        "config_path": str(config_path),
        "data_path": str(data_path),
        "folds_requested": folds,
        "warmup_bars": warmup_bars,
        "min_trades_per_fold": min_trades_per_fold,
        "pyramiding_enabled": bool(enable_pyramid),
        "replay_expectations": replay_expectations,
        "folds": fold_results,
        "summary": summary,
        "status": summary["status"],
    }
    return report


def build_issue_report(
    config_path: Path,
    *,
    status: str,
    detail: str,
    data_path: Path | None = None,
    folds: int = DEFAULT_FOLDS,
    warmup_bars: int = DEFAULT_WARMUP_BARS,
    min_trades_per_fold: int = DEFAULT_MIN_TRADES_PER_FOLD,
    enable_pyramid: bool = False,
) -> dict:
    """Write an explicit non-passing report instead of failing the whole refresh."""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    resolved_data_path = data_path or infer_data_path(config)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": config.get("symbol", ""),
        "strategy": config.get("strategy", ""),
        "config_path": str(config_path),
        "data_path": str(resolved_data_path),
        "folds_requested": folds,
        "warmup_bars": warmup_bars,
        "min_trades_per_fold": min_trades_per_fold,
        "pyramiding_enabled": bool(enable_pyramid),
        "replay_expectations": config.get("replay_expectations", {}),
        "folds": [],
        "summary": {
            "status": status,
            "rationale": detail,
            "folds_total": 0,
            "folds_scored": 0,
            "positive_folds": 0,
            "negative_folds": 0,
            "positive_ratio": 0.0,
            "total_trades": 0,
            "mean_expectancy": 0.0,
            "median_expectancy": 0.0,
            "worst_fold_expectancy": 0.0,
            "best_fold_expectancy": 0.0,
            "median_profit_factor": 0.0,
            "max_fold_drawdown": 0.0,
            "aggregate_total_pnl": 0.0,
        },
        "status": status,
    }


def output_path_for_symbol(symbol: str) -> Path:
    return LOGS_DIR / symbol.lower() / "walkforward_report.json"


def write_report(report: dict) -> Path:
    symbol = str(report.get("symbol", "")).lower()
    out_path = output_path_for_symbol(symbol)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return out_path


def print_report(report: dict, out_path: Path) -> None:
    summary = report["summary"]
    print("=" * 68)
    print(f"  Walk-Forward Validation — {report['symbol']} / {report['strategy']}")
    print("=" * 68)
    print(f"  Status:        {summary['status']}")
    print(f"  Rationale:     {summary['rationale']}")
    print(f"  Scored folds:  {summary['folds_scored']}/{summary['folds_total']}")
    print(f"  Positive:      {summary['positive_folds']} ({summary['positive_ratio']:.0%})")
    print(f"  Mean Exp:      {summary['mean_expectancy']:+.3f}")
    print(f"  Worst Exp:     {summary['worst_fold_expectancy']:+.3f}")
    print(f"  Median PF:     {summary['median_profit_factor']:.3f}")
    print(f"  Max Fold DD:   {summary['max_fold_drawdown']:.3f}")
    print(f"  Total Trades:  {summary['total_trades']}")
    if "expectancy_vs_baseline_ratio" in summary:
        print(
            f"  Vs Baseline:   {summary['mean_expectancy']:+.3f} / "
            f"{summary['baseline_expectancy']:+.3f} = {summary['expectancy_vs_baseline_ratio']:.2f}x"
        )
    print(f"  Report:        {out_path}")
    print("-" * 68)
    for fold in report["folds"]:
        print(
            f"  Fold {fold['fold']}: {fold['status']:>17} | trades={fold['trades']:>2} "
            f"| exp={fold['expectancy']:+.3f} | pf={fold['profit_factor']:.3f} "
            f"| dd={fold['max_drawdown']:.3f}"
        )


def run_one(
    config_path: Path,
    data_path: Path | None,
    folds: int,
    warmup_bars: int,
    min_trades_per_fold: int,
    enable_pyramid: bool,
) -> tuple[dict, Path]:
    try:
        report = build_report(
            config_path=config_path,
            data_path=data_path,
            folds=folds,
            warmup_bars=warmup_bars,
            min_trades_per_fold=min_trades_per_fold,
            enable_pyramid=enable_pyramid,
        )
    except FileNotFoundError as exc:
        report = build_issue_report(
            config_path=config_path,
            status="MISSING_DATA",
            detail=str(exc),
            data_path=data_path,
            folds=folds,
            warmup_bars=warmup_bars,
            min_trades_per_fold=min_trades_per_fold,
            enable_pyramid=enable_pyramid,
        )
    except Exception as exc:
        report = build_issue_report(
            config_path=config_path,
            status="ERROR",
            detail=str(exc),
            data_path=data_path,
            folds=folds,
            warmup_bars=warmup_bars,
            min_trades_per_fold=min_trades_per_fold,
            enable_pyramid=enable_pyramid,
        )
    out_path = write_report(report)
    print_report(report, out_path)
    return report, out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-forward validation for argus_flow configs")
    parser.add_argument("--config", help="Path to a config JSON")
    parser.add_argument("--symbol", help="Symbol to validate, e.g. EURUSD")
    parser.add_argument("--data", help="Override path to historical bar CSV")
    parser.add_argument("--folds", type=int, default=DEFAULT_FOLDS, help="Number of sequential folds")
    parser.add_argument("--warmup-bars", type=int, default=DEFAULT_WARMUP_BARS, help="Warmup bars before each fold")
    parser.add_argument("--min-trades-per-fold", type=int, default=DEFAULT_MIN_TRADES_PER_FOLD, help="Minimum trades needed for a fold to count")
    parser.add_argument("--pyramid", action="store_true", help="Enable pyramiding in the offline harness")
    parser.add_argument("--all-active", action="store_true", help="Run the active validation cohort")
    args = parser.parse_args()

    targets: list[Path] = []
    if args.all_active:
        targets = [
            REPO / runner["config_path"]
            for runner in discover_managed_runners()
            if runner["current_stage"] in (STAGE_WATCHER, STAGE_PAPER) and not runner["live"]
        ]
    else:
        targets = [resolve_config_path(symbol=args.symbol, config_path=args.config)]

    override_data = Path(args.data) if args.data else None
    status_counts: dict[str, int] = {}
    for config_path in targets:
        report, _ = run_one(
            config_path=config_path,
            data_path=override_data,
            folds=args.folds,
            warmup_bars=args.warmup_bars,
            min_trades_per_fold=args.min_trades_per_fold,
            enable_pyramid=args.pyramid,
        )
        status = str(report.get("status", "UNKNOWN")).upper()
        status_counts[status] = status_counts.get(status, 0) + 1

    if len(targets) > 1:
        print("=" * 68)
        print("  Walk-Forward Refresh Summary")
        print("=" * 68)
        for status, count in sorted(status_counts.items()):
            print(f"  {status:>14}: {count}")


if __name__ == "__main__":
    main()
