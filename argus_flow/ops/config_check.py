"""Config Integrity Check — validate all strategy configs before launch.

Usage:
    python -m argus_flow.ops.config_check
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO / "argus_flow" / "configs"
HASH_FILE = CONFIG_DIR / "hashes.json"

REQUIRED_FIELDS = ["strategy", "version", "symbol", "trigger", "risk", "replay_expectations"]
REQUIRED_RISK = ["timeout_minutes"]
REQUIRED_REPLAY = ["signals_per_day", "win_rate"]


def validate_config(path: Path) -> tuple[bool, list[str], str]:
    issues = []
    try:
        content = path.read_text()
        cfg = json.loads(content)
    except Exception as e:
        return False, [f"Parse error: {e}"], ""

    file_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

    # Required fields
    for field in REQUIRED_FIELDS:
        if field not in cfg:
            issues.append(f"Missing required field: {field}")

    # Trigger checks
    trigger = cfg.get("trigger", {})
    if not trigger:
        issues.append("Empty trigger config")

    # Risk checks
    risk = cfg.get("risk", {})
    for field in REQUIRED_RISK:
        if field not in risk:
            issues.append(f"Missing risk.{field}")
    for key, val in risk.items():
        if isinstance(val, (int, float)) and val < 0:
            issues.append(f"Negative risk value: {key}={val}")

    # Replay expectations
    replay = cfg.get("replay_expectations", {})
    for field in REQUIRED_REPLAY:
        if field not in replay:
            issues.append(f"Missing replay_expectations.{field}")
    if replay.get("win_rate", 0) > 1.0:
        issues.append(f"Win rate > 1.0: {replay.get('win_rate')} (should be decimal)")

    ok = len(issues) == 0
    return ok, issues, file_hash


def main():
    print("=" * 60)
    print(f"  Config Integrity Check — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print("=" * 60)

    # Load known hashes
    known_hashes = {}
    if HASH_FILE.exists():
        try:
            known_hashes = json.loads(HASH_FILE.read_text())
        except Exception:
            pass

    configs = sorted(CONFIG_DIR.glob("*_paper_v*.json"))
    if not configs:
        print("\n  \033[31mNo config files found!\033[0m")
        return

    all_ok = True
    new_hashes = {}

    for cfg_path in configs:
        ok, issues, file_hash = validate_config(cfg_path)
        new_hashes[cfg_path.name] = file_hash

        # Check hash change
        old_hash = known_hashes.get(cfg_path.name, "")
        hash_changed = old_hash and old_hash != file_hash

        status = "\033[32mPASS\033[0m" if ok and not hash_changed else (
            "\033[33mCHANGED\033[0m" if hash_changed and ok else "\033[31mFAIL\033[0m")

        cfg = json.loads(cfg_path.read_text())
        print(f"\n  {cfg_path.name}")
        print(f"    Strategy: {cfg.get('strategy', '?')} | Symbol: {cfg.get('symbol', '?')} | Version: {cfg.get('version', '?')}")
        print(f"    Hash: {file_hash} | Status: {status}")

        if hash_changed:
            print(f"    \033[33m! Config changed since last check (was {old_hash})\033[0m")

        if issues:
            all_ok = False
            for issue in issues:
                print(f"    \033[31m! {issue}\033[0m")

    # Save new hashes
    HASH_FILE.write_text(json.dumps(new_hashes, indent=2))

    print(f"\n{'=' * 60}")
    if all_ok:
        print(f"  \033[32mALL CONFIGS VALID\033[0m — hashes saved to {HASH_FILE.name}")
    else:
        print(f"  \033[31mCONFIG ISSUES FOUND — fix before launching\033[0m")
    print("=" * 60)


if __name__ == "__main__":
    main()