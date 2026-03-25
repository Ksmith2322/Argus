"""Class B Pair Onboarding Pipeline.
Validates config, runs backtest, evaluates results, prepares for cohort.

Usage:
    python -m argus_flow.ops.onboard_pair --config CONFIG [--data DATA_CSV] [--auto]

Examples:
    python -m argus_flow.ops.onboard_pair --config argus_flow/configs/usdjpy_ny_paper_v1.json --data argus_flow/data/usdjpy_1m.csv
    python -m argus_flow.ops.onboard_pair --config argus_flow/configs/audusd_ny_paper_v1.json --data argus_flow/data/audusd_1m.csv --auto
"""
import argparse
import json
import hashlib
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

REQUIRED_CONFIG_FIELDS = [
    "strategy",
    "version",
    "symbol",
    "instrument_type",
    "trigger",
    "risk",
    "direction",
    "replay_expectations",
]

PAYOFF_THRESHOLDS = {
    "min_wr": 0.25,
    "max_timeout_rate": 0.60,
    "max_stop_target_ratio": 2.0,
    "min_pf": 1.0,
}

HASHES_FILE = REPO / "argus_flow" / "configs" / "hashes.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_config(path: Path) -> dict:
    """Load and return a config JSON file."""
    with open(path, "r") as f:
        return json.load(f)


def _config_hash(cfg: dict) -> str:
    """Deterministic SHA-256 of the config dict."""
    blob = json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def _validate_fields(cfg: dict) -> list[str]:
    """Return list of missing required fields."""
    missing = []
    for field in REQUIRED_CONFIG_FIELDS:
        if field not in cfg:
            missing.append(field)
    return missing


def _ensure_hash(cfg: dict, cfg_path: Path) -> tuple[str, bool]:
    """Check config hash is in hashes.json. Add if missing. Returns (hash, was_added)."""
    h = _config_hash(cfg)
    hashes = {}
    if HASHES_FILE.exists():
        with open(HASHES_FILE, "r") as f:
            hashes = json.load(f)

    if h in hashes:
        return h, False

    hashes[h] = {
        "config": str(cfg_path.name),
        "symbol": cfg.get("symbol", "unknown"),
        "strategy": cfg.get("strategy", "unknown"),
    }
    HASHES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HASHES_FILE, "w") as f:
        json.dump(hashes, f, indent=2)
    return h, True


# ---------------------------------------------------------------------------
# Backtest runner + evaluator
# ---------------------------------------------------------------------------

def _run_backtest(config_path: Path, data_path: Path) -> dict | None:
    """Run fx_backtest and return parsed results dict, or None on failure."""
    cmd = [
        sys.executable, "-m", "argus_flow.ops.fx_backtest",
        "--config", str(config_path),
        "--data", str(data_path),
    ]
    print(f"  Running backtest: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO))
    if result.returncode != 0:
        print(f"  Backtest FAILED (rc={result.returncode})")
        if result.stderr:
            print(f"  stderr: {result.stderr[:500]}")
        return None

    # Try to find a results JSON in the output
    stdout = result.stdout
    print(f"  Backtest completed.")

    # Parse results from stdout — look for JSON block
    try:
        # Find last JSON object in output
        last_brace = stdout.rfind("}")
        if last_brace == -1:
            print("  WARNING: No JSON results found in backtest output.")
            return None
        first_brace = stdout.rfind("{", 0, last_brace)
        if first_brace == -1:
            return None
        results = json.loads(stdout[first_brace : last_brace + 1])
        return results
    except json.JSONDecodeError:
        print("  WARNING: Could not parse backtest results JSON.")
        return None


def _evaluate_results(results: dict) -> tuple[bool, list[str], list[str]]:
    """Evaluate backtest results against Payoff Contract thresholds.

    Returns (passed, kill_reasons, advisories).
    Kill reasons cause FAIL. Advisories are warnings only.
    """
    kills = []
    advisories = []

    # Win rate
    wr = results.get("win_rate", results.get("wr", 0))
    if isinstance(wr, str):
        wr = float(wr.strip("%")) / 100
    if wr < PAYOFF_THRESHOLDS["min_wr"]:
        kills.append(f"WR {wr:.1%} < {PAYOFF_THRESHOLDS['min_wr']:.0%} minimum")

    # Timeout rate
    timeout_rate = results.get("timeout_rate", 0)
    if isinstance(timeout_rate, str):
        timeout_rate = float(timeout_rate.strip("%")) / 100
    if timeout_rate > PAYOFF_THRESHOLDS["max_timeout_rate"]:
        kills.append(f"Timeout rate {timeout_rate:.1%} > {PAYOFF_THRESHOLDS['max_timeout_rate']:.0%} max")

    # Stop rate vs target rate
    stop_rate = results.get("stop_rate", 0)
    target_rate = results.get("target_rate", 0)
    if isinstance(stop_rate, str):
        stop_rate = float(stop_rate.strip("%")) / 100
    if isinstance(target_rate, str):
        target_rate = float(target_rate.strip("%")) / 100
    if target_rate > 0 and stop_rate > PAYOFF_THRESHOLDS["max_stop_target_ratio"] * target_rate:
        kills.append(
            f"Stop rate {stop_rate:.1%} > {PAYOFF_THRESHOLDS['max_stop_target_ratio']:.0f}x target rate {target_rate:.1%}"
        )

    # Median MFE (advisory)
    median_mfe = results.get("median_mfe", None)
    cost = results.get("cost", results.get("spread_cost", None))
    if median_mfe is not None and cost is not None and cost > 0:
        if median_mfe < 1.5 * cost:
            advisories.append(f"Median MFE {median_mfe} < 1.5x cost {cost} (edge may be too thin)")

    # Profit factor (advisory)
    pf = results.get("profit_factor", results.get("pf", 0))
    if pf < PAYOFF_THRESHOLDS["min_pf"]:
        advisories.append(f"PF {pf:.2f} < {PAYOFF_THRESHOLDS['min_pf']:.1f}")

    passed = len(kills) == 0
    return passed, kills, advisories


# ---------------------------------------------------------------------------
# Onboarding report
# ---------------------------------------------------------------------------

def _create_log_dir(symbol: str) -> Path:
    """Create per-symbol log directory."""
    log_dir = REPO / "argus_flow" / "logs" / symbol.lower()
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _print_auto_instructions(symbol: str):
    """Print manual instructions for adding pair to runners."""
    print()
    print("  Manual steps to complete onboarding:")
    print(f"    1. Add '{symbol}' to RUNNERS list in argus_flow/ops/daily_report.py")
    print(f"    2. Add '{symbol}' to RUNNERS list in argus_flow/ops/divergence_guard.py")
    print(f"    3. Create/update launcher script for {symbol}")


def _auto_add_to_runners(symbol: str):
    """Attempt to add symbol to daily_report.py and divergence_guard.py RUNNERS lists."""
    files = [
        REPO / "argus_flow" / "ops" / "daily_report.py",
        REPO / "argus_flow" / "ops" / "divergence_guard.py",
    ]
    for fpath in files:
        if not fpath.exists():
            print(f"  WARNING: {fpath.name} not found, skipping auto-add.")
            continue
        content = fpath.read_text()
        # Check if symbol already present
        if symbol.upper() in content:
            print(f"  {fpath.name}: {symbol.upper()} already present.")
            continue
        # Try to find RUNNERS list and add
        if "RUNNERS" in content:
            print(f"  {fpath.name}: Found RUNNERS list. Add '{symbol.upper()}' manually — "
                  f"auto-edit of list syntax not implemented yet.")
        else:
            print(f"  {fpath.name}: No RUNNERS list found. Add manually.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Onboard a Class B trading pair")
    parser.add_argument("--config", required=True, help="Path to config JSON")
    parser.add_argument("--data", default=None, help="Path to candle data CSV for backtest")
    parser.add_argument("--auto", action="store_true", help="Auto-add to runner lists")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = REPO / config_path

    print(f"{'='*60}")
    print(f"  ARGUS FLOW — Pair Onboarding Pipeline")
    print(f"{'='*60}")
    print()

    # Step 1: Load and validate config
    print("[1/5] Loading config...")
    if not config_path.exists():
        print(f"  FAIL: Config not found: {config_path}")
        sys.exit(1)

    cfg = _load_config(config_path)
    symbol = cfg.get("symbol", "UNKNOWN")
    print(f"  Symbol: {symbol}")
    print(f"  Strategy: {cfg.get('strategy', '?')} v{cfg.get('version', '?')}")

    missing = _validate_fields(cfg)
    if missing:
        print(f"  FAIL: Missing required fields: {missing}")
        sys.exit(1)
    print(f"  All {len(REQUIRED_CONFIG_FIELDS)} required fields present.")

    # Step 2: Validate / register config hash
    print()
    print("[2/5] Checking config hash...")
    h, was_added = _ensure_hash(cfg, config_path)
    if was_added:
        print(f"  Hash {h} added to hashes.json (new config)")
    else:
        print(f"  Hash {h} already registered.")

    # Step 3: Run backtest (if data provided)
    bt_passed = True
    kills = []
    advisories = []

    if args.data:
        data_path = Path(args.data)
        if not data_path.is_absolute():
            data_path = REPO / data_path

        print()
        print("[3/5] Running backtest...")
        if not data_path.exists():
            print(f"  FAIL: Data file not found: {data_path}")
            sys.exit(1)

        results = _run_backtest(config_path, data_path)
        if results is None:
            print("  FAIL: Backtest produced no results.")
            bt_passed = False
            kills.append("Backtest returned no results")
        else:
            bt_passed, kills, advisories = _evaluate_results(results)
            print()
            print("  Payoff Contract evaluation:")
            if kills:
                for k in kills:
                    print(f"    KILL: {k}")
            if advisories:
                for a in advisories:
                    print(f"    ADVISORY: {a}")
            if bt_passed and not advisories:
                print("    All thresholds passed.")
    else:
        print()
        print("[3/5] Skipping backtest (no --data provided)")

    # Step 4: Create log directory
    print()
    print("[4/5] Preparing log directory...")
    if bt_passed:
        log_dir = _create_log_dir(symbol)
        print(f"  Created: {log_dir}")
    else:
        print("  Skipped (backtest failed).")

    # Step 5: Onboarding report
    print()
    print("[5/5] Onboarding Report")
    print(f"{'='*60}")
    if bt_passed:
        print(f"  STATUS: READY")
        print(f"  Symbol: {symbol}")
        print(f"  Config: {config_path.name} (hash: {h})")
        if advisories:
            print(f"  Advisories: {len(advisories)}")
            for a in advisories:
                print(f"    - {a}")

        if args.auto:
            print()
            print("  Auto-adding to runner lists...")
            _auto_add_to_runners(symbol)
        else:
            _print_auto_instructions(symbol)
    else:
        print(f"  STATUS: FAIL")
        print(f"  Symbol: {symbol}")
        print(f"  Kill reasons:")
        for k in kills:
            print(f"    - {k}")
        print()
        print("  Pair did NOT pass Payoff Contract. Do not proceed to cohort.")

    print(f"{'='*60}")
    sys.exit(0 if bt_passed else 1)


if __name__ == "__main__":
    main()
