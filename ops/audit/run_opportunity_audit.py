"""Run the read-only opportunity throughput audit."""

from ops.audit.argus_audit_engine import main


if __name__ == "__main__":
    raise SystemExit(main(["--phase", "opportunity"]))
