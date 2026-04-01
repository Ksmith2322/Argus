"""Generate a live config from a paper config for micro-live trading.

Usage:
    python -m argus_flow.ops.generate_live_config argus_flow/configs/gbpusd_range_paper_v1.json
    python -m argus_flow.ops.generate_live_config argus_flow/configs/eurusd_t4_paper_v1.json --lot-size 1000
    python -m argus_flow.ops.generate_live_config --all  # generate for all promoted pairs
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from argus_flow.ops.fleet_registry import (
    RISK_POLICY_DEFAULTS,
    STAGE_REAL,
    default_log_dir,
)

REPO = Path(__file__).resolve().parents[2]
CONFIGS_DIR = REPO / "argus_flow" / "configs"
HASHES_FILE = CONFIGS_DIR / "hashes.json"
GATE_REPORT = REPO / "argus_flow" / "logs" / "promotion_gate_report.json"

# Defaults
DEFAULT_FX_LOT_SIZE = 1000       # 1 micro lot
DEFAULT_FUTURES_CONTRACTS = 1    # 1 contract

# Pip values per standard lot (100,000 units) for common pairs
# Used for pool-based risk sizing: lot_size = (pool * risk_pct) / (stop_pips * pip_value_per_unit)
PIP_VALUE_PER_UNIT = {
    # Non-JPY pairs: 1 pip = 0.0001 per unit in quote currency (≈ $0.0001 for USD-quoted)
    "default": 0.0001,
    # JPY pairs: 1 pip = 0.01 per unit; need USD conversion (~0.000067 at ~150 JPY/USD)
    "jpy": 0.000067,
}


def _sha256_file(path: Path) -> str:
    """Return full SHA-256 hex digest of a file's contents."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _short_hash(path: Path) -> str:
    """Return first 16 hex chars of SHA-256 (matches hashes.json format)."""
    return _sha256_file(path)[:16]


def _load_gate_report() -> dict | None:
    """Load promotion gate report if it exists."""
    if not GATE_REPORT.exists():
        return None
    with open(GATE_REPORT) as f:
        return json.load(f)


def _check_promotion_gate(symbol: str, gate_report: dict | None) -> bool:
    """Check if a symbol has PROMOTE status in the gate report.

    Returns True if promoted, False if not. Prints warnings.
    """
    if gate_report is None:
        print(f"  WARNING: promotion_gate_report.json not found — skipping gate check for {symbol}")
        return True  # allow but warn

    runners = gate_report.get("runners", []) if isinstance(gate_report, dict) else []
    symbol_upper = symbol.upper()
    for entry in runners:
        if not isinstance(entry, dict):
            continue
        entry_symbol = str(entry.get("symbol", "")).upper()
        if entry_symbol != symbol_upper:
            continue
        status = str(entry.get("verdict", entry.get("status", "UNKNOWN"))).upper()
        if status == "PROMOTE":
            return True
        print(f"  WARNING: {symbol} has status '{status}' in gate report (expected PROMOTE)")
        return False

    print(f"  WARNING: {symbol} not found in promotion_gate_report.json")
    return True  # allow but warn


def _compute_lot_size_from_pool(pool: float, risk_pct: float, stop_pips: float, symbol: str) -> int:
    """Compute FX lot size so that a stop-loss costs pool * risk_pct dollars.

    Example: pool=$10,000, risk_pct=0.02, stop_pips=20, EURUSD
    -> risk_amount = $200
    -> lot_size = $200 / (20 pips * $0.0001/pip/unit) = 100,000 units
    """
    is_jpy = "JPY" in symbol.upper()
    pip_val = PIP_VALUE_PER_UNIT["jpy"] if is_jpy else PIP_VALUE_PER_UNIT["default"]
    risk_amount = pool * risk_pct
    raw = risk_amount / (stop_pips * pip_val)
    # Round down to nearest 1000 (IBKR minimum increment for FX)
    # Cap at 100,000 units (1 standard lot) as safety ceiling
    lot = max(1000, int(raw / 1000) * 1000)
    MAX_LOT = 100_000
    if lot > MAX_LOT:
        lot = MAX_LOT
    return lot


def generate_live_config(
    paper_path: Path,
    lot_size: int = DEFAULT_FX_LOT_SIZE,
    contracts: int = DEFAULT_FUTURES_CONTRACTS,
    force: bool = False,
    pool: float = 0,
    risk_pct: float = 0,
) -> Path | None:
    """Read a paper config, produce a live config with reduced sizing.

    Returns the output path on success, None on failure.
    """
    if not paper_path.exists():
        print(f"ERROR: Paper config not found: {paper_path}")
        return None

    with open(paper_path) as f:
        paper = json.load(f)

    # --- Validate: replay_expectations required for promotion ---
    if "replay_expectations" not in paper:
        print(f"ERROR: {paper_path.name} has no replay_expectations — cannot promote to live")
        return None

    symbol = paper.get("symbol", "unknown").lower()
    instrument_type = paper.get("instrument_type", "forex")

    # --- Check promotion gate (HARD GATE — not advisory) ---
    gate_report = _load_gate_report()
    if not force:
        promoted = _check_promotion_gate(paper.get("symbol", ""), gate_report)
        if not promoted:
            print(f"ERROR: {paper.get('symbol', '?')} is NOT promoted. Cannot generate live config.")
            print(f"  Run promotion_gate_v2 first, or use --force to bypass (UNSAFE).")
            return None

    # --- Build live config ---
    live = json.loads(json.dumps(paper))  # deep copy

    # Version bump
    live["version"] = paper.get("version", "paper_v1").replace("paper_", "live_")
    live["live"] = True
    deployment = live.get("deployment", {}) if isinstance(live.get("deployment", {}), dict) else {}
    deployment["managed"] = True
    deployment["stage"] = STAGE_REAL
    deployment["log_dir"] = default_log_dir(paper.get("symbol", symbol), STAGE_REAL)
    deployment["paper_source"] = paper_path.name
    deployment["risk_policy"] = {
        "model_start_equity_usd": RISK_POLICY_DEFAULTS["model_start_equity_usd"],
        "base_risk_pct": float(risk_pct or RISK_POLICY_DEFAULTS["base_risk_pct"]),
        "active_risk_pct": float(risk_pct or RISK_POLICY_DEFAULTS["base_risk_pct"]),
        "earned_cap_pct": RISK_POLICY_DEFAULTS["earned_cap_pct"],
        "manual_step_up_required": RISK_POLICY_DEFAULTS["manual_step_up_required"],
        "scale_state": "BASE",
    }
    live["deployment"] = deployment

    # Sizing: keep dynamic risk sizing aligned with the stage risk policy.
    # Explicit lot/contract sizing remains as a fallback floor.
    if "risk" in live:
        live["risk"]["risk_pct"] = float(risk_pct or RISK_POLICY_DEFAULTS["base_risk_pct"])
        if instrument_type == "forex":
            if pool > 0 and risk_pct > 0:
                stop_pips = live["risk"].get("stop_pips", 20)
                computed_lot = _compute_lot_size_from_pool(pool, risk_pct, stop_pips, symbol)
                live["risk"]["lot_size"] = computed_lot
                live["risk"]["_sizing_method"] = "pool_based"
                live["risk"]["_pool"] = pool
                live["risk"]["_risk_pct"] = risk_pct
                print(f"  Pool-based sizing: ${pool} x {risk_pct*100:.1f}% / {stop_pips}pip = {computed_lot:,} units")
            else:
                live["risk"]["lot_size"] = lot_size
        elif instrument_type == "future":
            live["risk"]["num_contracts"] = contracts

    # Traceability fields
    live["paper_source"] = paper_path.name
    live["promoted_at"] = datetime.now(timezone.utc).isoformat()
    live["paper_config_hash"] = _sha256_file(paper_path)
    live["gate_report_hash"] = _sha256_file(GATE_REPORT) if GATE_REPORT.exists() else "none"

    # --- Determine output path ---
    out_name = f"{symbol}_live_v1.json"
    out_path = CONFIGS_DIR / out_name

    if out_path.exists() and not force:
        print(f"ERROR: {out_path} already exists. Use --force to overwrite.")
        return None

    # --- Write ---
    with open(out_path, "w") as f:
        json.dump(live, f, indent=4)
        f.write("\n")

    # --- Update hashes.json ---
    update_hashes(out_path)

    # --- Print summary ---
    print(f"\nGenerated live config: {out_path.relative_to(REPO)}")
    print(f"  paper source:    {paper_path.name}")
    print(f"  symbol:          {paper.get('symbol', '?')}")
    print(f"  instrument:      {instrument_type}")
    print(f"  strategy:        {paper.get('strategy', '?')}")
    _print_diff(paper, live, instrument_type)

    return out_path


def _print_diff(paper: dict, live: dict, instrument_type: str):
    """Print a human-readable diff between paper and live configs."""
    print("\n  Changes:")
    print(f"    version:             {paper.get('version')} -> {live.get('version')}")
    print(f"    live:                (absent) -> True")

    if instrument_type == "forex":
        paper_lot = paper.get("risk", {}).get("lot_size", "?")
        live_lot = live.get("risk", {}).get("lot_size", "?")
        print(f"    risk.lot_size:       {paper_lot} -> {live_lot}")
    elif instrument_type == "future":
        paper_ct = paper.get("risk", {}).get("num_contracts", "?")
        live_ct = live.get("risk", {}).get("num_contracts", "?")
        print(f"    risk.num_contracts:  {paper_ct} -> {live_ct}")

    print(f"    paper_source:        (absent) -> {live.get('paper_source')}")
    print(f"    promoted_at:         (absent) -> {live.get('promoted_at')}")
    print(f"    paper_config_hash:   (absent) -> {live.get('paper_config_hash', '')[:16]}...")
    print(f"    gate_report_hash:    (absent) -> {live.get('gate_report_hash', '')[:16]}...")


def update_hashes(config_path: Path):
    """Compute SHA-256 of config file and add/update entry in hashes.json."""
    hashes = {}
    if HASHES_FILE.exists():
        with open(HASHES_FILE) as f:
            hashes = json.load(f)

    short = _short_hash(config_path)
    hashes[config_path.name] = short

    # Write sorted
    with open(HASHES_FILE, "w") as f:
        json.dump(dict(sorted(hashes.items())), f, indent=2)
        f.write("\n")

    print(f"  hashes.json updated: {config_path.name} -> {short}")


def _get_promoted_pairs(gate_report: dict) -> list[Path]:
    """Return list of paper config paths for all PROMOTE pairs in gate report."""
    paths = []
    runners = gate_report.get("runners", []) if isinstance(gate_report, dict) else []
    for entry in runners:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("verdict", entry.get("status", "UNKNOWN"))).upper()
        if status != "PROMOTE":
            continue
        # Find matching paper config
        symbol = str(entry.get("symbol", "")).lower()
        # Search for any paper config containing this symbol
        matches = list(CONFIGS_DIR.glob(f"{symbol}_*_paper_v1.json"))
        if not matches:
            print(f"  WARNING: No paper config found for promoted symbol {symbol}")
        paths.extend(matches)
    return paths


def main():
    parser = argparse.ArgumentParser(
        description="Generate live config from paper config for micro-live trading"
    )
    parser.add_argument(
        "config",
        nargs="?",
        help="Path to paper config JSON",
    )
    parser.add_argument(
        "--lot-size",
        type=int,
        default=DEFAULT_FX_LOT_SIZE,
        help=f"FX lot size (default: {DEFAULT_FX_LOT_SIZE} = 1 micro lot)",
    )
    parser.add_argument(
        "--contracts",
        type=int,
        default=DEFAULT_FUTURES_CONTRACTS,
        help=f"Futures num_contracts (default: {DEFAULT_FUTURES_CONTRACTS})",
    )
    parser.add_argument(
        "--pool",
        type=float,
        default=0,
        help="Total trading pool in USD (e.g. 10000). Used with --risk-pct for dynamic sizing.",
    )
    parser.add_argument(
        "--risk-pct",
        type=float,
        default=RISK_POLICY_DEFAULTS["base_risk_pct"],
        help=f"Risk per trade as fraction of equity (default: {RISK_POLICY_DEFAULTS['base_risk_pct']:.3f} = 0.5%%)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing live config",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Generate live configs for all promoted pairs in gate report",
    )
    args = parser.parse_args()

    if args.all:
        gate_report = _load_gate_report()
        if gate_report is None:
            print("ERROR: --all requires promotion_gate_report.json to exist")
            sys.exit(1)
        paper_paths = _get_promoted_pairs(gate_report)
        if not paper_paths:
            print("No promoted pairs found in gate report.")
            sys.exit(0)
        print(f"Generating live configs for {len(paper_paths)} promoted pair(s)...\n")
        results = []
        for pp in sorted(paper_paths):
            result = generate_live_config(
                pp,
                lot_size=args.lot_size,
                contracts=args.contracts,
                force=args.force,
                pool=args.pool,
                risk_pct=args.risk_pct,
            )
            results.append(result)
            print()
        ok = sum(1 for r in results if r is not None)
        print(f"Done: {ok}/{len(paper_paths)} live configs generated.")
        if ok < len(paper_paths):
            sys.exit(1)

    elif args.config:
        config_path = Path(args.config)
        # Allow relative paths from repo root
        if not config_path.is_absolute():
            config_path = REPO / config_path
        result = generate_live_config(
            config_path,
            lot_size=args.lot_size,
            contracts=args.contracts,
            force=args.force,
            pool=args.pool,
            risk_pct=args.risk_pct,
        )
        if result is None:
            sys.exit(1)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
