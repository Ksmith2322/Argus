"""Deploy Watcher Variants — generate aggressive + conservative configs per FX pair.

Creates 2 variant configs per baseline FX pair, all in watcher stage.
Variants compete to identify which parameter profile produces better signals.
Winner gets promoted to paper; losers get trimmed.

Variant A (aggressive): wider session, looser range gate, shorter gap
Variant B (conservative): tighter session, higher range gate, longer timeout

Usage:
    python -m argus_flow.ops.deploy_variants              # generate all
    python -m argus_flow.ops.deploy_variants --symbol eurusd  # single pair
    python -m argus_flow.ops.deploy_variants --dry-run    # preview only
"""
from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONFIGS_DIR = REPO / "argus_flow" / "configs"

# FX baseline configs to generate variants for
FX_BASELINES = [
    "eurusd_t4_paper_v1.json",
    "gbpusd_range_paper_v1.json",
    "eurjpy_t4_paper_v1.json",
    "gbpjpy_t4_paper_v1.json",
    "usdjpy_ny_paper_v1.json",
    "audusd_ny_paper_v1.json",
    "cadjpy_t4_paper_v1.json",
    "audjpy_t4_paper_v1.json",
]

# Client ID range for variants (100-199, well above primary 10-33)
VARIANT_CLIENT_ID_START = 100


def _variant_a_aggressive(cfg: dict) -> dict:
    """Aggressive variant: wider session window, looser gates, shorter gap."""
    v = copy.deepcopy(cfg)
    t = v.setdefault("trigger", {})
    r = v.setdefault("risk", {})

    # Widen session by 2 hours each side
    start = t.get("session_start_utc", 8)
    end = t.get("session_end_utc", 14)
    t["session_start_utc"] = max(0, start - 2)
    t["session_end_utc"] = min(24, end + 2)

    # Looser range gate
    if t.get("range_pct_min", 0) > 0:
        t["range_pct_min"] = round(t["range_pct_min"] * 0.7, 5)  # 30% lower threshold

    # Shorter signal gap (more signals)
    if r.get("min_signal_gap_minutes", 0) > 5:
        r["min_signal_gap_minutes"] = max(5, r["min_signal_gap_minutes"] - 5)

    # Slightly wider stop (more breathing room)
    if r.get("stop_pips", 0) > 0:
        r["stop_pips"] = round(r["stop_pips"] * 1.2)

    return v


def _variant_b_conservative(cfg: dict) -> dict:
    """Conservative variant: tighter session, stricter gates, longer timeout."""
    v = copy.deepcopy(cfg)
    t = v.setdefault("trigger", {})
    r = v.setdefault("risk", {})

    # Narrow session by 1 hour each side
    start = t.get("session_start_utc", 8)
    end = t.get("session_end_utc", 14)
    t["session_start_utc"] = start + 1
    t["session_end_utc"] = max(start + 2, end - 1)

    # Stricter range gate
    if t.get("range_pct_min", 0) > 0:
        t["range_pct_min"] = round(t["range_pct_min"] * 1.3, 5)  # 30% higher threshold

    # Longer signal gap (more selective)
    r["min_signal_gap_minutes"] = r.get("min_signal_gap_minutes", 15) + 10

    # Tighter stop (less risk per trade)
    if r.get("stop_pips", 0) > 10:
        r["stop_pips"] = round(r["stop_pips"] * 0.8)

    # Longer timeout (let trades develop)
    if r.get("timeout_minutes", 0) > 0:
        r["timeout_minutes"] = round(r["timeout_minutes"] * 1.3)

    return v


VARIANT_DEFS = {
    "aggressive": {
        "suffix": "var_a",
        "label": "Aggressive",
        "transform": _variant_a_aggressive,
        "description": "wider session, looser range gate, shorter gap, wider stop",
    },
    "conservative": {
        "suffix": "var_b",
        "label": "Conservative",
        "transform": _variant_b_conservative,
        "description": "tighter session, stricter range gate, longer gap, tighter stop, longer timeout",
    },
}


def generate_variants(symbol_filter: str | None = None, dry_run: bool = False) -> list[dict]:
    """Generate variant configs for FX pairs."""
    generated = []
    client_id = VARIANT_CLIENT_ID_START

    baselines = FX_BASELINES
    if symbol_filter:
        target = symbol_filter.lower()
        baselines = [b for b in baselines if target in b.lower()]

    for baseline_name in baselines:
        baseline_path = CONFIGS_DIR / baseline_name
        if not baseline_path.exists():
            print(f"  SKIP: {baseline_name} not found")
            continue

        base_cfg = json.loads(baseline_path.read_text(encoding="utf-8"))
        symbol = base_cfg.get("symbol", "").upper()

        for variant_key, vdef in VARIANT_DEFS.items():
            suffix = vdef["suffix"]
            out_name = f"{symbol.lower()}_{suffix}_watcher_v1.json"
            out_path = CONFIGS_DIR / out_name

            # Skip if already exists
            if out_path.exists() and not dry_run:
                existing = json.loads(out_path.read_text(encoding="utf-8"))
                if existing.get("deployment", {}).get("managed"):
                    generated.append({
                        "symbol": symbol,
                        "variant": variant_key,
                        "path": str(out_path),
                        "status": "EXISTS",
                    })
                    client_id += 1
                    continue

            # Generate variant
            variant_cfg = vdef["transform"](base_cfg)

            # Set metadata
            variant_cfg["version"] = f"watcher_v1_{suffix}"
            variant_cfg["strategy"] = base_cfg.get("strategy", "unknown") + f"_{suffix}"
            variant_cfg["ibkr_client_id"] = client_id

            # Mark as managed watcher
            variant_cfg["deployment"] = {
                "managed": True,
                "stage": "watcher",
                "variant_of": baseline_name,
                "variant_type": variant_key,
                "variant_label": vdef["label"],
                "variant_description": vdef["description"],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            # Watcher doesn't trade — lot_size and risk_pct don't matter
            # but keep them for replay comparison
            variant_cfg.setdefault("risk", {})["lot_size"] = 0  # signal-only
            variant_cfg["risk"]["risk_pct"] = 0  # no risk in watcher

            info = {
                "symbol": symbol,
                "variant": variant_key,
                "label": vdef["label"],
                "path": str(out_path),
                "status": "CREATED" if not dry_run else "WOULD_CREATE",
                "changes": vdef["description"],
            }

            # Show key parameter diffs
            bt = base_cfg.get("trigger", {})
            vt = variant_cfg.get("trigger", {})
            br = base_cfg.get("risk", {})
            vr = variant_cfg.get("risk", {})
            diffs = {}
            for k in ["session_start_utc", "session_end_utc", "range_pct_min"]:
                if bt.get(k) != vt.get(k):
                    diffs[k] = f"{bt.get(k)} -> {vt.get(k)}"
            for k in ["stop_pips", "min_signal_gap_minutes", "timeout_minutes"]:
                if br.get(k) != vr.get(k):
                    diffs[k] = f"{br.get(k)} -> {vr.get(k)}"
            info["diffs"] = diffs

            if not dry_run:
                out_path.write_text(json.dumps(variant_cfg, indent=4), encoding="utf-8")

            generated.append(info)
            client_id += 1

    return generated


def main():
    parser = argparse.ArgumentParser(description="Deploy watcher variant configs")
    parser.add_argument("--symbol", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print("=" * 60)
    print(f"  Deploy Watcher Variants — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print("=" * 60)

    results = generate_variants(args.symbol, args.dry_run)

    if not results:
        print("\n  No variants generated.")
        return

    by_symbol = {}
    for r in results:
        by_symbol.setdefault(r["symbol"], []).append(r)

    for symbol, variants in sorted(by_symbol.items()):
        print(f"\n  {symbol}:")
        for v in variants:
            status_color = "EXISTS" if v["status"] == "EXISTS" else v["status"]
            print(f"    {v.get('label', v['variant']):>14s} [{status_color}]")
            for k, diff in v.get("diffs", {}).items():
                print(f"      {k}: {diff}")

    created = sum(1 for r in results if r["status"] == "CREATED")
    existed = sum(1 for r in results if r["status"] == "EXISTS")
    print(f"\n  Total: {created} created, {existed} already existed, {len(results)} total")

    if not args.dry_run and created > 0:
        print("\n  Variants are in watcher stage. They'll be auto-discovered on next")
        print("  managed truth refresh and start logging signals immediately.")


if __name__ == "__main__":
    main()
