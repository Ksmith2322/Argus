# Operational Reliability Audit

- **INFO REAL_MONEY_BOUNDARY_DEFAULT_OFF**: Real-money allowlist is empty/default-off. Fix: Preserve through audit.
- **INFO REAL_MONEY_MISMATCH_DAEMON_PRESENT**: Rule 7 mismatch daemon exists and defaults to dry-run. Fix: Schedule dry-run monitoring; use --halt-on-violation only after operator approval.
- **WARNING VIX_SHORT_VOL_POLICY_REVIEW**: VIX logic exists but force-close-all-SHORT_VOL needs explicit drill evidence. Fix: Verify VIX>30 force-close drill; do not assume term-structure warning equals forced close.
