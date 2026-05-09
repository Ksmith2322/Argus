"""Run the read-only killed/quarantined strategy review."""

from ops.audit.argus_audit_engine import main


if __name__ == "__main__":
    raise SystemExit(main(["--phase", "killed"]))
