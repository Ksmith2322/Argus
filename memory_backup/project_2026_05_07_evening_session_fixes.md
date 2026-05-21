---
name: 2026-05-07 evening session — fixes shipped after the 3-agent audit
description: 5 fixes shipped same evening: gdx_gld silent-death root cause permanently fixed (Windows os.kill SystemError), real-money boundary module + 21 tests, PnL reconciliation tests for vix_intraday, mamba heartbeat lie fixed, gdx_gld restarted in live mode. Plus 2 operator-decision items left open.
type: project
originSessionId: ca6e24c7-7756-4b66-a0df-d339b1453b20
---
# 5/7 evening session — fixes shipped after the audit

After the 3-agent verification audit (`project_2026_05_07_evening_audit.md`) identified the gaps, this session executed the action plan. **Five fixes shipped, two operator-decision items flagged.**

## What landed (in order)

### 1. Mamba heartbeat truth-in-reporting fix
- `forge/mamba/runner.py:982` hardcoded `"mode": "research_only"` regardless of CLI flag, so the heartbeat lied even when mamba was correctly running with `--live`.
- Fix: `write_heartbeat()` now infers mode from `sys.argv` — `live` if `--live` present, `loop_signal_only` if `--loop`, else `research_only`.
- Cosmetic only — mamba was already functionally in live mode (verified by reading `run_live()` and confirming it connects IBKR + uses `safe_position_size`).
- Running mamba process won't pick up the change until next restart. Heartbeat will continue to show `research_only` until then. Restart-when-convenient.

### 2. **gdx_gld silent-death root cause — permanently fixed**
This was Priority-2 unresolved on the morning audit. Root cause now diagnosed and fixed:
- `forge/gdx_gld_runner.py:445` calls `os.kill(old_pid, 0)` to test if a stale-lock PID is alive
- On Windows, when the PID is dead, CPython raises **both** `OSError [WinError 87]` and a chained `SystemError` ("returned a result with an exception set")
- The `except OSError:` clause caught only the OSError, leaving SystemError to propagate
- Result: every restart attempt failed at lock acquisition → silent death every cycle
- **Fix at line 449**: changed to `except (OSError, SystemError):` so stale locks are properly cleaned up
- Verified by relaunching with the `ops/launch_with_stderr.ps1` wrapper; gdx_gld came up clean and was still alive 14+ minutes later in `mode: live, position: FLAT`
- **The stderr-capture wrapper paid off on first launch** — caught the exception trace immediately. Without it, this would still be undiagnosed.

### 3. gdx_gld restart from `signal_only` → `live`
- `start_all_runners.ps1:69` had been updated to `--live --loop` but the running gdx_gld processes pre-dated that update and were running with `--signal-only --loop --interval-min 60`
- Killed both processes (venv shim + Windows redirector — see `reference_windows_venv_redirector.md`)
- Re-launched via the idempotent fleet launcher
- Now live: PID 21184 venv + PID 41776 redirector, heartbeat `mode: live`

### 4. Real-money boundary module — first defensive layer
The biggest 5/31 gap per the audit. Shipped MVP with the fail-closed primitives:

| File | Purpose |
|---|---|
| `helio/real_money.py` | `REAL_MONEY_ENABLED=False` default, `Allowlist` dataclass + loader, `is_real_money_connection()`, `enforce_real_money_boundary()`, `AccountBoundaryViolationError`, `real_money_order_tag()`, $5,000 hard per-order cap |
| `argus_flow/configs/real_money_allowlist.json` | Empty scaffold: `global_enabled=false`, `strategies=[]`. Adding a strategy requires ledger entry + signature + max-strategies cap respected. |
| `argus_flow/tests/test_real_money_boundary.py` | 21 tests covering: missing allowlist file → safe default, corrupt JSON → safe default, validate_self() rules, paper-passthrough is no-op, real with allowlist disabled rejects, strategy not in allowlist rejects, oversized notional rejects, well-formed real order passes, order tag formatting |
| `helio/ibkr_execution.py:323` | Wired `enforce_real_money_boundary()` as the FIRST check in `submit_bracket()` after argument validation. Paper passthrough (no-op) preserved; real connections trigger full validation. Boundary rejection surfaces via `reject_reason="real_money_boundary:..."`. |

**Out of scope for this session (still pending for 5/31)**:
- Wiring into `helio/ibkr_executor.py` (Greek family) and `helio/signal_executor.py` (scanner-style) — the same 3-line block from `submit_bracket` should be added to those executors
- Mismatch-detection daemon (Rule 7 — separate process polling broker every 5 min)
- Pre-submission Discord reaction approval (Rule 6 — first 30-day per-strategy gate)
- Daily real-money report + dedicated Discord channel (Rule 8)
- `REAL_ACCOUNT_ID` constant — still `None`; populate when real account is funded

### 5. PnL reconciliation tests — vix_intraday
The audit flagged "no `test_pnl_reconciliation*` exists" as a soft blocker. Shipped:

| Test | What it catches |
|---|---|
| `test_vix_intraday_pnl_matches_price_size_math` | Schema corruption, direction flips, sizing-formula bugs. Verifies `pnl_usd ≈ (exit-entry)*size*dir_sign` for all 61 rows. Tolerance $0.05. |
| `test_vix_intraday_no_phantom_sized_trades` | Phantom-trade canary. Refuses any vix_intraday trade with `position_size > 50,000` shares (would be a $1.8M order). |
| `test_vix_intraday_trades_have_canonical_fill_records` | Cross-checks trades.csv vs canonical_fills.jsonl since 2026-05-01. Allows up to 10% miss rate (early dual-write gap absorption). |

All 3 pass on current production data. These are the regression guards for the 5/7 audit's phantom-trade root fix (`safe_position_size()` + 50% NetLiq circuit breaker).

## Verification

- **21/21** new boundary tests pass
- **3/3** new PnL reconciliation tests pass
- **1101** other tests pass; **11 pre-existing failures** unrelated to this session (orphan_recovery MockInstrument lacks `.cfg`, fleet_sizing caps tightened but tests not updated, apollo strategy-killed test now stale, etc.)
- gdx_gld alive and stable 14+ min post-relaunch with the lock-bug fix
- All other runners (18) untouched, still alive

## Operator-decision items left open

1. **HALT.flag is currently SET in production**. Daily-loss circuit breaker tripped at -2.17% (PAUSE tier) on 5/7. All strategies blocked from new entries until auto-recovery (per hysteresis design — needs equity to recover). I did NOT manually clear it because the breaker fired on real loss data. Operator decision whether to clear early.
2. **ArgusWatchdog scheduled task** is `Ready` state but `LastRun=03/31/2026, LastResult=1, NextRun=(empty)` — task has been failing for 38 days. **However**, the live `watchdog.ps1` process IS running (PID 12260) and `helio.fleet_monitor` is also up. Supervision is alive even though the scheduled task is dormant. Cleanup item: either fix the task (give it a trigger) or delete it via `ops/scheduled_task_cleanup.py`.

## What's next (5/8 onwards)

For the 5/15 mid-cycle ceremony to be data-clean:
- Watch gdx_gld stay alive past the historical 22:00 UTC death window (first time the lock fix gets stress-tested in production)
- Argus pairs zero-fills diagnosis still pending (Priority-1 item from morning audit; not addressed tonight — needs MTF gate trace, separate session)
- Wire boundary module into `helio/ibkr_executor.py` + `helio/signal_executor.py` (same 3-line block as `submit_bracket`)
- Build mismatch-detection daemon (Rule 7) before any real-money go-live

## Cross-references
- Morning audit: `project_2026_05_07_week_audit.md`
- Evening audit: `project_2026_05_07_evening_audit.md`
- Real-money policy: `project_real_money_boundary.md`
- Lock-bug fix: `forge/gdx_gld_runner.py:438-460`, `helio/real_money.py:1-260`
