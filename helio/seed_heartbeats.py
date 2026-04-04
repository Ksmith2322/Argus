"""
Seed / update heartbeat.json for all Greek-family instruments.

- Creates missing heartbeats with default values.
- Updates existing heartbeats (DIA, GLD, SPY) to include system/family/stage
  while preserving their existing data.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

LOGS_DIR = Path(__file__).resolve().parent / "logs"

# Instrument definitions: (dir_name, symbol, family, extra_fields)
INSTRUMENTS = [
    # Helio swing — equities
    ("dia", "DIA", "helio", {}),
    ("gld", "GLD", "helio", {}),
    ("spy", "SPY", "helio", {}),
    # Helio swing — futures
    ("mes", "MES", "helio", {}),
    ("mgc", "MGC", "helio", {}),
    ("mnq", "MNQ", "helio", {}),
    ("mym", "MYM", "helio", {}),
    # Apollo mean reversion
    ("apollo_audjpy", "AUDJPY", "apollo", {"rsi": 50.0, "dist_atr": 0.0}),
    ("apollo_audusd", "AUDUSD", "apollo", {"rsi": 50.0, "dist_atr": 0.0}),
    ("apollo_cadjpy", "CADJPY", "apollo", {"rsi": 50.0, "dist_atr": 0.0}),
    ("apollo_eurjpy", "EURJPY", "apollo", {"rsi": 50.0, "dist_atr": 0.0}),
    ("apollo_eurusd", "EURUSD", "apollo", {"rsi": 50.0, "dist_atr": 0.0}),
    ("apollo_gbpjpy", "GBPJPY", "apollo", {"rsi": 50.0, "dist_atr": 0.0}),
    ("apollo_gbpusd", "GBPUSD", "apollo", {"rsi": 50.0, "dist_atr": 0.0}),
    ("apollo_usdjpy", "USDJPY", "apollo", {"rsi": 50.0, "dist_atr": 0.0}),
    # Hermes momentum
    ("hermes_gold_f", "GOLD_F", "hermes", {"atr": 0.0}),
]


def build_default(symbol: str, family: str, ts: str, extra: dict) -> dict:
    """Build a default heartbeat payload."""
    hb = {
        "ts": ts,
        "system": "helio",
        "family": family,
        "stage": "watcher",
        "symbol": symbol,
        "position": "FLAT",
        "close": 0.0,
        "trade_count": 0,
        "pnl_total": 0.0,
    }
    hb.update(extra)
    return hb


def main():
    ts = datetime.now(timezone.utc).isoformat()
    created = 0
    updated = 0

    for dir_name, symbol, family, extra in INSTRUMENTS:
        hb_path = LOGS_DIR / dir_name / "heartbeat.json"

        if hb_path.exists():
            # Read existing, merge new required fields
            with open(hb_path) as f:
                data = json.load(f)

            changed = False
            # Insert new fields right after ts if missing
            for key, val in [("system", "helio"), ("family", family), ("stage", "watcher")]:
                if key not in data:
                    data[key] = val
                    changed = True

            # Also add family-specific extras if missing
            for key, val in extra.items():
                if key not in data:
                    data[key] = val
                    changed = True

            if changed:
                # Rebuild with desired key order: ts, system, family, stage first, then rest
                ordered = {}
                priority_keys = ["ts", "system", "family", "stage"]
                for k in priority_keys:
                    if k in data:
                        ordered[k] = data[k]
                for k in data:
                    if k not in ordered:
                        ordered[k] = data[k]

                with open(hb_path, "w") as f:
                    json.dump(ordered, f, indent=2)
                    f.write("\n")
                updated += 1
                print(f"  UPDATED  {hb_path.relative_to(LOGS_DIR)}")
            else:
                print(f"  OK       {hb_path.relative_to(LOGS_DIR)}")
        else:
            # Create new heartbeat
            hb_path.parent.mkdir(parents=True, exist_ok=True)
            data = build_default(symbol, family, ts, extra)
            with open(hb_path, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            created += 1
            print(f"  CREATED  {hb_path.relative_to(LOGS_DIR)}")

    print(f"\nDone: {created} created, {updated} updated, {len(INSTRUMENTS)} total instruments.")


if __name__ == "__main__":
    main()
