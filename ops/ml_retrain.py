#!/usr/bin/env python3
"""ops/ml_retrain.py -- Full ML Governor retrain pipeline.

Extracts features from all backtest runs, trains a new governor model,
and optionally sends a Discord summary.

Usage:
    python ops/ml_retrain.py                  # full pipeline
    python ops/ml_retrain.py --no-discord     # skip Discord notification
    python ops/ml_retrain.py --min-trades 5   # skip runs with < 5 trades
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = str(Path(REPO).parent / ".venv" / "Scripts" / "python.exe")


def main():
    args = sys.argv[1:]
    no_discord = "--no-discord" in args
    min_trades = "0"
    if "--min-trades" in args:
        idx = args.index("--min-trades")
        min_trades = args[idx + 1]

    print("=" * 60)
    print("ML GOVERNOR RETRAIN PIPELINE")
    print("=" * 60)

    # Step 1: Extract features
    print("\n[1/2] Extracting features from backtest runs...")
    extract_cmd = [
        PYTHON, str(REPO / "ops" / "ml_extract_features.py"),
        "--min-trades", min_trades,
        "--output", str(REPO / "data" / "ml_trades.csv"),
    ]
    r1 = subprocess.run(extract_cmd, capture_output=True, text=True)
    print(r1.stdout)
    if r1.returncode != 0:
        print(f"FAILED: {r1.stderr}")
        sys.exit(1)

    # Step 2: Train model
    print("\n[2/2] Training governor model...")
    train_cmd = [PYTHON, str(REPO / "ops" / "ml_train_governor.py")]
    r2 = subprocess.run(train_cmd, capture_output=True, text=True)
    print(r2.stdout)
    if r2.returncode != 0:
        print(f"FAILED: {r2.stderr}")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("RETRAIN COMPLETE")
    print("=" * 60)

    # Discord notification
    if not no_discord:
        try:
            # Extract key metrics from train output
            lines = r2.stdout.strip().split("\n")
            summary_lines = [l for l in lines if any(k in l for k in [
                "Trades:", "Accuracy:", "ROC-AUC:", "Block bottom", "Model saved"
            ])]
            summary = "\n".join(summary_lines[:8])

            from ops.notify import send_discord
            send_discord(f"**ML Governor Retrain Complete**\n```\n{summary}\n```")
            print("[Discord notification sent]")
        except Exception as e:
            print(f"[Discord failed: {e}]")


if __name__ == "__main__":
    main()