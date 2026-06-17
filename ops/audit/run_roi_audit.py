"""Run the read-only ROI and capital-efficiency audit."""

from ops.audit.argus_audit_engine import main


if __name__ == "__main__":
    raise SystemExit(main(["--phase", "roi"]))
