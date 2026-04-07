#!/usr/bin/env python3
"""
ops/validate_candles.py

Sanity-check downloaded candle CSV files before wasting hours on a bad dataset.
Detects: gaps, stale data, unrealistic OHLCV values, duplicate epochs, wrong order.

Usage:
  python ops/validate_candles.py                          # validate all tracked pairs
  python ops/validate_candles.py data/eth_usd_1m.csv      # validate one file
  python ops/validate_candles.py --strict                  # fail on warnings too

Exit codes: 0 = PASS, 1 = FAIL (errors found), 2 = WARN (warnings only, no --strict)
"""
from __future__ import annotations

import csv
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
GRANULARITY_S = 60  # 1-minute candles

# Tracked pairs — must match ops/refresh_candles.ps1
TRACKED_PAIRS = [
    ("ETH-USD", "eth_usd_1m.csv"),
    ("BTC-USD", "btc_usd_1m.csv"),
    ("SOL-USD", "sol_usd_1m.csv"),
]

# Validation thresholds
MAX_GAP_CANDLES = 5          # >5 missing candles in a row = error
WARN_GAP_CANDLES = 2         # >2 missing candles = warning
MAX_PRICE_USD = 500_000      # no single asset should exceed this
MIN_PRICE_USD = 0.0001       # below this is suspicious
MAX_VOLUME_PER_CANDLE = 1e12 # absurdly high volume = bad data
MIN_CANDLES_EXPECTED = 1000  # fewer than this = probably stale/truncated
MAX_DUPLICATE_PCT = 0.1      # >0.1% duplicates = warning
OHLC_SANITY = True           # check high >= low, high >= open/close, etc.


@dataclass
class ValidationResult:
    path: str
    candle_count: int = 0
    first_epoch: int = 0
    last_epoch: int = 0
    span_days: float = 0.0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        if self.passed and self.warnings:
            status = "WARN"
        lines = [f"[{status}] {self.path}"]
        lines.append(f"  candles={self.candle_count} span={self.span_days:.1f}d")
        if self.first_epoch:
            t0 = datetime.fromtimestamp(self.first_epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            t1 = datetime.fromtimestamp(self.last_epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            lines.append(f"  range={t0} -> {t1}")
        for e in self.errors:
            lines.append(f"  ERROR: {e}")
        for w in self.warnings:
            lines.append(f"  WARN:  {w}")
        return "\n".join(lines)


def validate_candle_csv(path: str) -> ValidationResult:
    res = ValidationResult(path=path)

    if not os.path.exists(path):
        res.errors.append("File not found")
        return res

    # Read CSV
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    if not rows:
        res.errors.append("Empty file")
        return res

    # Detect header
    header = rows[0]
    has_header = any(h.strip().lower() in ("time", "epoch", "open", "close") for h in header)
    body = rows[1:] if has_header else rows

    if not body:
        res.errors.append("No data rows")
        return res

    # Parse candles: expect coinbase_exchange format (time, low, high, open, close, volume)
    epochs = []
    bad_rows = 0
    ohlc_violations = 0
    negative_volume = 0
    extreme_prices = 0

    for i, row in enumerate(body):
        if len(row) < 5:
            bad_rows += 1
            continue

        try:
            epoch = int(row[0].strip())
            # normalize ms to s
            if epoch >= 100_000_000_000:
                epoch = epoch // 1000
        except (ValueError, IndexError):
            bad_rows += 1
            continue

        try:
            low = float(row[1])
            high = float(row[2])
            opn = float(row[3])
            close = float(row[4])
            vol = float(row[5]) if len(row) > 5 and row[5].strip() else 0.0
        except (ValueError, IndexError):
            bad_rows += 1
            continue

        epochs.append(epoch)

        # OHLC sanity
        if OHLC_SANITY:
            if high < low:
                ohlc_violations += 1
            if high < max(opn, close) - 0.01:
                ohlc_violations += 1
            if low > min(opn, close) + 0.01:
                ohlc_violations += 1

        # Price range
        for px in (opn, high, low, close):
            if px > MAX_PRICE_USD or px < MIN_PRICE_USD:
                extreme_prices += 1
                break

        # Volume
        if vol < 0:
            negative_volume += 1
        if vol > MAX_VOLUME_PER_CANDLE:
            extreme_prices += 1

    res.candle_count = len(epochs)

    if res.candle_count == 0:
        res.errors.append("No valid candles parsed")
        return res

    if bad_rows > 0:
        if bad_rows > res.candle_count * 0.01:
            res.errors.append(f"{bad_rows} unparseable rows ({bad_rows/len(body)*100:.1f}%)")
        else:
            res.warnings.append(f"{bad_rows} unparseable rows")

    # Sort check
    sorted_epochs = sorted(epochs)
    if epochs != sorted_epochs:
        res.warnings.append("Epochs not in ascending order (loader will sort, but source may be corrupt)")

    # Use sorted for gap analysis
    epochs = sorted_epochs
    res.first_epoch = epochs[0]
    res.last_epoch = epochs[-1]
    res.span_days = (res.last_epoch - res.first_epoch) / 86400.0

    # Duplicate check
    unique_epochs = set(epochs)
    dupe_count = len(epochs) - len(unique_epochs)
    if dupe_count > 0:
        dupe_pct = dupe_count / len(epochs) * 100
        if dupe_pct > MAX_DUPLICATE_PCT:
            res.warnings.append(f"{dupe_count} duplicate epochs ({dupe_pct:.2f}%)")

    # Gap analysis
    gap_errors = 0
    gap_warnings = 0
    max_gap_seen = 0
    prev = epochs[0]
    for e in epochs[1:]:
        gap_candles = (e - prev) // GRANULARITY_S - 1
        if gap_candles > max_gap_seen:
            max_gap_seen = gap_candles
        if gap_candles > MAX_GAP_CANDLES:
            gap_errors += 1
        elif gap_candles > WARN_GAP_CANDLES:
            gap_warnings += 1
        prev = e

    if gap_errors > 0:
        res.errors.append(f"{gap_errors} large gaps (>{MAX_GAP_CANDLES} candles missing), max_gap={max_gap_seen}")
    if gap_warnings > 0:
        res.warnings.append(f"{gap_warnings} small gaps (>{WARN_GAP_CANDLES} candles missing)")

    # Stale data check
    if res.candle_count < MIN_CANDLES_EXPECTED:
        res.warnings.append(f"Only {res.candle_count} candles (expected >={MIN_CANDLES_EXPECTED})")

    # OHLC violations
    if ohlc_violations > 0:
        if ohlc_violations > res.candle_count * 0.01:
            res.errors.append(f"{ohlc_violations} OHLC violations (high<low or similar)")
        else:
            res.warnings.append(f"{ohlc_violations} OHLC violations")

    # Extreme prices
    if extreme_prices > 0:
        res.warnings.append(f"{extreme_prices} candles with extreme price/volume values")

    if negative_volume > 0:
        res.errors.append(f"{negative_volume} candles with negative volume")

    return res


def main() -> int:
    args = sys.argv[1:]
    strict = "--strict" in args
    paths = [a for a in args if not a.startswith("--")]

    if not paths:
        # Validate all tracked pairs
        data_dir = REPO_ROOT / "data"
        paths = [str(data_dir / fname) for _, fname in TRACKED_PAIRS]
        # Only validate files that exist
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            print("No candle files found in data/")
            return 1

    results = []
    for p in paths:
        r = validate_candle_csv(p)
        results.append(r)
        print(r.summary())
        print()

    has_errors = any(not r.passed for r in results)
    has_warnings = any(r.warnings for r in results)

    if has_errors:
        print("=== VALIDATION FAILED ===")
        return 1
    if has_warnings and strict:
        print("=== VALIDATION FAILED (--strict) ===")
        return 1
    if has_warnings:
        print("=== VALIDATION PASSED (with warnings) ===")
        return 2

    print("=== VALIDATION PASSED ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())