# backtest/runner.py
from __future__ import annotations

import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, List, Tuple, Any, Dict, Iterable

# ✅ FIX: import engine-layer modules from the parent package (Argus)
from ..config import load_config
from ..state import BotState
from ..engine import step
from ..io_logs import (
    ensure_logs,
    log_signal_snapshot,
    log_bt_event,
    signals_csv_path,
    events_csv_path,
)
from ..feed_coinbase import make_http

# --------- LINE ABOVE: from ..feed_coinbase import make_http
# IMPORTANT:
# - Use RELATIVE imports inside the backtest package so `python -m ...Argus.backtest.runner` works reliably.
from .loader import load_candles_csv
from .feed import ticks_from_close_series, PriceTick
from .results import (
    BacktestResults,
    Trade,
    parse_buy_event_message,
    parse_sell_event_message,
)


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
    cfg = {**cfg}

    # Backtest-mode toggles (used by engine/state)
    cfg["BACKTEST_MODE"] = True

    # Backtest safety: stale tick guard should never suppress actions during candle-jumps.
    tf_1m_s = int(cfg.get("CANDLE_SECONDS", 60))
    cfg["STALE_TICK_SECONDS"] = max(int(cfg.get("STALE_TICK_SECONDS", 120)), tf_1m_s + 5)

    # Optional: make tracker safer for backtests (prevents WinError 5 when file is open)
    cfg.setdefault("TRADE_TRACKER_NON_ATOMIC_WRITES", True)

    # Optional: disable tracker during backtest via env without editing config.py
    cfg["DISABLE_TRADE_TRACKER_IN_BACKTEST"] = _as_bool_env(
        "DISABLE_TRADE_TRACKER_IN_BACKTEST",
        bool(cfg.get("DISABLE_TRADE_TRACKER_IN_BACKTEST", True)),
    )

    # Optional: override confluence minimum for quick experiments:
    bt_conf_min = _as_int_env("BT_CONFLUENCE_MIN", None)
    if bt_conf_min is not None:
        cfg["CONFLUENCE_MIN_SCORE"] = int(bt_conf_min)

    # Optional: override start cash for experiments:
    bt_start_cash = _as_decimal_env("BT_START_CASH", None)
    if bt_start_cash is not None:
        cfg["START_CASH_USD"] = bt_start_cash

    # Optional: override USD_PER_TRADE (if you’re not using vol sizing)
    bt_usd_per_trade = _as_decimal_env("BT_USD_PER_TRADE", None)
    if bt_usd_per_trade is not None:
        cfg["USD_PER_TRADE"] = bt_usd_per_trade

    # Optional: override poll knobs (sometimes useful when debugging logs)
    bt_summary_every = _as_int_env("BT_SUMMARY_EVERY_SECONDS", None)
    if bt_summary_every is not None:
        cfg["SUMMARY_EVERY_SECONDS"] = int(bt_summary_every)

    bt_poll_fast = _as_float_env("BT_POLL_FAST_SECONDS", None)
    if bt_poll_fast is not None:
        cfg["POLL_FAST_SECONDS"] = float(bt_poll_fast)

    # Optional: force "should" events so parsing is consistent in backtests
    cfg["USE_SHOULD_EVENTS"] = _as_bool_env("BT_USE_SHOULD_EVENTS", bool(cfg.get("USE_SHOULD_EVENTS", False)))

    # --------- LINE ABOVE: cfg["USE_SHOULD_EVENTS"] = _as_bool_env(...)
    # ✅ Backtest measurability: ALWAYS synth bid/ask unless explicitly disabled.
    # Candle-close backtests don't have L2, so spread stats will be None otherwise.
    synth_env = _as_float_env("BT_SYNTH_SPREAD_BPS", None)
    if synth_env is not None:
        synth = float(synth_env)  # allow explicit override including 0
    else:
        synth_cfg = float(cfg.get("BT_SYNTH_SPREAD_BPS", 0.0) or 0.0)
        if synth_cfg > 0.0:
            synth = synth_cfg
        else:
            synth = float(cfg.get("LIQ_SYNTH_SPREAD_FLOOR_BPS", 8) or 8)

    cfg["BT_SYNTH_SPREAD_BPS"] = float(synth)

    return cfg


def _p_decimal(d: Any, default: str = "0") -> Decimal:
    """
    Best-effort Decimal coercion.
    """
    if d is None:
        return Decimal(default)
    if isinstance(d, Decimal):
        return d
    try:
        return Decimal(str(d))
    except Exception:
        return Decimal(default)


def _extract_first_event(snap, names: set[str]) -> Optional[Any]:
    """
    Prefer the first matching event in the order emitted by engine.
    """
    for ev in (getattr(snap, "events", None) or []):
        if getattr(ev, "name", "") in names:
            return ev
    return None


def _iso_utc(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()


def _synth_bid_ask(
    px: Decimal,
    *,
    spread_bps: float,
) -> Tuple[Optional[Decimal], Optional[Decimal]]:
    """
    Deterministically synthesize bid/ask around px using a configured spread_bps.
    spread_bps=10 => total spread 0.10% => +/- 0.05% around mid.
    """
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
    """
    Compute spread in bps from bid/ask: (ask-bid)/mid * 10_000.
    """
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


# --------- LINE ABOVE: def _spread_bps_from_bid_ask(
@dataclass(frozen=True)
class DamageProfile:
    name: str = "baseline"

    # --- Tick / data integrity damage ---
    data_dropout_pct: float = 0.0
    freeze_px_pct: float = 0.0
    vol_zero_pct: float = 0.0
    epoch_jitter_s: int = 0
    max_freeze_run: int = 5

    # --- Feature sabotage ---
    tf_dropout: Optional[List[str]] = None
    signal_flip_pct: float = 0.0
    fake_score_bump: int = 0
    score_noise: int = 0
    force_regime: Optional[str] = None
    force_liq_block: bool = False

    # --- Determinism ---
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


def _bt_event_row(
    *,
    symbol: str,
    snap: Any,
    ev: Any,
    name: str,
    msg: str,
) -> Dict[str, Any]:
    return dict(
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
        confluence_score=(
            None
            if getattr(snap, "confluence_score", None) is None
            else _safe_int(getattr(snap, "confluence_score", 0), 0)
        ),
        confluence_gate=str(getattr(snap, "confluence_gate", "")),
        confluence_reasons=str(getattr(snap, "confluence_reasons", "")),
        regime=str(getattr(snap, "regime", "") or ""),
        ac_adjusted_gate=str(getattr(snap, "ac_adjusted_gate", "") or ""),
        vol_used=getattr(snap, "vol_used", None),
        sizing_note=str(getattr(snap, "sizing_note", "") or ""),
        liq_ok=getattr(snap, "liq_ok", None),
        liq_spread_bps=getattr(snap, "liq_spread_bps", None),
        session=str(getattr(snap, "session", "") or ""),
        notify_title=str(getattr(ev, "notify_title", "") or ""),
        notify_body=str(getattr(ev, "notify_body", "") or ""),
    )


def _record_entry_attempt_metrics(out: Any, snap: Any, tick: Optional[Any] = None) -> None:
    """
    Record entry-time metrics with fallbacks:
      - Prefer snap.liq_* fields if present
      - Else compute spread_bps from (tick.bid, tick.ask) or (snap.bid, snap.ask)
      - Use tick.vol_1m as fallback for liq_vol_1m
    """
    if not hasattr(out, "record_entry_attempt"):
        return

    # --------- LINE ABOVE: if not hasattr(out, "record_entry_attempt"):
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


def _truncate_backtest_logs_if_requested(*, enabled: bool) -> None:
    """
    Backtests should not append across runs; it destroys analysis even if CSV is well-quoted.
    Default: enabled.
    """
    if not enabled:
        return

    # --------- LINE ABOVE: if not enabled:
    for p in (signals_csv_path(), events_csv_path()):
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


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

    # --------- LINE ABOVE: cfg = cfg_override if cfg_override is not None else load_config()
    cfg = _ensure_bt_cfg(cfg)
    tf_1m_s = int(cfg.get("CANDLE_SECONDS", 60))
    synth_spread_bps = float(cfg.get("BT_SYNTH_SPREAD_BPS", 0.0) or 0.0)

    # Speed knobs (runner-only)
    bt_print_events = _as_bool_env("BT_PRINT_EVENTS", False)
    bt_log_flush_n = _as_int_env("BT_LOG_FLUSH_N", 2000) or 2000
    bt_signal_log_every_n = _as_int_env("BT_SIGNAL_LOG_EVERY_N", 1) or 1
    bt_equity_every_n = _as_int_env("BT_EQUITY_EVERY_N", 1) or 1

    # Backtest log hygiene (default ON)
    bt_truncate_logs = _as_bool_env("BT_TRUNCATE_LOGS", True)

    # Argus feature sabotage: pass profile to engine via cfg["ARGUS_PROFILE"]
    argus_profile = _to_argus_profile_dict(damage)
    if argus_profile is not None:
        cfg = {**cfg, "ARGUS_PROFILE": argus_profile, "ARGUS_SEED": int(damage.seed if damage else 1337)}

    state = BotState.from_config(cfg)
    symbol = state.symbol

    if write_logs:
        _truncate_backtest_logs_if_requested(enabled=bt_truncate_logs)
        ensure_logs()

    candles = load_candles_csv(candles_csv, format_hint=csv_format_hint, limit=limit)
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

    BUY_EVENTS = {"WOULD_BUY", "SHOULD_BUY"}
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

    bt_event_buf: List[Dict[str, Any]] = []

    def _flush_bt_events() -> None:
        if not write_logs:
            return
        if not bt_event_buf:
            return
        for r in bt_event_buf:
            log_bt_event(**r)
        bt_event_buf.clear()

    http = make_http()
    try:
        i = 0
        for tick in ticks:
            i += 1

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

            if bt_equity_every_n <= 1 or (i % bt_equity_every_n == 0):
                equity_curve.append((int(snap.epoch), _p_decimal(getattr(snap, "equity_usd", "0"), "0")))

            attempted_this_tick = False

            for ev in (getattr(snap, "events", None) or []):
                name = str(getattr(ev, "name", "") or "")
                msg = str(getattr(ev, "message", "") or "")

                # --------- LINE ABOVE: msg = str(getattr(ev, "message", "") or "")
                # ✅ PATCH-1: always pass snapshot so BacktestResults can sample fields when available
                try:
                    out.add_event(name, msg, snapshot=snap)
                except Exception:
                    pass

                # Inject ENTRY_ATTEMPT once per tick if engine emitted a buy intent
                if name in BUY_EVENTS and not attempted_this_tick:
                    attempted_this_tick = True
                    attempt_msg = f"ATTEMPT | px={getattr(snap, 'px', '')} event={name} {msg}".strip()

                    # --------- LINE ABOVE: attempt_msg = f"ATTEMPT | px={getattr(snap, 'px', '')} event={name} {msg}".strip()
                    # Debug visibility: prove whether engine is producing baseline/atr_norm fields at entry time.
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

                    try:
                        out.add_event("ENTRY_METRICS", metrics_msg, snapshot=snap)
                    except Exception:
                        pass

                    if write_logs:
                        bt_event_buf.append(
                            _bt_event_row(
                                symbol=symbol,
                                snap=snap,
                                ev=ev,
                                name="ENTRY_METRICS",
                                msg=metrics_msg,
                            )
                        )

                    # --------- LINE ABOVE: attempt_msg = f"ATTEMPT | px=..."
                    try:
                        out.add_event("ENTRY_ATTEMPT", attempt_msg, snapshot=snap)
                    except Exception:
                        pass

                    if write_logs:
                        bt_event_buf.append(
                            _bt_event_row(
                                symbol=symbol,
                                snap=snap,
                                ev=ev,
                                name="ENTRY_ATTEMPT",
                                msg=attempt_msg,
                            )
                        )

                # --------- LINE ABOVE: if name in BUY_EVENTS and not attempted_this_tick:
                # ✅ PATCH-2: fallback sampling from tick bid/ask even if snap.liq_* missing
                if name in BUY_EVENTS or name.startswith("MISSED_BUY_"):
                    _record_entry_attempt_metrics(out, snap, tick)

                if write_logs:
                    bt_event_buf.append(
                        _bt_event_row(
                            symbol=symbol,
                            snap=snap,
                            ev=ev,
                            name=name,
                            msg=msg,
                        )
                    )
                    if len(bt_event_buf) >= int(bt_log_flush_n):
                        _flush_bt_events()

                if bt_print_events:
                    print("[BT_EVENT]", name, msg)

            buy_ev = _extract_first_event(snap, BUY_EVENTS)
            if buy_ev is not None:
                p = parse_buy_event_message(buy_ev.message)
                fill_px = p.get("fill_px") or p.get("px") or Decimal("0")
                qty = p.get("qty") or Decimal("0")

                open_trade = Trade(
                    entry_epoch=int(snap.epoch),
                    entry_px=_p_decimal(fill_px, "0"),
                    qty=_p_decimal(qty, "0"),
                )
                trades.append(open_trade)

            sell_ev = _extract_first_event(snap, SELL_EVENTS)
            if sell_ev is not None:
                p = parse_sell_event_message(sell_ev.message)
                exit_px = p.get("eff_sell") or p.get("px") or Decimal("0")

                if open_trade is not None and open_trade.exit_epoch is None:
                    open_trade.exit_epoch = int(snap.epoch)
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

            if write_logs and (bt_signal_log_every_n <= 1 or (i % bt_signal_log_every_n == 0)):
                try:
                    log_signal_snapshot(snap, symbol=symbol, price=getattr(snap, "px", None))
                except Exception:
                    pass

        _flush_bt_events()

    finally:
        try:
            http.close()
        except Exception:
            pass

    last_close = _p_decimal(candles[-1].close, "0")
    end_equity = _p_decimal(state.ledger.equity_usd(last_close), "0")
    out.end_equity = end_equity

    return out


if __name__ == "__main__":
    # --------- LINE ABOVE: if __name__ == "__main__":
    default_csv = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),  # .../Argus
        "data",
        "eth_usd_1m.csv",
    )

    candles_csv = os.environ.get("BACKTEST_CSV", default_csv)
    fmt = os.environ.get("BACKTEST_FORMAT", "auto")
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
