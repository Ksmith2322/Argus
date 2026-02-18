# runner_live.py
import asyncio
import time

# line above: import os
import os
import sys
import inspect
import traceback
from typing import Any, Optional


# --- BOOTSTRAP FOR FILE EXECUTION --------------------------------------------
# Allows BOTH:
#   1) python -m nova_scripts.Argus.runner_live
#   2) python nova_scripts/Argus/runner_live.py
#
# Ensures repo root is on sys.path and package is set so `from .x import y` works.
if __package__ in (None, ""):
    # line above: here = os.path.dirname(os.path.abspath(__file__))
    here = os.path.dirname(os.path.abspath(__file__))                 # .../nova_scripts/Argus
    project_root = os.path.abspath(os.path.join(here, "..", ".."))    # .../Nova
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    __package__ = "nova_scripts.Argus"


# --- PACKAGE IMPORTS ----------------------------------------------------------
# line above: from .config import load_config
from .config import load_config
from .feed_coinbase import make_http, fetch_spot_price, preload_indicator_history
from .io_logs import (
    ensure_logs,
    ensure_signals_header_matches_file,
    is_kill_switch_on,
    is_paused,
    log_event,
    log_signal_snapshot,
)
from .notify import maybe_notify_discord
from .state import BotState
from .engine import step
from .utils import safe_str


def _load_cfg(cfg_path: Optional[str] = None) -> dict[str, Any]:
    """
    Robust config loader:
      - If load_config supports cfg_path/path/filename (kw or positional), pass it.
      - Otherwise, call load_config() with no args.

    This prevents: "load_config() takes 0 positional arguments but 1 was given"
    """
    # line above: if not cfg_path:
    if not cfg_path:
        return load_config()

    # Inspect signature to decide what is legal
    try:
        sig = inspect.signature(load_config)
        params = sig.parameters
    except Exception:
        # If we can't introspect, don't risk passing args
        return load_config()

    # Case 1: supports **kwargs
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        try:
            return load_config(cfg_path=cfg_path)  # type: ignore[call-arg]
        except Exception:
            return load_config(path=cfg_path)      # type: ignore[call-arg]

    # Case 2: explicit kw parameters
    for key in ("cfg_path", "path", "filename", "config_path", "config"):
        if key in params:
            try:
                return load_config(**{key: cfg_path})  # type: ignore[call-arg]
            except TypeError:
                # key exists but call rejected; fall through
                break

    # Case 3: accepts positional argument(s)
    positional_slots = [
        p for p in params.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if len(positional_slots) >= 1:
        try:
            return load_config(cfg_path)  # type: ignore[misc]
        except TypeError:
            pass

    # Final fallback: zero-arg load_config; cfg_path ignored
    try:
        cfg = load_config()
        # Add breadcrumb so you *see* this in your events log
        try:
            log_event("?", "CFG_PATH_IGNORED", f"load_config signature={sig} does not accept path; ignored cfg_path={cfg_path}")
        except Exception:
            pass
        return cfg
    except Exception:
        raise


async def run_live(*, cfg_path: Optional[str] = None, dry_run: bool = True, once: bool = False) -> int:
    """
    Live runner (paper ledger by default).

    Args:
      cfg_path: optional config file path (if load_config supports it)
      dry_run:  retained for parity with main.py (your engine currently paper-ledger)
      once:     if True, run a single tick then exit cleanly
    """
    cfg = _load_cfg(cfg_path)
    ensure_logs()
    ensure_signals_header_matches_file()

    http = make_http()
    state = BotState.from_config(cfg)
    symbol = state.symbol

    # --------- LINE ABOVE: symbol = state.symbol
    # Preload enough history for each TF strategy
    needed = max(cfg["MA_TREND_200"], cfg["MA_TREND_50"], cfg["MA_SLOW"], cfg["MA_FAST"]) + 10
    try:
        seeded_1m = await preload_indicator_history(
            indicator_engine=state.strat_1m.ind,
            http=http,
            cfg={**cfg, "CANDLE_SECONDS": int(cfg.get("CANDLE_SECONDS", 60))},
            needed_candles=needed,
        )
        seeded_5m = await preload_indicator_history(
            indicator_engine=state.strat_5m.ind,
            http=http,
            cfg={**cfg, "CANDLE_SECONDS": int(cfg.get("CANDLE_SECONDS_5M", cfg.get("TF_5M_SECONDS", 300)))},
            needed_candles=needed,
        )
        seeded_1h = await preload_indicator_history(
            indicator_engine=state.strat_1h.ind,
            http=http,
            cfg={**cfg, "CANDLE_SECONDS": int(cfg.get("CANDLE_SECONDS_1H", cfg.get("TF_1H_SECONDS", 3600)))},
            needed_candles=needed,
        )
        print(f"[PRELOAD] Seeded 1m={seeded_1m} 5m={seeded_5m} 1h={seeded_1h} (needed~{needed})")
        log_event(symbol, "PRELOAD", f"1m={seeded_1m} 5m={seeded_5m} 1h={seeded_1h} needed~{needed}")
    except Exception as e:
        print(f"[PRELOAD] Failed: {e}")
        log_event(symbol, "PRELOAD_FAIL", str(e))

    print(f"[START] PAPER LEDGER (NO LIVE ORDERS) | {symbol}")
    print(f"  feed: {cfg.get('COINBASE_SPOT_URL', '')}")
    print(f"  logs: ./logs | kill={cfg.get('KILL_SWITCH_FILE', 'KILL_SWITCH.txt')} pause={cfg.get('PAUSE_FILE', 'PAUSE.txt')}")
    print(f"  dry_run={int(bool(dry_run))} once={int(bool(once))}")

    fail_count = 0

    try:
        while True:
            if is_kill_switch_on(cfg):
                msg = "Kill switch file detected. Exiting."
                log_event(symbol, "KILL_SWITCH", msg)
                if cfg.get("DISCORD_WEBHOOK_URL"):
                    maybe_notify_discord(http, cfg["DISCORD_WEBHOOK_URL"], "KILL SWITCH", msg)
                print("[EXIT] Kill switch file detected.")
                return 0

            paused = is_paused(cfg)

            # --------- LINE ABOVE: paused = is_paused(cfg)
            # Fetch tick with backoff (keeps the loop resilient)
            try:
                tick = await fetch_spot_price(http, cfg)
                fail_count = 0

                # --------- LINE ABOVE: fail_count = 0
                # Guard against stale/incorrect tick epochs (prevents "day rewind" risk resets)
                try:
                    now_sys = int(time.time())
                    te = int(getattr(tick, "epoch", 0) or 0)
                    max_drift = int(cfg.get("MAX_EPOCH_DRIFT_SECONDS", 3600))  # default: 1h

                    # If epoch is missing OR drifts too far from system time, clamp it.
                    if te <= 0 or abs(te - now_sys) > max_drift:
                        try:
                            setattr(tick, "epoch", now_sys)
                        except Exception:
                            pass
                        try:
                            # Optional: align tick.ts too (not required for correctness)
                            setattr(tick, "ts", time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now_sys)) + "+00:00")
                        except Exception:
                            pass
                        try:
                            log_event(symbol, "EPOCH_CLAMP", f"tick.epoch={te} -> {now_sys} (drift>{max_drift}s)")
                        except Exception:
                            pass
                except Exception:
                    pass

            except Exception as e:
                fail_count += 1
                log_event(symbol, "PRICE_FETCH_FAIL", str(e))
                if cfg.get("DISCORD_WEBHOOK_URL"):
                    maybe_notify_discord(http, cfg["DISCORD_WEBHOOK_URL"], "PRICE FETCH FAIL", str(e))
                backoff = min(
                    float(cfg.get("MAX_FAIL_BACKOFF_SECONDS", 60)),
                    float(cfg.get("POLL_MED_SECONDS", 2)) * (2 ** min(fail_count, 6)),
                )
                await asyncio.sleep(backoff)
                continue

            snap = step(state, tick, cfg, paused=paused)

            # --------- LINE ABOVE: snap = step(state, tick, cfg, paused=paused)
            # Engine events (logging + optional Discord)
            if getattr(snap, "events", None):
                for ev in snap.events:
                    try:
                        log_event(symbol, ev.name, ev.message)
                    except Exception:
                        pass

                    if getattr(ev, "notify_title", "") and cfg.get("DISCORD_WEBHOOK_URL"):
                        try:
                            maybe_notify_discord(
                                http,
                                cfg["DISCORD_WEBHOOK_URL"],
                                ev.notify_title,
                                ev.notify_body or ev.message,
                            )
                        except Exception:
                            pass

            # Console output
            print(
                f"[{snap.ts}] px={snap.px:.2f} "
                f"sig={safe_str(getattr(snap, 'sig_1m', None))} trend_ok={safe_str(getattr(snap, 'trend_ok_1m', None))} "
                f"score={safe_str(getattr(snap, 'score_1m', None))} "
                f"cash={getattr(snap, 'cash_usd', 0):.2f} qty={safe_str(getattr(snap, 'position_qty', None))} "
                f"equity={getattr(snap, 'equity_usd', 0):.2f} "
                f"unrl={getattr(snap, 'unrl_pnl_usd', 0):.2f} realized={getattr(snap, 'realized_pnl_usd', 0):.2f} "
                f"hold={safe_str(getattr(snap, 'hold_s', None))}s "
                f"belowN={safe_str(getattr(snap, 'trend_below_count', None))} "
                f"cooldown={safe_str(getattr(snap, 'cooldown_remaining_s', None))}s "
                f"paused={int(bool(paused))} "
                f"action={safe_str(getattr(snap, 'action', None))} "
                f"{safe_str(getattr(snap, 'action_reason', None))} "
                f"{safe_str(getattr(snap, 'risk_blocked_reason', None))} "
                f"next={safe_str(getattr(snap, 'next_poll_s', None))}s "
                f"conf={safe_str(getattr(snap, 'confluence_score', None))} "
                f"{safe_str(getattr(snap, 'confluence_reasons', None))}"
            )

            # --------- LINE ABOVE: print(...)
            # CSV logging aligned to io_logs schema (prevents row-length drift)
            try:
                log_signal_snapshot(snap, symbol=symbol, price=getattr(snap, "px", None))
            except Exception:
                pass

            if once:
                return 0

            await asyncio.sleep(float(getattr(snap, "next_poll_s", 1.0)))

    except KeyboardInterrupt:
        print("[STOP] KeyboardInterrupt")
        return 0
    except Exception as e:
        try:
            log_event(symbol, "RUNNER_CRASH", str(e))
        except Exception:
            pass
        print(f"[CRASH] {e}")
        print(traceback.format_exc())
        return 1
    finally:
        try:
            close = getattr(http, "close", None)
            if callable(close):
                await close()
        except Exception:
            pass


if __name__ == "__main__":
    # line above: raise SystemExit(asyncio.run(run_live(...)))
    raise SystemExit(asyncio.run(run_live(cfg_path=None, dry_run=True, once=False)))
