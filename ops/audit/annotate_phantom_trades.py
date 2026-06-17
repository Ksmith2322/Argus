"""Create a sidecar annotation ledger for phantom trades.

This deliberately does not rewrite strategy ``trades.csv`` files. Raw artifacts
stay raw; the annotation ledger is the analytical truth layer that says which
rows must be excluded from promotion, ROI, and PnL scoring.
"""

from __future__ import annotations

import csv
from pathlib import Path

from ops.audit.argus_audit_engine import OUT_DIR, REPO, is_phantom_trade, read_csv, rel, trade_pnl, write_csv


def build_annotations() -> list[dict]:
    rows: list[dict] = []
    for path in sorted(list(REPO.glob("forge/logs/*/trades.csv")) + list(REPO.glob("argus_flow/logs/*/trades.csv"))):
        trades = read_csv(path)
        for idx, trade in enumerate(trades, start=2):  # account for CSV header
            if not is_phantom_trade(trade):
                continue
            rows.append({
                "source_file": rel(path),
                "source_line": idx,
                "strategy": rel(path).split("/")[2] if rel(path).startswith("forge/logs/") else rel(path).split("/")[2],
                "symbol": trade.get("symbol", ""),
                "ts": trade.get("ts") or trade.get("entry_ts") or trade.get("timestamp") or "",
                "pnl_usd": round(trade_pnl(trade), 2),
                "position_size": trade.get("position_size") or trade.get("qty") or trade.get("size") or "",
                "notional_usd": trade.get("notional_usd", ""),
                "annotation": "PHANTOM",
                "promotion_policy": "exclude_from_promotion_roi_and_pnl_scoring",
                "reason": "phantom-sized or explicitly phantom/rejected trade; requires broker-fill confirmation before scoring",
            })
    return rows


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = build_annotations()
    write_csv(OUT_DIR / "phantom_trade_annotations.csv", rows)
    print(f"wrote {len(rows)} phantom annotations to {OUT_DIR / 'phantom_trade_annotations.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
