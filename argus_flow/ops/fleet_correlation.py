"""Fleet PnL Correlation & PCA — measure true diversification across instruments.

Key question: 15 instruments but how many independent bets?
If most pairs are correlated, a "diversified" fleet is concentrated risk.

Computes:
- Pairwise daily PnL correlation matrix
- PCA: how many principal components explain 90% of variance
- Worst-day analysis: do all pairs lose together?
- Effective independent bets (1 / sum of squared PCA weights)

Usage:
    python -m argus_flow.ops.fleet_correlation
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
LOGS_ROOT = REPO / "argus_flow" / "logs"
OUTPUT_PATH = LOGS_ROOT / "fleet_correlation_report.json"


def _read_daily_pnl(log_dir: Path) -> dict[str, float]:
    """Read trades.csv and aggregate PnL by date."""
    trade_file = log_dir / "trades.csv"
    if not trade_file.exists():
        return {}

    daily = defaultdict(float)
    try:
        with open(trade_file, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ts_str = row.get("ts", row.get("timestamp", ""))
                pnl_usd = float(row.get("pnl_usd", 0))
                if ts_str:
                    try:
                        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        day = dt.strftime("%Y-%m-%d")
                        daily[day] += pnl_usd
                    except Exception:
                        pass
    except Exception:
        pass
    return dict(daily)


def _build_daily_matrix(daily_by_symbol: dict[str, dict[str, float]]) -> tuple[list[str], list[str], np.ndarray]:
    """Build aligned daily PnL matrix. Missing days = 0 (no trade = no PnL)."""
    all_days = sorted(set(d for daily in daily_by_symbol.values() for d in daily))
    symbols = sorted(daily_by_symbol.keys())

    if not all_days or not symbols:
        return [], [], np.array([])

    matrix = np.zeros((len(all_days), len(symbols)))
    for j, sym in enumerate(symbols):
        daily = daily_by_symbol[sym]
        for i, day in enumerate(all_days):
            matrix[i, j] = daily.get(day, 0.0)

    return all_days, symbols, matrix


def _correlation_matrix(matrix: np.ndarray) -> np.ndarray:
    """Compute pairwise Pearson correlation, handling constant columns."""
    n_cols = matrix.shape[1]
    corr = np.eye(n_cols)
    for i in range(n_cols):
        for j in range(i + 1, n_cols):
            x, y = matrix[:, i], matrix[:, j]
            # Only compute correlation if both have variance
            if np.std(x) > 0 and np.std(y) > 0:
                r = np.corrcoef(x, y)[0, 1]
                corr[i, j] = r
                corr[j, i] = r
            else:
                corr[i, j] = 0
                corr[j, i] = 0
    return corr


def _pca_analysis(matrix: np.ndarray) -> dict:
    """PCA analysis: eigenvalues, explained variance, effective bets."""
    # Center the data
    centered = matrix - matrix.mean(axis=0)

    # Covariance matrix
    cov = np.cov(centered.T)
    if cov.ndim == 0:
        return {"n_components_90pct": 1, "effective_bets": 1.0, "explained_variance": [1.0]}

    eigenvalues = np.linalg.eigvalsh(cov)
    eigenvalues = np.sort(eigenvalues)[::-1]  # descending
    eigenvalues = np.maximum(eigenvalues, 0)  # clip numerical noise

    total_var = eigenvalues.sum()
    if total_var == 0:
        return {"n_components_90pct": len(eigenvalues), "effective_bets": float(len(eigenvalues)),
                "explained_variance": []}

    explained = eigenvalues / total_var

    # Components for 90% variance
    cumulative = np.cumsum(explained)
    n_90 = int(np.searchsorted(cumulative, 0.9) + 1)

    # Effective number of independent bets (Herfindahl-based)
    # = 1 / sum(w_i^2) where w_i are explained variance ratios
    herfindahl = np.sum(explained ** 2)
    effective_bets = 1.0 / herfindahl if herfindahl > 0 else len(eigenvalues)

    return {
        "n_components_90pct": n_90,
        "effective_bets": round(effective_bets, 2),
        "explained_variance": [round(float(e), 4) for e in explained[:10]],
    }


def _worst_day_analysis(days: list[str], matrix: np.ndarray, symbols: list[str]) -> list[dict]:
    """Find worst days and show per-instrument breakdown."""
    daily_total = matrix.sum(axis=1)  # total fleet PnL per day
    worst_indices = np.argsort(daily_total)[:5]  # 5 worst days

    results = []
    for idx in worst_indices:
        if daily_total[idx] >= 0:
            continue  # skip non-losing days
        day_detail = {
            "date": days[idx],
            "fleet_pnl_usd": round(float(daily_total[idx]), 2),
            "instruments_losing": 0,
            "instruments_winning": 0,
            "breakdown": {},
        }
        for j, sym in enumerate(symbols):
            pnl = float(matrix[idx, j])
            if pnl != 0:
                day_detail["breakdown"][sym] = round(pnl, 2)
                if pnl < 0:
                    day_detail["instruments_losing"] += 1
                else:
                    day_detail["instruments_winning"] += 1
        results.append(day_detail)

    return results


def generate_report() -> dict:
    """Generate full fleet correlation and PCA report."""
    daily_by_symbol = {}

    if not LOGS_ROOT.exists():
        return {"timestamp": datetime.now(timezone.utc).isoformat(), "error": "no logs directory"}

    for log_dir in sorted(LOGS_ROOT.iterdir()):
        if not log_dir.is_dir() or log_dir.name.startswith("_"):
            continue
        daily = _read_daily_pnl(log_dir)
        if daily:
            daily_by_symbol[log_dir.name.upper()] = daily

    if len(daily_by_symbol) < 2:
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "INSUFFICIENT_DATA",
            "instruments_with_trades": len(daily_by_symbol),
            "minimum_required": 2,
        }

    days, symbols, matrix = _build_daily_matrix(daily_by_symbol)
    corr = _correlation_matrix(matrix)
    pca = _pca_analysis(matrix)
    worst_days = _worst_day_analysis(days, matrix, symbols)

    # High correlation pairs (|r| > 0.5)
    high_corr_pairs = []
    for i in range(len(symbols)):
        for j in range(i + 1, len(symbols)):
            r = corr[i, j]
            if abs(r) > 0.5:
                high_corr_pairs.append({
                    "pair": f"{symbols[i]}/{symbols[j]}",
                    "correlation": round(float(r), 3),
                })
    high_corr_pairs.sort(key=lambda x: -abs(x["correlation"]))

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "OK",
        "instruments": symbols,
        "trading_days": len(days),
        "date_range": {"start": days[0], "end": days[-1]} if days else {},
        "pca": pca,
        "high_correlation_pairs": high_corr_pairs,
        "correlation_matrix": {
            symbols[i]: {symbols[j]: round(float(corr[i, j]), 3) for j in range(len(symbols))}
            for i in range(len(symbols))
        },
        "worst_days": worst_days,
        "summary": {
            "effective_independent_bets": pca["effective_bets"],
            "components_for_90pct": pca["n_components_90pct"],
            "highly_correlated_pairs": len(high_corr_pairs),
            "total_instruments": len(symbols),
        },
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    return report


def main():
    print("=" * 60)
    print(f"  Fleet Correlation & PCA — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print("=" * 60)

    report = generate_report()

    if report.get("status") == "INSUFFICIENT_DATA":
        print(f"\n  Insufficient data: {report['instruments_with_trades']} instruments have trades (need >= 2)")
        return

    if report.get("status") != "OK":
        print(f"\n  Error: {report.get('error', 'unknown')}")
        return

    summary = report["summary"]
    print(f"\n  Instruments: {summary['total_instruments']}")
    print(f"  Trading days: {report['trading_days']}")
    print(f"  Effective independent bets: {summary['effective_independent_bets']}")
    print(f"  Components for 90% variance: {summary['components_for_90pct']}")

    if report["high_correlation_pairs"]:
        print(f"\n  Highly correlated pairs (|r| > 0.5):")
        for pair in report["high_correlation_pairs"][:10]:
            print(f"    {pair['pair']:>20s}: r = {pair['correlation']:+.3f}")
    else:
        print(f"\n  No highly correlated pairs found.")

    if report["worst_days"]:
        print(f"\n  Worst fleet days:")
        for wd in report["worst_days"]:
            losing = wd["instruments_losing"]
            total = losing + wd["instruments_winning"]
            print(f"    {wd['date']}: ${wd['fleet_pnl_usd']:+.2f} ({losing}/{total} instruments losing)")

    print(f"\n  Report: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
