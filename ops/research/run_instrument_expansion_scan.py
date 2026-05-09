"""Run the Argus research-only instrument expansion scan.

The scanner writes ranked candidates and a backtest queue, but never edits
live configs or promotes symbols into execution.
"""

from ops.audit.argus_audit_engine import main


if __name__ == "__main__":
    raise SystemExit(main(["--phase", "atlas"]))
