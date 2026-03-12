#!/usr/bin/env python3
# backtest/runner.py
#!/usr/bin/env python3
# backtest/runner.py
from __future__ import annotations

import os
import sys

# line above: import sys
# ----------------------------
# Phase 7: BT env defaults MUST be set before importing io_logs (or anything that might log)
# ----------------------------
def _set_env_if_missing_or_blank(key: str, value: str) -> None:
    # line above: def _set_env_if_missing_or_blank(key: str, value: str) -> None:
    cur = os.environ.get(key, "")
    if cur is None or str(cur).strip() == "":
        os.environ[key] = value

_set_env_if_missing_or_blank("ARGUS_MODE", "bt")
_set_env_if_missing_or_blank("ARGUS_ARTIFACT_ROOT", r"C:\Argus\repo")
_set_env_if_missing_or_blank("ARGUS_BT_ARTIFACT_DIR", os.path.join(os.environ["ARGUS_ARTIFACT_ROOT"], "ops", "logs"))

# Sandbox LIVE_* paths for backtest (must happen before io_logs import)
def _ensure_bt_sandbox_live_paths_early() -> None:
    # line above: def _ensure_bt_sandbox_live_paths_early() -> None:
    mode = (os.environ.get("ARGUS_MODE") or "").strip().lower()
    if mode not in ("bt", "backtest"):
        return
    out_dir = os.environ["ARGUS_BT_ARTIFACT_DIR"]
    os.makedirs(out_dir, exist_ok=True)
    _set_env_if_missing_or_blank("LIVE_EVENTS_CSV", os.path.join(out_dir, "bt_sandbox_live_events.csv"))
    _set_env_if_missing_or_blank("LIVE_SIGNALS_CSV", os.path.join(out_dir, "bt_sandbox_live_signals.csv"))
    _set_env_if_missing_or_blank("ARGUS_DISABLE_LIVE_ARTIFACTS", "1")

_ensure_bt_sandbox_live_paths_early()

# add repo root so `import config` works when running as a script
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import json
import random
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Tuple, Any, Dict, Iterable

# ✅ FIX (repo layout): engine-layer modules live at repo root (C:\Argus\repo\*.py)
# LINE ABOVE: from typing import Optional, List, Tuple, Any, Dict, Iterable
from config import load_config
from state import BotState
from engine import step
from io_logs import (
    ensure_logs,
    logs_dir,
    log_signal_snapshot,
    log_bt_event,
    signals_csv_path,
    events_csv_path,
    log_bt_signal_snapshot,   # ✅ run-scoped signals
    log_bt_equity,            # ✅ run-scoped equity
    bt_events_csv_path,       # ✅ run-scoped events path helper
    bt_signals_csv_path,      # ✅ run-scoped signals path helper
    bt_equity_csv_path,       # ✅ run-scoped equity path helper
    ensure_signals_header_matches_path,  # ✅ header upgrade for run-scoped signals
)
from feed_coinbase import make_http

# --------- LINE ABOVE: from feed_coinbase import make_http
# IMPORTANT:
# - Prefer RELATIVE imports inside the backtest package so `python -m backtest.runner` works.
# - Provide fallback ABSOLUTE imports so `python backtest/runner.py` can still work.
try:
    from .loader import load_candles_csv
    from .feed import ticks_from_close_series, PriceTick
    from .results import (
        BacktestResults,
        Trade,
        parse_buy_event_message,
        parse_sell_event_message,
    )
except Exception:
    from backtest.loader import load_candles_csv
    from backtest.feed import ticks_from_close_series, PriceTick
    from backtest.results import (
        BacktestResults,
        Trade,
        parse_buy_event_message,
        parse_sell_event_message,
    )


# ----------------------------
# Phase 7: Run Identity + Artifact Contract (backtest)
# ----------------------------
def _utc_ts_compact() -> str:
    # line above: def _utc_ts_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _new_run_id(prefix: str = "bt") -> str:
    # line above: def _new_run_id(prefix: str = "bt") -> str:
    return f"{prefix}_{_utc_ts_compact()}_{secrets.token_hex(4)}"


def _artifact_root() -> str:
    # line above: def _artifact_root() -> str:
    # Canonical artifact root for ALL run-scoped outputs.
    # Defaults to repo root when not provided.
    return os.environ.get("ARGUS_ARTIFACT_ROOT", r"C:\Argus\repo")


def _artifact_out_dir() -> str:
    # line above: def _artifact_out_dir() -> str:
    # Canonical out dir for backtest artifacts.
    return os.environ.get(
        "ARGUS_BT_ARTIFACT_DIR",
        os.path.join(_artifact_root(), "ops", "logs"),
    )


def _bt_paths(run_id: str) -> Dict[str, str]:
    # line above: def _bt_paths(run_id: str) -> Dict[str, str]:
    out_dir = _artifact_out_dir()
    return {
        "out_dir": out_dir,
        "bt_log": os.path.join(out_dir, f"bt_{run_id}.log"),
        "bt_summary": os.path.join(out_dir, f"bt_summary_{run_id}.json"),
        "bt_summary_latest": os.path.join(out_dir, "bt_summary_latest.json"),
        "events": os.path.join(out_dir, f"events_{run_id}.csv"),
        "signals": os.path.join(out_dir, f"signals_{run_id}.csv"),
        "equity": os.path.join(out_dir, f"equity_{run_id}.csv"),
        "trades": os.path.join(out_dir, f"trades_{run_id}.csv"),
        "event_counts": os.path.join(out_dir, f"event_counts_{run_id}.csv"),
        "entry_attempts": os.path.join(out_dir, f"entry_attempts_{run_id}.csv"),
        "entry_attempt_detail": os.path.join(out_dir, f"entry_attempt_detail_{run_id}.csv"),
        "run_header": os.path.join(out_dir, f"run_header_{run_id}.json"),
    }


def _ensure_bt_sandbox_live_paths() -> None:
    """
    CRITICAL:
    Backtest must NEVER write to the canonical live_* artifacts.

    We enforce this in-Python (not just in PowerShell), because any module that
    builds defaults before reading env will otherwise mutate live files.
    """
    # line above: def _ensure_bt_sandbox_live_paths() -> None:
    mode = (os.environ.get("ARGUS_MODE") or "").strip().lower()
    # accept "bt" or "backtest" (you had drift in earlier runs)
    if mode not in ("bt", "backtest"):
        return

    out_dir = _artifact_out_dir()
    os.makedirs(out_dir, exist_ok=True)

    os.environ.setdefault("LIVE_EVENTS_CSV", os.path.join(out_dir, "bt_sandbox_live_events.csv"))
    os.environ.setdefault("LIVE_SIGNALS_CSV", os.path.join(out_dir, "bt_sandbox_live_signals.csv"))

    # Optional: if any downstream code branches on this
    os.environ.setdefault("ARGUS_DISABLE_LIVE_ARTIFACTS", "1")


def _stable_hash(obj: Any) -> str:
    # line above: def _stable_hash(obj: Any) -> str:
    try:
        s = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        s = str(obj)
    import hashlib

    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def _get_git_sha(repo_root: str) -> str:
    # line above: def _get_git_sha(repo_root: str) -> str:
    try:
        head = os.path.join(repo_root, ".git", "HEAD")
        if not os.path.exists(head):
            return "unknown"
        ref = open(head, "r", encoding="utf-8").read().strip()
        if ref.startswith("ref:"):
            ref_path = os.path.join(repo_root, ".git", ref.split(":", 1)[1].strip())
            if os.path.exists(ref_path):
                return open(ref_path, "r", encoding="utf-8").read().strip()[:12]
        return ref[:12]
    except Exception:
        return "unknown"


def _write_json_atomic(path: str, obj: Any) -> bool:
    # line above: def _write_json_atomic(path: str, obj: Any) -> bool:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _append_log_line(path: str, msg: str) -> None:
    # line above: def _append_log_line(path: str, msg: str) -> None:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")
    except Exception:
        pass


def _csv_sanitize(v: Any) -> str:
    # line above: def _csv_sanitize(v: Any) -> str:
    try:
        s = "" if v is None else str(v)
    except Exception:
        s = ""
    return s.replace("\r", " ").replace("\n", " ").replace(",", " ")


# ----------------------------
# Env helpers
# ----------------------------
def _as_int_env(name: str, default: Optional[int] = None) -> Optional[int]:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    try:
        return int(str(v).strip())
    except Exception:
        return default


def _as_bool_env(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default


def _as_decimal_env(name: str, default: Optional[Decimal] = None) -> Optional[Decimal]:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    try:
        return Decimal(str(v).strip())
    except Exception:
        return default


def _as_str_env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return str(v).strip()


def _as_float_env(name: str, default: Optional[float] = None) -> Optional[float]:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    try:
        return float(str(v).strip())
    except Exception:
        return default


def _ensure_bt_cfg(cfg: dict) -> dict:
    """
    Make a backtest-safe config overlay without mutating the original dict.
    """
    # line above: def _ensure_bt_cfg(cfg: dict) -> dict:
    cfg = {**cfg}

    cfg["BACKTEST_MODE"] = True

    # Backtest has no async fill-back loop — ADAPTER mode would emit intents that
    # never update the ledger, causing the engine to re-enter on every eligible tick.
    # Force ENGINE mode so the engine calls ledger.buy/sell directly and position
    # state is correct for the next tick's risk/sizing checks.
    cfg["EXECUTION_MODE"] = "ENGINE"

    tf_1m_s = int(cfg.get("CANDLE_SECONDS", 60))
    cfg["STALE_TICK_SECONDS"] = max(int(cfg.get("STALE_TICK_SECONDS", 120)), tf_1m_s + 5)

    cfg.setdefault("TRADE_TRACKER_NON_ATOMIC_WRITES", True)

    cfg["DISABLE_TRADE_TRACKER_IN_BACKTEST"] = _as_bool_env(
        "DISABLE_TRADE_TRACKER_IN_BACKTEST",
        bool(cfg.get("DISABLE_TRADE_TRACKER_IN_BACKTEST", True)),
    )

    bt_conf_min = _as_int_env("BT_CONFLUENCE_MIN", None)
    if bt_conf_min is not None:
        cfg["CONFLUENCE_MIN_SCORE"] = int(bt_conf_min)

    bt_start_cash = _as_decimal_env("BT_START_CASH", None)
    if bt_start_cash is not None:
        cfg["START_CASH_USD"] = bt_start_cash

    bt_usd_per_trade = _as_decimal_env("BT_USD_PER_TRADE", None)
    if bt_usd_per_trade is not None:
        cfg["USD_PER_TRADE"] = bt_usd_per_trade

    bt_summary_every = _as_int_env("BT_SUMMARY_EVERY_SECONDS", None)
    if bt_summary_every is not None:
        cfg["SUMMARY_EVERY_SECONDS"] = int(bt_summary_every)

    bt_poll_fast = _as_float_env("BT_POLL_FAST_SECONDS", None)
    if bt_poll_fast is not None:
        cfg["POLL_FAST_SECONDS"] = float(bt_poll_fast)

    cfg["USE_SHOULD_EVENTS"] = _as_bool_env(
        "BT_USE_SHOULD_EVENTS",
        bool(cfg.get("USE_SHOULD_EVENTS", False)),
    )

    # ✅ Backtest measurability: ALWAYS synth bid/ask unless explicitly disabled.
    synth_env = _as_float_env("BT_SYNTH_SPREAD_BPS", None)
    if synth_env is not None:
        synth = float(synth_env)
    else:
        synth_cfg = float(cfg.get("BT_SYNTH_SPREAD_BPS", 0.0) or 0.0)
        if synth_cfg > 0.0:
            synth = synth_cfg
        else:
            synth = float(cfg.get("LIQ_SYNTH_SPREAD_FLOOR_BPS", 8) or 8)

    cfg["BT_SYNTH_SPREAD_BPS"] = float(synth)

    # ── Phase 12: empirical friction injection ─────────────────────────────
    # FRICTION_MODE: off (default) | constant | conditional | monte_carlo
    # FRICTION_REPORT_PATH: explicit path to friction_report_*.json
    # FRICTION_REPORT_LATEST: if "1", auto-load latest from log dir
    # In constant mode, SLIPPAGE_BPS is overridden with p50 from the report.
    # conditional/monte_carlo modes are applied per-tick by the walk-forward
    # and stress harnesses; here we only handle the constant override.
    friction_mode = os.environ.get("FRICTION_MODE", "off").strip().lower()
    if friction_mode not in ("", "off", "none"):
        try:
            from backtest.friction_injector import load_friction_report, load_latest_friction_report, FrictionInjector
            fp = os.environ.get("FRICTION_REPORT_PATH", "").strip()
            if fp and os.path.exists(fp):
                report = load_friction_report(fp)
            elif os.environ.get("FRICTION_REPORT_LATEST", "").strip() in ("1", "true", "yes"):
                log_dir = os.environ.get("ARGUS_LOG_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "ops", "logs"))
                report = load_latest_friction_report(log_dir)
            else:
                report = None

            if report is not None:
                injector = FrictionInjector(report, mode="constant")
                if injector.all_p50_bps > 0:
                    cfg = injector.inject_into_cfg(cfg)
                cfg["_friction_mode"] = friction_mode
                cfg["_friction_report_sufficient"] = injector.sufficient_data
        except Exception:
            pass  # friction injection is best-effort; never break a backtest run

    return cfg


def _p_decimal(d: Any, default: str = "0") -> Decimal:
    # line above: def _p_decimal(d: Any, default: str = "0") -> Decimal:
    if d is None:
        return Decimal(default)
    if isinstance(d, Decimal):
        return d
    try:
        return Decimal(str(d))
    except Exception:
        return Decimal(default)


def _extract_first_event(snap, names: set[str]) -> Optional[Any]:
    for ev in (getattr(snap, "events", None) or []):
        if getattr(ev, "name", "") in names:
            return ev
    return None


def _iso_utc(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()


def _synth_bid_ask(px: Decimal, *, spread_bps: float) -> Tuple[Optional[Decimal], Optional[Decimal]]:
    if spread_bps <= 0:
        return None, None
    try:
        bps = Decimal(str(spread_bps))
        half = (bps / Decimal("2")) / Decimal("10000")
        bid = px * (Decimal("1") - half)
        ask = px * (Decimal("1") + half)
        return bid, ask
    except Exception:
        return None, None


def _spread_bps_from_bid_ask(bid: Any, ask: Any) -> Optional[Decimal]:
    try:
        b = _p_decimal(bid, "0")
        a = _p_decimal(ask, "0")
        if b <= 0 or a <= 0 or a <= b:
            return None
        mid = (a + b) / Decimal("2")
        if mid <= 0:
            return None
        return (a - b) / mid * Decimal("10000")
    except Exception:
        return None


@dataclass(frozen=True)
class DamageProfile:
    name: str = "baseline"

    data_dropout_pct: float = 0.0
    freeze_px_pct: float = 0.0
    vol_zero_pct: float = 0.0
    epoch_jitter_s: int = 0
    max_freeze_run: int = 5

    tf_dropout: Optional[List[str]] = None
    signal_flip_pct: float = 0.0
    fake_score_bump: int = 0
    score_noise: int = 0
    force_regime: Optional[str] = None
    force_liq_block: bool = False

    seed: int = 1337


def _to_argus_profile_dict(profile: Optional[DamageProfile]) -> Optional[Dict[str, Any]]:
    if profile is None:
        return None

    tf_dropout = profile.tf_dropout
    if tf_dropout is not None:
        tf_dropout = [str(x).strip() for x in tf_dropout if str(x).strip()]

    d: Dict[str, Any] = {
        "name": profile.name,
        "seed": int(profile.seed),
        "tf_dropout": tf_dropout,
        "signal_flip_pct": float(profile.signal_flip_pct or 0.0),
        "fake_score_bump": int(profile.fake_score_bump or 0),
        "score_noise": int(profile.score_noise or 0),
        "force_regime": (str(profile.force_regime).strip().upper() if profile.force_regime else None),
        "force_liq_block": bool(profile.force_liq_block),
    }

    any_on = False
    if d.get("tf_dropout"):
        any_on = True
    if float(d.get("signal_flip_pct", 0.0)) > 0:
        any_on = True
    if int(d.get("fake_score_bump", 0)) != 0:
        any_on = True
    if int(d.get("score_noise", 0)) > 0:
        any_on = True
    if d.get("force_regime") is not None:
        any_on = True
    if bool(d.get("force_liq_block", False)):
        any_on = True

    return d if any_on else None


def apply_damage_to_ticks(
    ticks: Iterable[PriceTick],
    *,
    profile: Optional[DamageProfile],
    candle_seconds: int,
) -> Iterable[PriceTick]:
    if profile is None or profile.name == "baseline":
        for t in ticks:
            yield t
        return

    rng = random.Random(int(profile.seed))
    last_px: Optional[Decimal] = None
    last_epoch: Optional[int] = None
    freeze_run = 0

    for t in ticks:
        if profile.data_dropout_pct > 0 and rng.random() < float(profile.data_dropout_pct):
            continue

        epoch = int(getattr(t, "epoch", 0))
        px = getattr(t, "px", None)
        vol_1m = getattr(t, "vol_1m", None)

        px_dec: Optional[Decimal]
        if px is None:
            px_dec = None
        else:
            try:
                px_dec = _p_decimal(px, "0")
            except Exception:
                px_dec = None

        do_freeze = (
            profile.freeze_px_pct > 0
            and last_px is not None
            and freeze_run < int(profile.max_freeze_run)
            and rng.random() < float(profile.freeze_px_pct)
        )
        if do_freeze:
            px_dec = last_px
            freeze_run += 1
        else:
            freeze_run = 0

        if profile.vol_zero_pct > 0 and vol_1m is not None and rng.random() < float(profile.vol_zero_pct):
            vol_1m = Decimal("0")
        elif vol_1m is not None:
            try:
                vol_1m = _p_decimal(vol_1m, "0")
            except Exception:
                vol_1m = None

        if int(profile.epoch_jitter_s) != 0:
            j = int(profile.epoch_jitter_s)
            epoch = epoch + rng.randint(-j, +j)

        if last_epoch is not None and epoch < last_epoch:
            epoch = last_epoch

        if px_dec is not None:
            last_px = px_dec
        last_epoch = epoch

        yield PriceTick(
            ts=_iso_utc(epoch),
            px=px_dec if px_dec is not None else Decimal("0"),
            epoch=epoch,
            vol_1m=vol_1m if isinstance(vol_1m, Decimal) else (None if vol_1m is None else _p_decimal(vol_1m, "0")),
            bid=getattr(t, "bid", None),
            ask=getattr(t, "ask", None),
        )


def _build_close_series_with_volume(candles) -> List[Tuple[int, Any, Any]]:
    out: List[Tuple[int, Any, Any]] = []
    for c in candles:
        e = int(getattr(c, "epoch", 0))
        close = getattr(c, "close", None)
        vol = getattr(c, "volume", None)
        if close is None or e <= 0:
            continue
        out.append((e, close, vol if vol is not None else None))
    return out


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _record_entry_attempt_metrics(out: Any, snap: Any, tick: Optional[Any] = None) -> None:
    if not hasattr(out, "record_entry_attempt"):
        return

    liq_spread_bps = getattr(snap, "liq_spread_bps", None)
    liq_vol_1m = getattr(snap, "liq_vol_1m", None)
    liq_vol_baseline = getattr(snap, "liq_vol_baseline", None)
    liq_atr_norm = getattr(snap, "liq_atr_norm", None)

    if liq_spread_bps is None:
        bid = None
        ask = None
        if tick is not None:
            bid = getattr(tick, "bid", None)
            ask = getattr(tick, "ask", None)
        if bid is None or ask is None:
            bid = getattr(snap, "bid", None)
            ask = getattr(snap, "ask", None)
        liq_spread_bps = _spread_bps_from_bid_ask(bid, ask)

    if liq_vol_1m is None and tick is not None:
        liq_vol_1m = getattr(tick, "vol_1m", None)

    try:
        out.record_entry_attempt(
            liq_spread_bps=liq_spread_bps,
            liq_vol_1m=liq_vol_1m,
            liq_vol_baseline=liq_vol_baseline,
            liq_atr_norm=liq_atr_norm,
        )
    except Exception:
        return


def _truncate_backtest_logs_if_requested(*, enabled: bool, paths: Dict[str, str]) -> None:
    """
    Backtests should not append across runs.
    Default: enabled.

    NOTE: We only delete files under the *canonical* out_dir for this run.
    We do NOT touch repo\\logs or any other drift directories here.
    """
    # line above: def _truncate_backtest_logs_if_requested(*, enabled: bool, paths: Dict[str, str]) -> None:
    if not enabled:
        return

    # drop any legacy shared (non-run-scoped) files the io_logs module might use
    for p in (signals_csv_path(), events_csv_path()):
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

    # drop canonical run-scoped outputs for this run_id (if rerun with same ID)
    for k in ("events", "signals", "equity", "trades", "event_counts", "entry_attempts", "entry_attempt_detail"):
        p = paths.get(k)
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

    # drop any run-scoped files generated via io_logs helpers (if they differ)
    for p in (bt_events_csv_path(os.environ.get("ARGUS_RUN_ID", "")),
              bt_signals_csv_path(os.environ.get("ARGUS_RUN_ID", "")),
              bt_equity_csv_path(os.environ.get("ARGUS_RUN_ID", ""))):
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def _write_run_header(*, run_id: str, mode: str, cfg: Dict[str, Any], candles_csv: str, out_dir: str) -> Optional[str]:
    try:
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        hdr = {
            "run_id": run_id,
            "mode": mode,
            "git_sha": _get_git_sha(repo_root),
            "config_hash": _stable_hash(cfg),
            "symbol": str(cfg.get("SYMBOL", "")),
            "timeframes": [str(cfg.get("TF_FAST", "1m")), str(cfg.get("TF_SLOW", "5m"))],
            "data_root": os.path.dirname(os.path.abspath(candles_csv)),
            "start_ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "repo_root": repo_root,
            "artifact_root": _artifact_root(),
            "artifact_dir": out_dir,
            "candles_csv": os.path.abspath(candles_csv),
        }
        path = os.path.join(out_dir, f"run_header_{run_id}.json")
        ok = _write_json_atomic(path, hdr)
        return path if ok else None
    except Exception:
        return None


def _write_bt_summary(
    *,
    run_id: str,
    summary: Dict[str, Any],
    out_dir: str,
    paths: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    try:
        os.makedirs(out_dir, exist_ok=True)
        path_run = os.path.join(out_dir, f"bt_summary_{run_id}.json")
        path_latest = os.path.join(out_dir, "bt_summary_latest.json")

        if paths:
            summary = {
                **summary,
                "run_id": run_id,
                "mode": summary.get("mode") or "bt",
                "artifact_dir": out_dir,
                "signals_csv": os.path.abspath(paths.get("signals", "")) if paths.get("signals") else None,
                "events_csv": os.path.abspath(paths.get("events", "")) if paths.get("events") else None,
                "equity_csv": os.path.abspath(paths.get("equity", "")) if paths.get("equity") else None,
                "trades_csv": os.path.abspath(paths.get("trades", "")) if paths.get("trades") else None,
                "event_counts_csv": os.path.abspath(paths.get("event_counts", "")) if paths.get("event_counts") else None,
                "entry_attempts_csv": os.path.abspath(paths.get("entry_attempts", "")) if paths.get("entry_attempts") else None,
                "entry_attempt_detail_csv": os.path.abspath(paths.get("entry_attempt_detail", "")) if paths.get("entry_attempt_detail") else None,
                "run_header_json": os.path.abspath(paths.get("run_header", "")) if paths.get("run_header") else None,
            }

        ok1 = _write_json_atomic(path_run, summary)
        ok2 = _write_json_atomic(path_latest, summary)
        return (path_run if ok1 else None, path_latest if ok2 else None)
    except Exception:
        return None, None


def run_backtest(
    *,
    candles_csv: str,
    csv_format_hint: str = "auto",
    limit: Optional[int] = None,
    write_logs: bool = True,
    cfg_override: Optional[dict] = None,
    damage: Optional[DamageProfile] = None,
) -> BacktestResults:
    cfg = cfg_override if cfg_override is not None else load_config()
    cfg = _ensure_bt_cfg(cfg)

    tf_1m_s = int(cfg.get("CANDLE_SECONDS", 60))
    synth_spread_bps = float(cfg.get("BT_SYNTH_SPREAD_BPS", 0.0) or 0.0)

    # Phase 7: establish mode + run_id EARLY
    run_id = os.environ.get("ARGUS_RUN_ID") or _new_run_id("bt")
    os.environ["ARGUS_RUN_ID"] = run_id

    # IMPORTANT: match your PowerShell contract (ARGUS_MODE=bt)
    os.environ["ARGUS_MODE"] = "bt"

    # Canonical artifact sink for the run (single source of truth)
    out_dir = _artifact_out_dir()
    os.makedirs(out_dir, exist_ok=True)

    # CRITICAL: export the chosen sink so io_logs (and anything else) can’t drift to repo\logs
    # line above: out_dir = _artifact_out_dir()
    os.environ["ARGUS_ARTIFACT_ROOT"] = _artifact_root()
    os.environ["ARGUS_BT_ARTIFACT_DIR"] = out_dir
    # compat knobs (in case io_logs uses different env names)
    os.environ["ARGUS_LOG_DIR"] = out_dir
    os.environ["ARGUS_LOGS_DIR"] = out_dir

    # Enforce sandboxing of LIVE_* CSVs (prevents mutation of live_events/signals)
    _ensure_bt_sandbox_live_paths()

    paths = _bt_paths(run_id)

    # Speed knobs (runner-only)
    bt_print_events = _as_bool_env("BT_PRINT_EVENTS", False)
    bt_signal_log_every_n = _as_int_env("BT_SIGNAL_LOG_EVERY_N", 1) or 1
    bt_equity_every_n = _as_int_env("BT_EQUITY_EVERY_N", 1) or 1

    # Backtest log hygiene (default ON)
    bt_truncate_logs = _as_bool_env("BT_TRUNCATE_LOGS", True)

    # ---- Phase 7 testing window knobs ----
    # Line above: bt_truncate_logs = _as_bool_env("BT_TRUNCATE_LOGS", True)
    bt_last_n = _as_bool_env("BACKTEST_LAST_N", True)  # if limit is set, prefer last N candles by default

    # Argus feature sabotage: pass profile to engine via cfg["ARGUS_PROFILE"]
    argus_profile = _to_argus_profile_dict(damage)
    if argus_profile is not None:
        cfg = {
            **cfg,
            "ARGUS_PROFILE": argus_profile,
            "ARGUS_SEED": int(damage.seed if damage else 1337),
        }

    state = BotState.from_config(cfg)
    symbol = state.symbol

    # ---- run-scoped artifact paths (single source of truth) ----
    events_path = bt_events_csv_path(run_id)
    signals_path = bt_signals_csv_path(run_id)
    equity_path = bt_equity_csv_path(run_id)
    # note: io_logs helpers may point into out_dir; if they don't, you will SEE IT immediately

    if write_logs:
        _truncate_backtest_logs_if_requested(enabled=bt_truncate_logs, paths=paths)
        ensure_logs()  # creates sandbox LIVE_* + upgrades sandbox header if needed

        # Ensure run-scoped headers exist and are compatible
        ensure_signals_header_matches_path(signals_path)  # create/upgrade header for run-scoped signals
        paths["run_header"] = os.path.join(out_dir, f"run_header_{run_id}.json")
        _write_run_header(run_id=run_id, mode="bt", cfg=cfg, candles_csv=candles_csv, out_dir=out_dir)
    else:
        _write_run_header(run_id=run_id, mode="bt", cfg=cfg, candles_csv=candles_csv, out_dir=out_dir)

    candles = load_candles_csv(candles_csv, format_hint=csv_format_hint, limit=limit)

    # ---- Phase 7 testing window: force last N candles if requested ----
    # Line above: candles = load_candles_csv(candles_csv, format_hint=csv_format_hint, limit=limit)
    if limit is not None and limit > 0 and bt_last_n and len(candles) > limit:
        candles = candles[-limit:]

    if not candles:
        raise RuntimeError("No candles loaded. Check CSV format/path.")

    close_series = _build_close_series_with_volume(candles)
    base_ticks = ticks_from_close_series(close_series, as_candle_close=True, candle_seconds=tf_1m_s)
    ticks = apply_damage_to_ticks(base_ticks, profile=damage, candle_seconds=tf_1m_s)

    start_epoch = int(candles[0].epoch)
    end_epoch = int(candles[-1].epoch)

    start_equity = _p_decimal(cfg.get("START_CASH_USD", "0"), "0")
    equity_curve: List[Tuple[int, Decimal]] = []
    trades: List[Trade] = []
    open_trade: Optional[Trade] = None

    BUY_EVENTS = {"WOULD_BUY", "SHOULD_BUY", "ENTRY_FILLED"}
    SELL_EVENTS = {"WOULD_SELL", "SHOULD_SELL"}

    out = BacktestResults(
        symbol=symbol,
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        start_equity=start_equity,
        end_equity=start_equity,
        trades=trades,
        equity_curve=equity_curve,
    )
    # set run identity if the results object supports it
    try:
        out.run_id = run_id
        out.mode = "bt"
    except Exception:
        pass

    http = make_http()

    try:
        i = 0
        for tick in ticks:
            i += 1

            # synth bid/ask for candle-close ticks (measurable spread stats)
            if (getattr(tick, "bid", None) is None or getattr(tick, "ask", None) is None) and synth_spread_bps > 0:
                try:
                    bid, ask = _synth_bid_ask(_p_decimal(getattr(tick, "px", "0"), "0"), spread_bps=synth_spread_bps)
                    if bid is not None and ask is not None:
                        tick = PriceTick(
                            ts=getattr(tick, "ts", ""),
                            px=_p_decimal(getattr(tick, "px", "0"), "0"),
                            epoch=int(getattr(tick, "epoch", 0)),
                            vol_1m=getattr(tick, "vol_1m", None),
                            bid=bid,
                            ask=ask,
                        )
                except Exception:
                    pass

            snap = step(state, tick, cfg, paused=False, http=http)

            # run-scoped equity curve + optional equity CSV
            if bt_equity_every_n <= 1 or (i % bt_equity_every_n == 0):
                eq_val = _p_decimal(getattr(snap, "equity_usd", getattr(snap, "equity", "0")), "0")
                equity_curve.append((int(getattr(snap, "epoch", 0)), eq_val))
                if write_logs:
                    try:
                        log_bt_equity(
                            run_id=run_id,
                            epoch=int(getattr(snap, "epoch", 0)),
                            equity_usd=eq_val,
                            cash_usd=getattr(snap, "cash_usd", getattr(snap, "cash", "")),
                            position_qty=getattr(snap, "position_qty", getattr(snap, "qty", "")),
                        )
                    except Exception:
                        pass

            attempted_this_tick = False

            for ev in (getattr(snap, "events", None) or []):
                name = str(getattr(ev, "name", "") or "")
                msg = str(getattr(ev, "message", "") or "")

                # Runner owns attempt boundaries; engine debug events can exist but we don't re-count them here.
                if name in ("ENTRY_ATTEMPT", "ENTRY_METRICS"):
                    continue

                # Record attempt once per tick on first boundary (BUY or MISSED_BUY_*)
                if (name in BUY_EVENTS or name.startswith("MISSED_BUY_")) and not attempted_this_tick:
                    attempted_this_tick = True

                    attempt_msg = f"ATTEMPT | px={getattr(snap, 'px', '')} event={name} {msg}".strip()
                    metrics_msg = (
                        "METRICS | "
                        f"liq_spread_bps={getattr(snap, 'liq_spread_bps', None)} "
                        f"liq_vol_1m={getattr(snap, 'liq_vol_1m', None)} "
                        f"liq_vol_baseline={getattr(snap, 'liq_vol_baseline', None)} "
                        f"liq_atr_norm={getattr(snap, 'liq_atr_norm', None)} "
                        f"tick_vol_1m={getattr(tick, 'vol_1m', None)} "
                        f"tick_bid={getattr(tick, 'bid', None)} "
                        f"tick_ask={getattr(tick, 'ask', None)}"
                    )

                    # Counts + sampling are owned by BacktestResults
                    try:
                        out.add_event("ENTRY_METRICS", metrics_msg, snapshot=snap)
                    except Exception:
                        pass
                    try:
                        out.add_event("ENTRY_ATTEMPT", attempt_msg, snapshot=snap)
                    except Exception:
                        pass

                    if write_logs:
                        # write the synthetic boundary events into the SAME events file
                        try:
                            log_bt_event(
                                symbol=symbol,
                                epoch=int(getattr(snap, "epoch", 0)),
                                price=getattr(snap, "px", ""),
                                event="ENTRY_METRICS",
                                detail=metrics_msg,
                                paused=bool(getattr(snap, "paused", False)),
                                stale=bool(getattr(snap, "stale", False)),
                                action=str(getattr(snap, "action", "")),
                                action_reason=str(getattr(snap, "action_reason", "")),
                                risk_blocked_reason=str(getattr(snap, "risk_blocked_reason", "")),
                                confluence_score=getattr(snap, "confluence_score", None),
                                confluence_gate=str(getattr(snap, "confluence_gate", "")),
                                confluence_reasons=str(getattr(snap, "confluence_reasons", "")),
                                regime=str(getattr(snap, "regime", "") or ""),
                                ac_adjusted_gate=str(getattr(snap, "ac_adjusted_gate", "") or ""),
                                vol_used=getattr(snap, "vol_used", None),
                                sizing_note=str(getattr(snap, "sizing_note", "") or ""),
                                notify_title=str(getattr(ev, "notify_title", "") or ""),
                                notify_body=str(getattr(ev, "notify_body", "") or ""),
                                path=events_path,
                            )
                            log_bt_event(
                                symbol=symbol,
                                epoch=int(getattr(snap, "epoch", 0)),
                                price=getattr(snap, "px", ""),
                                event="ENTRY_ATTEMPT",
                                detail=attempt_msg,
                                paused=bool(getattr(snap, "paused", False)),
                                stale=bool(getattr(snap, "stale", False)),
                                action=str(getattr(snap, "action", "")),
                                action_reason=str(getattr(snap, "action_reason", "")),
                                risk_blocked_reason=str(getattr(snap, "risk_blocked_reason", "")),
                                confluence_score=getattr(snap, "confluence_score", None),
                                confluence_gate=str(getattr(snap, "confluence_gate", "")),
                                confluence_reasons=str(getattr(snap, "confluence_reasons", "")),
                                regime=str(getattr(snap, "regime", "") or ""),
                                ac_adjusted_gate=str(getattr(snap, "ac_adjusted_gate", "") or ""),
                                vol_used=getattr(snap, "vol_used", None),
                                sizing_note=str(getattr(snap, "sizing_note", "") or ""),
                                notify_title=str(getattr(ev, "notify_title", "") or ""),
                                notify_body=str(getattr(ev, "notify_body", "")),
                                path=events_path,
                            )
                        except Exception:
                            pass

                # Entry attempt sampling (for BUY and MISSED_BUY_*)
                if name in BUY_EVENTS or name.startswith("MISSED_BUY_"):
                    _record_entry_attempt_metrics(out, snap, tick)

                # Always count/log event itself
                try:
                    out.add_event(name, msg, snapshot=snap)
                except Exception:
                    pass

                if write_logs:
                    try:
                        log_bt_event(
                            symbol=symbol,
                            epoch=int(getattr(snap, "epoch", 0)),
                            price=getattr(snap, "px", ""),
                            event=name,
                            detail=msg,
                            paused=bool(getattr(snap, "paused", False)),
                            stale=bool(getattr(snap, "stale", False)),
                            action=str(getattr(snap, "action", "")),
                            action_reason=str(getattr(snap, "action_reason", "")),
                            risk_blocked_reason=str(getattr(snap, "risk_blocked_reason", "")),
                            confluence_score=getattr(snap, "confluence_score", None),
                            confluence_gate=str(getattr(snap, "confluence_gate", "")),
                            confluence_reasons=str(getattr(snap, "confluence_reasons", "")),
                            regime=str(getattr(snap, "regime", "") or ""),
                            ac_adjusted_gate=str(getattr(snap, "ac_adjusted_gate", "")),
                            vol_used=getattr(snap, "vol_used", None),
                            sizing_note=str(getattr(snap, "sizing_note", "") or ""),
                            notify_title=str(getattr(ev, "notify_title", "") or ""),
                            notify_body=str(getattr(ev, "notify_body", "") or ""),
                            path=events_path,
                        )
                    except Exception:
                        pass

                if bt_print_events:
                    print("[BT_EVENT]", name, msg)

            # Trade extraction (depends on events; your engine may emit WOULD/SHOULD)
            buy_ev = _extract_first_event(snap, {"WOULD_BUY", "SHOULD_BUY"})
            if buy_ev is not None:
                p = parse_buy_event_message(buy_ev.message)
                fill_px = p.get("fill_px") or p.get("px") or Decimal("0")
                qty = p.get("qty") or Decimal("0")

                open_trade = Trade(
                    entry_epoch=int(getattr(snap, "epoch", 0)),
                    entry_px=_p_decimal(fill_px, "0"),
                    qty=_p_decimal(qty, "0"),
                )
                trades.append(open_trade)

            sell_ev = _extract_first_event(snap, SELL_EVENTS)
            if sell_ev is not None:
                p = parse_sell_event_message(sell_ev.message)
                exit_px = p.get("eff_sell") or p.get("px") or Decimal("0")

                if open_trade is not None and open_trade.exit_epoch is None:
                    open_trade.exit_epoch = int(getattr(snap, "epoch", 0))
                    open_trade.exit_px = _p_decimal(exit_px, "0")
                    open_trade.realized_usd = _p_decimal(p.get("realized_trade", "0"), "0")

                    if getattr(snap, "mfe_pct", None) is not None:
                        try:
                            open_trade.mfe_pct = _p_decimal(snap.mfe_pct, "0") * Decimal("100")
                        except Exception:
                            pass
                    if getattr(snap, "mae_pct", None) is not None:
                        try:
                            open_trade.mae_pct = _p_decimal(snap.mae_pct, "0") * Decimal("100")
                        except Exception:
                            pass

                    open_trade = None

            # ---- run-scoped signals ----
            if write_logs and (bt_signal_log_every_n <= 1 or (i % bt_signal_log_every_n == 0)):
                try:
                    # legacy compatibility (writes to sandbox LIVE_SIGNALS_CSV)
                    log_signal_snapshot(snap, symbol=symbol, price=getattr(snap, "px", None))
                except Exception:
                    pass

                try:
                    # canonical run-scoped
                    ensure_signals_header_matches_path(signals_path)
                    log_bt_signal_snapshot(snap, run_id=run_id, symbol=symbol, price=getattr(snap, "px", None))
                except Exception:
                    pass

    finally:
        try:
            http.close()
        except Exception:
            pass

    # compute final equity from ledger
    last_close = _p_decimal(candles[-1].close, "0")
    end_equity = _p_decimal(state.ledger.equity_usd(last_close), "0")
    out.end_equity = end_equity

    # Write canonical per-run artifacts (via BacktestResults helpers)
    if write_logs:
        try:
            out.write_trades_csv(paths["trades"])
        except Exception as e:
            _append_log_line(paths["bt_log"], f"[WARN] trades write failed: {e}")

        try:
            out.write_event_counts_csv(paths["event_counts"])
        except Exception as e:
            _append_log_line(paths["bt_log"], f"[WARN] event_counts write failed: {e}")

        try:
            out.write_entry_attempt_stats_csv(paths["entry_attempts"])
        except Exception as e:
            _append_log_line(paths["bt_log"], f"[WARN] entry_attempts write failed: {e}")

        # Phase 7.1 detail CSV (if you included it in results.py)
        try:
            if hasattr(out, "write_entry_attempt_detail_csv"):
                out.write_entry_attempt_detail_csv(paths["entry_attempt_detail"])
        except Exception as e:
            _append_log_line(paths["bt_log"], f"[WARN] entry_attempt_detail write failed: {e}")

        # Summary artifacts (run-scoped + latest)
        try:
            _write_bt_summary(run_id=run_id, summary=out.summary(), out_dir=out_dir, paths=paths)
        except Exception:
            pass

    return out


if __name__ == "__main__":
    # --------- LINE ABOVE: if __name__ == "__main__":
    os.environ.setdefault("ARGUS_ARTIFACT_ROOT", r"C:\Argus\repo")
    os.environ.setdefault("ARGUS_RUN_ID", _new_run_id("bt"))
    os.environ.setdefault("ARGUS_MODE", "bt")

    # canonical sink (export before any io_logs defaults get computed)
    out_dir = _artifact_out_dir()
    os.environ.setdefault("ARGUS_BT_ARTIFACT_DIR", os.path.join(_artifact_root(), "ops", "logs"))
    os.environ.setdefault("ARGUS_LOG_DIR", out_dir)
    os.environ.setdefault("ARGUS_LOGS_DIR", out_dir)

    # enforce sandbox paths even when run as a script
    _ensure_bt_sandbox_live_paths()

    default_csv = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),  # repo root
        "data",
        "eth_usd_1m.csv",
    )

    candles_csv = os.environ.get("BACKTEST_CSV", default_csv)
    fmt = os.environ.get("BACKTEST_FORMAT", "auto")

    # Convenience: BACKTEST_DAYS overrides BACKTEST_LIMIT if set
    # Line above: fmt = os.environ.get("BACKTEST_FORMAT", "auto")
    days = os.environ.get("BACKTEST_DAYS")
    if days and str(days).strip().replace(".", "", 1).isdigit():
        limit_n = int(float(str(days).strip()) * 1440)
    else:
        limit = os.environ.get("BACKTEST_LIMIT")
        limit_n = int(limit) if limit and str(limit).isdigit() else None

    write_logs = _as_bool_env("BT_WRITE_LOGS", True)

    res = run_backtest(
        candles_csv=candles_csv,
        csv_format_hint=fmt,
        limit=limit_n,
        write_logs=write_logs,
    )

    print(res.summary())