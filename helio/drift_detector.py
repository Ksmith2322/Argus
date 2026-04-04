"""
Feature distribution drift detector for Helio/Argus trading systems.

Compares live feature distributions against baseline (training/backtest)
distributions and alerts when significant drift occurs.

Usage:
    python -m helio.drift_detector           # run detection, print report
    python -m helio.drift_detector --build   # build baselines from signals
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "helio" / "data" / "feature_baselines.json"
REPORT_PATH = REPO_ROOT / "helio" / "logs" / "drift_report.json"

SIGNAL_DIRS = [
    REPO_ROOT / "argus_flow" / "logs",   # argus_flow/<pair>/signals.csv
    REPO_ROOT / "helio" / "logs",         # helio/<strategy>/signals.csv
]

# Features we track for drift (numeric columns present in signals.csv)
TRACKED_FEATURES = [
    "range_pct",
    "vol_z",
    "range_accel",
    "dist_from_low",
    "trend_strength",
    "efficiency_ratio",
]

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DriftedFeature:
    name: str
    type: str          # "mean_shift" | "variance" | "range_escape"
    severity: str      # "warning" | "critical"
    details: str


@dataclass
class DriftReport:
    features_checked: int = 0
    drifted_features: list[DriftedFeature] = field(default_factory=list)
    overall_health: str = "GREEN"
    timestamp: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ---------------------------------------------------------------------------
# Baseline I/O
# ---------------------------------------------------------------------------

_DEFAULT_BASELINES = {
    "range_pct": {"mean": 0.003, "std": 0.002, "p25": 0.0015, "p50": 0.0028, "p75": 0.004, "min": 0.0, "max": 0.02, "n": 1000},
    "vol_z":     {"mean": 0.0, "std": 1.0, "p25": -0.67, "p50": 0.0, "p75": 0.67, "min": -3.0, "max": 3.0, "n": 1000},
    "range_accel": {"mean": 0.0, "std": 0.5, "p25": -0.3, "p50": 0.0, "p75": 0.3, "min": -2.0, "max": 2.0, "n": 1000},
    "dist_from_low": {"mean": 0.5, "std": 0.28, "p25": 0.25, "p50": 0.5, "p75": 0.75, "min": 0.0, "max": 1.0, "n": 1000},
    "trend_strength": {"mean": 0.3, "std": 0.25, "p25": 0.1, "p50": 0.25, "p75": 0.45, "min": 0.0, "max": 1.0, "n": 1000},
    "efficiency_ratio": {"mean": 0.15, "std": 0.12, "p25": 0.05, "p50": 0.12, "p75": 0.22, "min": 0.0, "max": 1.0, "n": 1000},
}


def load_baselines(path: Path = BASELINE_PATH) -> dict:
    """Load baseline stats from JSON. Creates template if missing."""
    if path.exists():
        with open(path, "r") as f:
            return json.load(f)
    # Create template with defaults
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(_DEFAULT_BASELINES, f, indent=2)
    return dict(_DEFAULT_BASELINES)


def save_baselines(baselines: dict, path: Path = BASELINE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(baselines, f, indent=2)


# ---------------------------------------------------------------------------
# Live data collection
# ---------------------------------------------------------------------------

def collect_live_signals(
    signal_dirs: list[Path] = SIGNAL_DIRS,
    max_rows_per_file: int = 5000,
) -> pd.DataFrame:
    """Read recent signals from all signal dirs, return combined DataFrame."""
    frames: list[pd.DataFrame] = []
    for base_dir in signal_dirs:
        if not base_dir.exists():
            continue
        for csv_path in base_dir.rglob("signals.csv"):
            try:
                df = pd.read_csv(csv_path, nrows=max_rows_per_file)
                df["_source"] = str(csv_path.relative_to(REPO_ROOT))
                frames.append(df)
            except Exception:
                continue
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------

MEAN_SHIFT_THRESHOLD = 2.0     # |live_mean - base_mean| / base_std
VARIANCE_RATIO_HIGH = 2.0      # live_std / base_std
VARIANCE_RATIO_LOW = 0.5
RANGE_ESCAPE_PCT = 0.20        # fraction of live values outside baseline range


def _check_feature(
    name: str,
    live_values: np.ndarray,
    baseline: dict,
) -> list[DriftedFeature]:
    """Run all drift checks on a single feature. Returns list of drifts found."""
    results: list[DriftedFeature] = []
    live_mean = float(np.nanmean(live_values))
    live_std = float(np.nanstd(live_values))
    base_mean = baseline["mean"]
    base_std = baseline["std"]
    base_min = baseline["min"]
    base_max = baseline["max"]

    # Guard against zero std
    if base_std < 1e-12:
        base_std = 1e-12

    # 1. Mean shift
    shift = abs(live_mean - base_mean) / base_std
    if shift > MEAN_SHIFT_THRESHOLD:
        severity = "critical" if shift > 4.0 else "warning"
        results.append(DriftedFeature(
            name=name,
            type="mean_shift",
            severity=severity,
            details=f"shift={shift:.2f}x std (live_mean={live_mean:.4f}, base_mean={base_mean:.4f})",
        ))

    # 2. Variance change
    ratio = live_std / base_std
    if ratio > VARIANCE_RATIO_HIGH or ratio < VARIANCE_RATIO_LOW:
        severity = "critical" if ratio > 4.0 or ratio < 0.25 else "warning"
        results.append(DriftedFeature(
            name=name,
            type="variance",
            severity=severity,
            details=f"std_ratio={ratio:.2f} (live_std={live_std:.4f}, base_std={base_std:.4f})",
        ))

    # 3. Range escape
    n_total = len(live_values)
    if n_total > 0:
        outside = np.sum((live_values < base_min) | (live_values > base_max))
        escape_frac = outside / n_total
        if escape_frac > RANGE_ESCAPE_PCT:
            severity = "critical" if escape_frac > 0.40 else "warning"
            results.append(DriftedFeature(
                name=name,
                type="range_escape",
                severity=severity,
                details=f"{escape_frac:.1%} outside [{base_min}, {base_max}]",
            ))

    return results


def detect_drift(
    baselines: Optional[dict] = None,
    live_df: Optional[pd.DataFrame] = None,
) -> DriftReport:
    """Run drift detection across all tracked features."""
    if baselines is None:
        baselines = load_baselines()
    if live_df is None:
        live_df = collect_live_signals()

    report = DriftReport(timestamp=datetime.now(timezone.utc).isoformat())

    if live_df.empty:
        report.overall_health = "GREEN"
        return report

    features_checked = 0
    all_drifts: list[DriftedFeature] = []

    for feat in TRACKED_FEATURES:
        if feat not in live_df.columns or feat not in baselines:
            continue
        values = live_df[feat].dropna().values.astype(float)
        if len(values) < 10:
            continue
        features_checked += 1
        drifts = _check_feature(feat, values, baselines[feat])
        all_drifts.extend(drifts)

    report.features_checked = features_checked
    report.drifted_features = all_drifts

    # Determine overall health
    n_critical = sum(1 for d in all_drifts if d.severity == "critical")
    n_warning = sum(1 for d in all_drifts if d.severity == "warning")
    if n_critical > 0 or (n_warning >= 3):
        report.overall_health = "RED"
    elif n_warning > 0:
        report.overall_health = "YELLOW"
    else:
        report.overall_health = "GREEN"

    return report


def write_report(report: DriftReport, path: Path = REPORT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(report.to_dict(), f, indent=2)


# ---------------------------------------------------------------------------
# Baseline builder
# ---------------------------------------------------------------------------

def build_baseline(csv_paths: list[Path], output: Path = BASELINE_PATH) -> dict:
    """Generate baseline stats from backtest/historical signal CSVs."""
    frames: list[pd.DataFrame] = []
    for p in csv_paths:
        if not p.exists():
            print(f"  SKIP (not found): {p}")
            continue
        try:
            df = pd.read_csv(p)
            frames.append(df)
        except Exception as exc:
            print(f"  SKIP (error): {p} — {exc}")
            continue

    if not frames:
        print("ERROR: no valid CSVs found — baseline not built.")
        return {}

    combined = pd.concat(frames, ignore_index=True)
    baselines: dict = {}

    for feat in TRACKED_FEATURES:
        if feat not in combined.columns:
            continue
        vals = combined[feat].dropna().values.astype(float)
        if len(vals) < 5:
            continue
        baselines[feat] = {
            "mean": round(float(np.nanmean(vals)), 6),
            "std": round(float(np.nanstd(vals)), 6),
            "p25": round(float(np.nanpercentile(vals, 25)), 6),
            "p50": round(float(np.nanpercentile(vals, 50)), 6),
            "p75": round(float(np.nanpercentile(vals, 75)), 6),
            "min": round(float(np.nanmin(vals)), 6),
            "max": round(float(np.nanmax(vals)), 6),
            "n": int(len(vals)),
        }

    save_baselines(baselines, output)
    print(f"Baseline saved to {output} ({len(baselines)} features, "
          f"{len(combined)} total rows from {len(frames)} files)")
    return baselines


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_report(report: DriftReport) -> None:
    health_colors = {"GREEN": "OK", "YELLOW": "WARN", "RED": "ALERT"}
    label = health_colors.get(report.overall_health, "???")
    print(f"\n=== Drift Report [{label}] {report.overall_health} ===")
    print(f"  Timestamp:        {report.timestamp}")
    print(f"  Features checked: {report.features_checked}")
    print(f"  Drifts detected:  {len(report.drifted_features)}")

    if report.drifted_features:
        print()
        for d in report.drifted_features:
            tag = "CRIT" if d.severity == "critical" else "WARN"
            print(f"  [{tag}] {d.name} ({d.type}): {d.details}")
    else:
        print("  No drift detected.")
    print()


def main() -> None:
    args = sys.argv[1:]

    if "--build" in args:
        # Build baselines from all available signal CSVs
        csvs: list[Path] = []
        for base_dir in SIGNAL_DIRS:
            if base_dir.exists():
                csvs.extend(base_dir.rglob("signals.csv"))
        if not csvs:
            print("No signals.csv files found in signal directories.")
            sys.exit(1)
        print(f"Building baseline from {len(csvs)} signal files...")
        build_baseline(csvs)
        sys.exit(0)

    # Default: run drift detection
    baselines = load_baselines()
    live_df = collect_live_signals()

    if live_df.empty:
        print("No live signal data found. Nothing to check.")
        sys.exit(0)

    report = detect_drift(baselines, live_df)
    write_report(report)
    _print_report(report)

    # Exit code: 0=green, 1=yellow, 2=red
    code = {"GREEN": 0, "YELLOW": 1, "RED": 2}.get(report.overall_health, 0)
    sys.exit(code)


if __name__ == "__main__":
    main()
