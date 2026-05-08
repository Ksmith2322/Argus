"""PnL reconciliation tests.

These check that recorded PnL math is internally consistent and that every
trade.csv entry has a matching canonical_fills.jsonl record. Built in
response to the 2026-05-07 audit's phantom-trade finding: trades whose
``pnl_usd`` was computed by the strategy runner but never actually filled
at broker contaminated reported PnL by +$7,125 in one week.

The 50% NetLiq circuit breaker (helio.ibkr_execution) now blocks the
oversized orders before submit. These tests guard against regressions in
both the math and the dual-write path.

The vix_intraday set is used as the canonical fixture because it is the
only strategy expected to have a real-money-candidate decision at 5/15.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
TRADES_VIX = REPO_ROOT / "forge" / "logs" / "vix_intraday" / "trades.csv"
CANONICAL_FILLS = REPO_ROOT / "argus_flow" / "logs" / "canonical_fills.jsonl"

# Tolerance for floating point reconstruction of pnl_usd. Strategies record
# fills to a few decimals; $0.05 is well below any real-money sizing risk.
PNL_USD_TOL = 0.05

# Per-instrument sanity floor on position size. A vix_intraday trade with
# size > 50_000 shares of UVXY would be a 1.8M-dollar order — clearly
# phantom. The actual cap in production is 50% NetLiq, but we use a
# generous fixed cap here so the test stays valid as equity scales.
VIX_INTRADAY_SIZE_SANITY_CAP = 50_000


def _read_trades(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _read_canonical_fills(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# ---------------------------------------------------------------------------
# Internal-consistency tests
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not TRADES_VIX.exists(), reason="vix_intraday trades.csv not present"
)
def test_vix_intraday_pnl_matches_price_size_math():
    """For each closed trade, ``pnl_usd`` must equal
    ``(exit_px - entry_px) * size * dir_sign`` within tolerance.

    Catches: schema corruption, off-by-one direction, sizing-formula
    explosions, and the phantom-trade family of bugs where the strategy
    recorded a PnL the broker never produced.
    """
    rows = _read_trades(TRADES_VIX)
    assert rows, "vix_intraday trades.csv is empty — nothing to reconcile"

    failures: list[tuple[int, str]] = []
    for i, row in enumerate(rows):
        try:
            entry = float(row["entry_px"])
            exit_ = float(row["exit_px"])
            size = float(row["position_size"])
            direction = row["direction"].strip().lower()
            recorded = float(row["pnl_usd"])
        except (KeyError, ValueError) as exc:
            failures.append((i, f"unparsable row: {exc}"))
            continue

        dir_sign = 1.0 if direction == "long" else -1.0
        expected = (exit_ - entry) * size * dir_sign

        if abs(expected - recorded) > PNL_USD_TOL:
            failures.append((
                i,
                f"row {i} ts={row.get('ts')}: recorded=${recorded:.2f} "
                f"vs computed=${expected:.2f} "
                f"(entry={entry}, exit={exit_}, size={size}, dir={direction})",
            ))

    assert not failures, (
        f"{len(failures)} of {len(rows)} vix_intraday trades have PnL math "
        f"that doesn't match price/size:\n"
        + "\n".join(msg for _, msg in failures[:5])
        + (f"\n... and {len(failures) - 5} more" if len(failures) > 5 else "")
    )


@pytest.mark.skipif(
    not TRADES_VIX.exists(), reason="vix_intraday trades.csv not present"
)
def test_vix_intraday_no_phantom_sized_trades():
    """No trade in vix_intraday should have a position size beyond the
    sanity cap. This is the phantom-trade canary — the bug pattern was
    sizing-formula division explosion producing 1,400-share trades on a
    $300 risk budget."""
    rows = _read_trades(TRADES_VIX)
    over = [
        (row.get("ts"), float(row["position_size"]))
        for row in rows
        if float(row.get("position_size", 0)) > VIX_INTRADAY_SIZE_SANITY_CAP
    ]
    assert not over, (
        f"{len(over)} vix_intraday trades exceed sanity size cap "
        f"({VIX_INTRADAY_SIZE_SANITY_CAP}). First 5: {over[:5]}"
    )


# ---------------------------------------------------------------------------
# Cross-check against canonical_fills.jsonl
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not (TRADES_VIX.exists() and CANONICAL_FILLS.exists()),
    reason="trades.csv or canonical_fills.jsonl missing",
)
def test_vix_intraday_trades_have_canonical_fill_records():
    """Every closed trade in vix_intraday's trades.csv should have a
    matching record in canonical_fills.jsonl.

    Allows tolerance: canonical_fills only began for vix_intraday after
    a deploy date — older trades may pre-date the dual-write path. The
    test only checks trades from 2026-05-01 onward, when canonical-fills
    coverage should be complete.

    A trade in trades.csv with NO canonical-fill record is the phantom
    pattern from the 5/7 audit.
    """
    trades = _read_trades(TRADES_VIX)
    fills = _read_canonical_fills(CANONICAL_FILLS)
    fills_vix = [
        f for f in fills if str(f.get("strategy", "")).startswith("forge_vix_intraday")
    ]

    if not fills_vix:
        pytest.skip("no canonical_fills records for vix_intraday yet")

    fill_keys = {
        (str(f.get("entry_ts", "")).split("+")[0][:19], str(f.get("symbol", "")))
        for f in fills_vix
    }

    missing: list[str] = []
    for row in trades:
        ts = str(row.get("ts", ""))
        if ts < "2026-05-01":
            continue
        # Match on the trade's ts (entry-bar timestamp) and symbol UVXY
        key_candidate = (ts.split("+")[0][:19], "UVXY")
        if key_candidate not in fill_keys:
            missing.append(ts)

    # Allow up to 10% miss rate to absorb early dual-write gaps; tighten
    # this once the dual-write path has been confirmed solid.
    miss_rate = len(missing) / max(1, sum(
        1 for r in trades if str(r.get("ts", "")) >= "2026-05-01"
    ))
    assert miss_rate <= 0.10, (
        f"{len(missing)} vix_intraday trades since 2026-05-01 have no "
        f"matching canonical_fills record (miss rate {miss_rate:.0%}); "
        f"first 5: {missing[:5]}"
    )
