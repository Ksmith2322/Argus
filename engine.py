#!/usr/bin/env python3
# engine.py

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Optional, Tuple, Any, List, Dict
import hashlib
import random

from confluence import TFState
from decisions import DecisionSnapshot, EngineEvent
from strategy_phase2 import ma200_exit_level, StrategyState
from utils import pct_dist, safe_str

# Phase 5A
from structure import StructureResult

# Phase 18
from trendlines import TrendlineResult

# Phase 5B
from liquidity import LiquidityResult  # noqa: F401

# Phase 5C
from session import classify_session, apply_session_to_score

# ML Governor (Phase ML-1)
import ml_governor

# Cross-Coin Correlation Guard
from correlation_guard import can_enter_cross_coin
from btc_momentum_guard import check_btc_momentum, write_btc_trend_state


def choose_poll_seconds(cfg, in_pos: bool, min_dist: Optional[Decimal]) -> float:
    if min_dist is None:
        return float(cfg["POLL_MED_SECONDS"])

    if min_dist <= Decimal(str(cfg["TURBO_PCT"])):
        return float(cfg["POLL_TURBO_SECONDS"])

    if in_pos:
        if min_dist <= Decimal(str(cfg["NEAR_EXIT_PCT"])):
            return float(cfg["POLL_FAST_SECONDS"])
        return float(cfg["POLL_MED_SECONDS"])

    if min_dist <= Decimal(str(cfg["NEAR_ENTRY_PCT"])):
        return float(cfg["POLL_FAST_SECONDS"])

    return float(cfg["POLL_SLOW_SECONDS"])


def _bool_cfg(cfg: dict, k: str, default: bool = False) -> bool:
    v = cfg.get(k, default)
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return bool(v)


def _as_decimal(v: Any, default: str = "0") -> Decimal:
    if v is None:
        return Decimal(default)
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(default)


def _normalize_epoch_seconds(e: int) -> int:
    """
    Normalize epoch units to seconds (ms -> s) everywhere.
    """
    if not e:
        return 0
    if e >= 100_000_000_000:
        return int(e // 1000)
    return int(e)


def _execution_mode(cfg: dict, state: Any = None) -> str:
    mode = str(cfg.get("EXECUTION_MODE", "") or "").strip().upper()
    if not mode and state is not None:
        mode = str(getattr(state, "execution_mode", "") or "").strip().upper()
    return mode or "ENGINE"


def _adapter_mode_enabled(cfg: dict, state: Any = None) -> bool:
    if _execution_mode(cfg, state) != "ADAPTER":
        return False
    if state is None:
        return True
    return getattr(state, "execution_adapter", None) is not None


def _engine_mode_enabled(cfg: dict, state: Any = None) -> bool:
    return _execution_mode(cfg, state) == "ENGINE"


def _deterministic_suffix(symbol: str, side: str, epoch_s: int) -> str:
    """Deterministic 10-char hex from inputs — same inputs always produce same ID."""
    h = hashlib.sha256(f"{symbol}:{side}:{int(epoch_s)}".encode()).hexdigest()
    return h[:10]


def _new_intent_id(symbol: str, side: str, epoch_s: int) -> str:
    return f"{symbol}:{side}:{int(epoch_s)}:{_deterministic_suffix(symbol, side, epoch_s)}"


def _new_client_order_id(symbol: str, side: str, epoch_s: int) -> str:
    symbol_part = str(symbol).replace("-", "").replace("/", "").upper()
    side_part = str(side).upper()
    return f"argus-{symbol_part}-{side_part}-{int(epoch_s)}-{_deterministic_suffix(symbol, side, epoch_s)}"


def _compute_buy_qty_and_reason(ledger, px: Decimal, cfg: dict) -> Tuple[Decimal, str]:
    """
    Supports either:
      - ledger.compute_buy_qty(px, cfg) -> Decimal
      - ledger.compute_buy_qty(px, cfg) -> (Decimal, reason)
      - ledger.compute_buy_qty(cfg, px) -> Decimal or (Decimal, reason)
      - ledger.compute_buy_qty_and_reason(px, cfg) -> (Decimal, reason)
    """
    if hasattr(ledger, "compute_buy_qty_and_reason"):
        out = ledger.compute_buy_qty_and_reason(px, cfg)
        if isinstance(out, tuple) and len(out) == 2:
            return Decimal(out[0]), str(out[1])
        return Decimal(out), "OK"

    for args in ((px, cfg), (cfg, px)):
        try:
            out: Any = ledger.compute_buy_qty(*args)
            if isinstance(out, tuple) and len(out) == 2:
                return Decimal(out[0]), str(out[1])
            out_d = Decimal(out)
            return out_d, "OK" if out_d > 0 else "BLOCKED"
        except TypeError:
            continue

    return Decimal("0"), "BUY_QTY_FUNC_MISMATCH"


def _missed_buy_event_from_risk_reason(risk_reason: str) -> str:
    rr = (risk_reason or "").upper()
    if "MAX_TRADES" in rr:
        return "MISSED_BUY_MAX_TRADES_PER_DAY"
    if "DAILY_MAX_LOSS" in rr:
        return "MISSED_BUY_DAILY_MAX_LOSS"
    if "LOCKOUT" in rr or "COOLDOWN" in rr:
        return "MISSED_BUY_RISK_LOCKOUT"
    if "SPREAD" in rr:
        return "MISSED_BUY_LIQUIDITY"
    if "PORTFOLIO" in rr or "POSITION" in rr:
        return "MISSED_BUY_EXPOSURE_CAP"
    if "MARKET_DATA" in rr or "STALE" in rr:
        return "MISSED_BUY_STALE_DATA"
    return "MISSED_BUY_RISK_LOCKOUT"


def _missed_buy_event_from_qty_reason(qty_reason: str) -> str:
    qr = (qty_reason or "").upper()
    if "NO_CASH" in qr:
        return "MISSED_BUY_NO_CASH"
    if "EXPOSURE" in qr or "CAP" in qr:
        return "MISSED_BUY_EXPOSURE_CAP"
    if "MIN_ORDER" in qr:
        return "MISSED_BUY_MIN_ORDER"
    return (
        "MISSED_BUY_MIN_ORDER" if "MIN" in qr
        else "MISSED_BUY_EXPOSURE_CAP" if "CAP" in qr
        else "MISSED_BUY_NO_CASH"
    )


def _fmt_lockout(now_epoch: int, lockout_until_epoch: int) -> str:
    if not lockout_until_epoch:
        return "0"
    return f"{lockout_until_epoch} (rem={max(0, int(lockout_until_epoch - now_epoch))}s)"


def _to_tf_state(st: Optional[StrategyState], tf: str) -> Optional[TFState]:
    """
    CRITICAL INVARIANT:
      - if StrategyState is missing, return None (so ignore_missing_tfs can work)
      - never synthesize zeros here (zeros belong in ConfluenceEngine.norm()).
    """
    if st is None:
        return None
    return TFState(
        tf=tf,
        signal=int(st.signal),
        trend_ok=bool(st.trend_ok),
        score=int(st.score),
        reasons=str(st.reasons or ""),
    )


def _add_event(
    snap: DecisionSnapshot,
    name: str,
    message: str,
    notify_title: Optional[str] = None,
    notify_body: Optional[str] = None,
    client_order_id: Optional[str] = None,
    order_id: Optional[str] = None,
    trade_id: Optional[str] = None,
):
    snap.events.append(
        EngineEvent(
            name=name,
            message=message,
            notify_title=notify_title,
            notify_body=notify_body,
            client_order_id=client_order_id,
            order_id=order_id,
            trade_id=trade_id,
        )
    )


def _emit_missed_buy(
    snap: DecisionSnapshot,
    *,
    event: str,
    prefix: str,
    px: Decimal,
    confluence_min_score: int,
    cooldown_remaining: int,
    equity: Decimal,
    exposure: Decimal,
    extra: str = "",
) -> None:
    liq_part = (
        f"spread_bps={safe_str(getattr(snap,'liq_spread_bps',None))} "
        f"vol_1m={safe_str(getattr(snap,'liq_vol_1m',None))} "
        f"baseline={safe_str(getattr(snap,'liq_vol_baseline',None))} "
        f"atr_norm={safe_str(getattr(snap,'liq_atr_norm',None))} "
        f"liq_mode={safe_str(getattr(snap,'liq_mode',''))} "
        f"liq_ok={safe_str(getattr(snap,'liq_ok',None))} "
        f"liq_reasons={safe_str(getattr(snap,'liq_reasons',''))}"
    )
    conf_part = (
        f"conf_score={safe_str(getattr(snap,'confluence_score',None))} "
        f"min={int(confluence_min_score)} gate={safe_str(getattr(snap,'confluence_gate',''))} "
        f"conf_reasons={safe_str(getattr(snap,'confluence_reasons',''))}"
    )
    struct_part = f"struct={safe_str(getattr(snap,'structure_reasons',''))}"
    sess_part = f"session={safe_str(getattr(snap,'session',''))} labels={safe_str(getattr(snap,'session_labels',''))}"

    msg = (
        f"{prefix} | px={px} equity={equity:.2f} exposure={exposure:.2f} "
        f"cooldown_remaining={int(cooldown_remaining)}s | "
        f"{conf_part} | {struct_part} | {liq_part} | {sess_part}"
    )
    if extra:
        msg = msg + " | " + str(extra)

    _add_event(snap, event, msg)


def _should_bump_hold(state, now_epoch: int, cfg: dict) -> bool:
    every_s = int(cfg.get("HOLD_BUMP_EVERY_SECONDS", 300))
    if every_s <= 0:
        return False

    last = int(getattr(state, "last_hold_bump_epoch", 0) or 0)
    if last == 0:
        state.last_hold_bump_epoch = now_epoch
        return True

    if (now_epoch - last) >= every_s:
        state.last_hold_bump_epoch = now_epoch
        return True

    return False


def _classify_hold_reason(
    *,
    paused: bool,
    stale: bool,
    cooldown_remaining: int,
    entry_signal_ok: bool,
    conf_ok: bool,
    conf_score: Optional[int],
    conf_min: int,
    st_1m: StrategyState,
) -> Tuple[str, str]:
    if paused:
        return "HOLD_PAUSED", "PAUSE_FILE present"
    if stale:
        return "HOLD_STALE_DATA", "stale tick guard"
    if cooldown_remaining > 0:
        return "HOLD_COOLDOWN", f"cooldown_remaining_s={cooldown_remaining}"
    if not entry_signal_ok:
        if not bool(st_1m.trend_ok):
            return "HOLD_NO_TREND_OK", f"trend_ok=0 score={st_1m.score} reasons={st_1m.reasons}"
        if int(st_1m.signal) != 1:
            return "HOLD_NO_SIGNAL", f"signal={st_1m.signal} score={st_1m.score} reasons={st_1m.reasons}"
        return "HOLD_ENTRY_SIGNAL_FALSE", f"trend_ok={int(st_1m.trend_ok)} signal={st_1m.signal}"
    if not conf_ok:
        return "HOLD_CONFLUENCE", f"conf_score={safe_str(conf_score)} min={conf_min}"
    return "HOLD_OTHER", f"trend_ok={int(st_1m.trend_ok)} signal={st_1m.signal} conf={safe_str(conf_score)}"


def _map_trade_event(cfg: dict, would_name: str) -> str:
    if not _bool_cfg(cfg, "USE_SHOULD_EVENTS", False):
        return would_name
    if would_name == "WOULD_BUY":
        return "SHOULD_BUY"
    if would_name == "WOULD_SELL":
        return "SHOULD_SELL"
    return would_name


def _compute_volatility_fraction(state, cfg: dict) -> Tuple[Optional[Decimal], str]:
    try:
        strat = getattr(state, "strat_1m", None)
        ind = getattr(strat, "ind", None)
        if ind is None:
            return None, "NO_INDICATOR_ENGINE"

        win = int(cfg.get("VOL_LOOKBACK", cfg.get("VOL_WINDOW", 20)))
        win = max(5, min(500, win))

        vol = ind.volatility(win)
        if vol is None:
            return None, "VOL_NONE"

        vol = _as_decimal(vol, "0")
        if vol <= 0:
            return None, "VOL_NONPOS"

        return vol, "VOL_OK"
    except Exception as e:
        return None, f"VOL_ERR:{e}"


def _apply_phase4_vol_sizing(
    *,
    state,
    px: Decimal,
    qty_cap: Decimal,
    cfg: dict,
) -> Tuple[Decimal, str, Optional[Decimal], Optional[Decimal]]:
    use_vol = _bool_cfg(cfg, "USE_VOL_SIZING", False)
    if not use_vol:
        return qty_cap, "VOL_SIZING_DISABLED", None, None

    if not hasattr(state, "risk") or state.risk is None:
        return qty_cap, "NO_RISK_MANAGER", None, None

    if not hasattr(state.risk, "size_by_vol"):
        return qty_cap, "RISK_NO_SIZE_BY_VOL", None, None

    vol, vol_reason = _compute_volatility_fraction(state, cfg)
    if vol is None:
        fallback = _bool_cfg(cfg, "VOL_FALLBACK_TO_CAPS", True)
        if fallback:
            return qty_cap, f"{vol_reason}|fallback_caps", None, None
        return Decimal("0"), vol_reason, None, None

    qty_vol, why = state.risk.size_by_vol(px, vol, cfg)
    qty_vol_d = _as_decimal(qty_vol, "0")
    if qty_vol_d <= 0:
        fallback = _bool_cfg(cfg, "VOL_FALLBACK_TO_CAPS", True)
        if fallback:
            return qty_cap, f"{why}|fallback_caps", vol, qty_vol_d
        return Decimal("0"), why, vol, qty_vol_d

    final_qty = min(qty_cap, qty_vol_d)
    return final_qty, f"VOL_APPLIED vol={vol} ({why})", vol, qty_vol_d


def _ensure_state_has_regime_inputs(st_1m: StrategyState, vol: Optional[Decimal]):
    if vol is None:
        return
    try:
        setattr(st_1m, "volatility", vol)
    except Exception:
        pass


def _compute_structure(
    state,
    *,
    px: Decimal,
    closed_1m,
    cfg: dict,
) -> Optional[StructureResult]:
    if not _bool_cfg(cfg, "USE_STRUCTURE", False):
        return None

    se = getattr(state, "structure_engine", None)
    if se is None:
        return None

    try:
        if closed_1m is not None:
            state.last_closed_candle_1m = closed_1m
            se.update_on_close(closed_1m)

        last_closed = getattr(state, "last_closed_candle_1m", None)
        return se.evaluate(px, last_closed)
    except Exception:
        return None


def _compute_trendlines(
    state,
    *,
    px: Decimal,
    closed_1h,
    cfg: dict,
) -> Optional[TrendlineResult]:
    if not _bool_cfg(cfg, "USE_TRENDLINES", False):
        return None

    te = getattr(state, "trendline_engine", None)
    if te is None:
        return None

    try:
        if closed_1h is not None:
            state.last_closed_candle_1h = closed_1h
            te.update_on_close(closed_1h)

        last_closed = getattr(state, "last_closed_candle_1h", None)
        return te.evaluate(px, last_closed)
    except Exception:
        return None


def _compute_liquidity(
    state,
    *,
    tick,
    px: Decimal,
    closed_1m,
    cfg: dict,
    atr_norm_fallback: Optional[Decimal] = None,
) -> Optional[Any]:
    if not (_bool_cfg(cfg, "USE_LIQUIDITY_FILTERS", False) or _bool_cfg(cfg, "USE_LIQUIDITY", False)):
        return None

    le = getattr(state, "liquidity_engine", None)
    if le is None:
        return None

    try:
        if closed_1m is not None:
            v = getattr(closed_1m, "volume", None)
            if v is None:
                v = getattr(tick, "vol_1m", None)
            if v is not None:
                le.update_on_1m_close(v)

        bid = getattr(tick, "bid", None)
        ask = getattr(tick, "ask", None)

        atr_norm = getattr(tick, "atr_norm", None)
        if atr_norm is None:
            atr_norm = atr_norm_fallback

        vol_1m = getattr(tick, "vol_1m", None)
        if vol_1m is None and closed_1m is not None:
            vol_1m = getattr(closed_1m, "volume", None)
        if vol_1m is None:
            vol_1m = getattr(state, "last_candle_volume_1m", None)

        return le.evaluate(bid=bid, ask=ask, vol_1m=vol_1m, atr_norm=atr_norm)
    except Exception:
        return None


def _apply_liquidity_overlay_to_confluence(
    *,
    eff_score: Optional[int],
    eff_gate: str,
    eff_reason: str,
    liq: Optional[Any],
) -> Tuple[Optional[int], str, str, List[str]]:
    if liq is None:
        return eff_score, eff_gate, eff_reason, []

    mode = str(getattr(liq, "mode", "") or "").upper()
    ok = bool(getattr(liq, "ok", True))
    reasons = str(getattr(liq, "reasons", "") or "")
    penalty = getattr(liq, "penalty_points", None)

    hard_blocks: List[str] = []

    if reasons:
        eff_reason = (eff_reason + " | " if eff_reason else "") + f"liq:{reasons}"
    else:
        eff_reason = (eff_reason + " | " if eff_reason else "") + "liq:OK"

    # PENALIZE mode: apply penalty to score regardless of ok status,
    # then return without hard-blocking (ok=True means trade is allowed, just penalized)
    if mode == "PENALIZE":
        if eff_score is not None and penalty is not None:
            try:
                p = int(penalty)
            except Exception:
                p = 0
            if p > 0:
                eff_score = max(0, min(100, int(eff_score) - p))
                eff_reason = eff_reason + f" | liq_penalty=-{p}"
        return eff_score, eff_gate, eff_reason, []

    if ok:
        return eff_score, eff_gate, eff_reason, []

    if mode == "BLOCK":
        hard_blocks.append("BLOCK:LIQUIDITY")
        return eff_score, eff_gate, eff_reason, hard_blocks

    hard_blocks.append("BLOCK:LIQUIDITY_MODE_UNKNOWN")
    return eff_score, eff_gate, eff_reason, hard_blocks


def _gate_from_score(eff_score: Optional[int], min_score: int) -> str:
    if eff_score is None:
        return "NONE"
    try:
        s = int(eff_score)
    except Exception:
        return "NONE"
    return "TRADE" if s >= int(min_score) else "WATCH"


def _argus_profile(cfg: dict) -> Optional[Dict[str, Any]]:
    p = cfg.get("ARGUS_PROFILE")
    return p if isinstance(p, dict) and p else None


def _argus_rng(cfg: dict, profile: Dict[str, Any]) -> random.Random:
    seed = cfg.get("ARGUS_SEED", None)
    if seed is None:
        seed = profile.get("seed", 1337)
    try:
        seed_i = int(seed)
    except Exception:
        seed_i = 1337
    return random.Random(seed_i)


def _argus_flip_signal(sig: int) -> int:
    if sig == 1:
        return 0
    if sig == 0:
        return 1
    return sig


def _argus_damage_tfstate(
    st: Optional[TFState],
    *,
    tf: str,
    profile: Dict[str, Any],
    rng: random.Random,
) -> Optional[TFState]:
    if st is None:
        return None

    tf_dropout = profile.get("tf_dropout")
    if isinstance(tf_dropout, str):
        tf_dropout = [tf_dropout]
    if isinstance(tf_dropout, list) and tf in [str(x) for x in tf_dropout]:
        return None

    out = TFState(
        tf=st.tf,
        signal=int(st.signal),
        trend_ok=bool(st.trend_ok),
        score=int(st.score),
        reasons=str(st.reasons or ""),
    )

    flip_pct = profile.get("signal_flip_pct", 0.0)
    try:
        flip_p = float(flip_pct)
    except Exception:
        flip_p = 0.0
    if flip_p > 0 and rng.random() < flip_p:
        out.signal = _argus_flip_signal(int(out.signal))
        out.reasons = (out.reasons + " | " if out.reasons else "") + "ARGUS:signal_flip"

    bump = profile.get("fake_score_bump", 0)
    try:
        bump_i = int(bump)
    except Exception:
        bump_i = 0
    if bump_i != 0:
        out.score = int(max(0, min(100, out.score + bump_i)))
        out.reasons = (out.reasons + " | " if out.reasons else "") + f"ARGUS:score_bump={bump_i}"

    noise = profile.get("score_noise", 0)
    try:
        noise_i = int(noise)
    except Exception:
        noise_i = 0
    if noise_i > 0:
        n = rng.randint(-noise_i, +noise_i)
        if n != 0:
            out.score = int(max(0, min(100, out.score + n)))
            out.reasons = (out.reasons + " | " if out.reasons else "") + f"ARGUS:score_noise={n:+d}"

    return out


def _argus_apply_feature_damage(
    *,
    cfg: dict,
    snap: DecisionSnapshot,
    st_1m_tf: Optional[TFState],
    st_5m_tf: Optional[TFState],
    st_1h_tf: Optional[TFState],
) -> Tuple[Optional[TFState], Optional[TFState], Optional[TFState], Optional[str], bool]:
    profile = _argus_profile(cfg)
    if profile is None:
        return st_1m_tf, st_5m_tf, st_1h_tf, None, False

    rng = _argus_rng(cfg, profile)

    try:
        snap.argus_profile = str(profile.get("name", "argus"))
    except Exception:
        pass

    st_1m_tf2 = _argus_damage_tfstate(st_1m_tf, tf="1m", profile=profile, rng=rng)
    st_5m_tf2 = _argus_damage_tfstate(st_5m_tf, tf="5m", profile=profile, rng=rng)
    st_1h_tf2 = _argus_damage_tfstate(st_1h_tf, tf="1h", profile=profile, rng=rng)

    force_regime = profile.get("force_regime")
    forced_regime_label: Optional[str] = None
    if force_regime is not None and str(force_regime).strip() != "":
        forced_regime_label = str(force_regime).strip().upper()
        try:
            snap.argus_forced_regime = forced_regime_label
        except Exception:
            pass

    force_liq_block = bool(profile.get("force_liq_block", False))
    if force_liq_block:
        try:
            snap.argus_forced_liq_block = True
        except Exception:
            pass

    return st_1m_tf2, st_5m_tf2, st_1h_tf2, forced_regime_label, force_liq_block


def _verify_tf_snapshots(
    snap: DecisionSnapshot,
    *,
    st_1m_tf: Optional[TFState],
    st_5m_tf: Optional[TFState],
    st_1h_tf: Optional[TFState],
    cfg: dict,
) -> None:
    if not _bool_cfg(cfg, "VERIFY_TF_SNAPSHOTS", True):
        return

    parts: List[str] = []
    parts.append(f"1m={'OK' if st_1m_tf is not None else 'NONE'}")
    parts.append(f"5m={'OK' if st_5m_tf is not None else 'NONE'}")
    parts.append(f"1h={'OK' if st_1h_tf is not None else 'NONE'}")

    if st_1m_tf is not None:
        parts.append(
            f"1m_sig={int(st_1m_tf.signal)} 1m_trend={int(bool(st_1m_tf.trend_ok))} 1m_score={int(st_1m_tf.score)}"
        )
        if int(st_1m_tf.signal) == 0 and (not bool(st_1m_tf.trend_ok)) and int(st_1m_tf.score) == 0:
            parts.append("WARN:1M_DEGENERATE_ALL_ZERO")

    if st_5m_tf is not None:
        parts.append(
            f"5m_sig={int(st_5m_tf.signal)} 5m_trend={int(bool(st_5m_tf.trend_ok))} 5m_score={int(st_5m_tf.score)}"
        )
    if st_1h_tf is not None:
        parts.append(
            f"1h_sig={int(st_1h_tf.signal)} 1h_trend={int(bool(st_1h_tf.trend_ok))} 1h_score={int(st_1h_tf.score)}"
        )

    _add_event(snap, "TF_SNAPSHOT_VERIFY", " | ".join(parts))


def _force_test_override(
    *,
    state,
    tick,
    cfg: dict,
    snap: DecisionSnapshot,
    px: Decimal,
    now_e: int,
    emit_actions: bool,
) -> bool:
    """
    Deterministic execution smoke-test override.

    Must run BEFORE warmup / trend / confluence / liquidity HOLD returns.
    Returns True if snap was mutated into an actionable forced BUY/SELL.
    """
    if not emit_actions:
        return False

    force_test_buy = _bool_cfg(cfg, "FORCE_TEST_BUY", False)
    force_test_sell = _bool_cfg(cfg, "FORCE_TEST_SELL", False)
    force_test_only_once = _bool_cfg(cfg, "FORCE_TEST_ONLY_ONCE", True)

    ledger_qty = _as_decimal(getattr(state.ledger, "position_qty", None), "0")
    in_position = ledger_qty > 0

    if force_test_buy and (not in_position):
        already_done = bool(getattr(state, "_force_test_buy_done", False))
        if (not force_test_only_once) or (not already_done):
            usd_per_trade = _as_decimal(cfg.get("USD_PER_TRADE", "0"), "0")
            min_order_usd = _as_decimal(cfg.get("MIN_ORDER_USD", "0"), "0")

            qty = Decimal("0")
            if px > 0 and usd_per_trade > 0:
                qty = usd_per_trade / px

            if px > 0 and qty > 0 and (qty * px) >= min_order_usd:
                state._force_test_buy_done = True

                snap.intent_id = _new_intent_id(state.symbol, "BUY", now_e)
                snap.entry_intent_id = snap.intent_id
                snap.client_order_id = _new_client_order_id(state.symbol, "BUY", now_e)

                snap.execution_qty = qty
                snap.vol_sizing_qty = qty
                snap.vol_used = None
                snap.vol_reason = "FORCE_TEST_BUY"
                snap.sizing_note = "FORCE_TEST_BUY"

                snap.action = "BUY"
                snap.action_reason = "FORCE_TEST_BUY"
                snap.execution_status = "PENDING_SUBMIT"
                snap.execution_reason = "FORCE_TEST_BUY"
                snap.risk_blocked_reason = ""
                snap.next_poll_s = float(cfg.get("POLL_FAST_SECONDS", 2.0))

                _add_event(
                    snap,
                    _map_trade_event(cfg, "WOULD_BUY"),
                    (
                        f"ENTRY_INTENT | FORCE_TEST_BUY | px={px} qty={qty} "
                        f"client_order_id={snap.client_order_id}"
                    ),
                    notify_title="BUY",
                    notify_body=(
                        f"{state.symbol} BUY intent\n"
                        f"reason=FORCE_TEST_BUY\n"
                        f"px={px}\nqty={qty}\n"
                        f"client_order_id={snap.client_order_id}"
                    ),
                    client_order_id=snap.client_order_id,
                )
                return True

    if force_test_sell and in_position:
        already_done = bool(getattr(state, "_force_test_sell_done", False))
        if (not force_test_only_once) or (not already_done):
            if ledger_qty > 0:
                state._force_test_sell_done = True

                snap.intent_id = _new_intent_id(state.symbol, "SELL", now_e)
                snap.exit_intent_id = snap.intent_id
                snap.client_order_id = _new_client_order_id(state.symbol, "SELL", now_e)

                snap.execution_qty = ledger_qty
                snap.vol_sizing_qty = ledger_qty
                snap.vol_used = None
                snap.vol_reason = "FORCE_TEST_SELL"
                snap.sizing_note = "FORCE_TEST_SELL"

                snap.action = "SELL"
                snap.action_reason = "FORCE_TEST_SELL"
                snap.execution_status = "PENDING_SUBMIT"
                snap.execution_reason = "FORCE_TEST_SELL"
                snap.risk_blocked_reason = ""
                snap.next_poll_s = float(cfg.get("POLL_FAST_SECONDS", 2.0))

                _add_event(
                    snap,
                    _map_trade_event(cfg, "WOULD_SELL"),
                    (
                        f"EXIT_INTENT | FORCE_TEST_SELL | px={px} qty={ledger_qty} "
                        f"client_order_id={snap.client_order_id}"
                    ),
                    notify_title="SELL",
                    notify_body=(
                        f"{state.symbol} SELL intent\n"
                        f"reason=FORCE_TEST_SELL\n"
                        f"px={px}\nqty={ledger_qty}\n"
                        f"client_order_id={snap.client_order_id}"
                    ),
                    client_order_id=snap.client_order_id,
                )
                return True

    return False


def step(state, tick, cfg: dict, *, paused: bool, http=None) -> DecisionSnapshot:
    if getattr(state, "cooldown_until_epoch", None) is None:
        state.cooldown_until_epoch = 0
    if getattr(state, "trend_below_count", None) is None:
        state.trend_below_count = 0
    if getattr(state, "last_good_tick_epoch", None) is None:
        state.last_good_tick_epoch = None
    if getattr(state, "stale_logged", None) is None:
        state.stale_logged = False
    if getattr(state, "peak_price", None) is None:
        state.peak_price = None
    if getattr(state, "entry_epoch", None) is None:
        state.entry_epoch = None
    if getattr(state, "mfe_pct", None) is None:
        state.mfe_pct = Decimal("0")
    if getattr(state, "mae_pct", None) is None:
        state.mae_pct = Decimal("0")
    if getattr(state, "high_since_entry", None) is None:
        state.high_since_entry = None
    if getattr(state, "low_since_entry", None) is None:
        state.low_since_entry = None
    if getattr(state, "last_candle_volume_1m", None) is None:
        state.last_candle_volume_1m = None

    symbol = state.symbol
    exec_mode = _execution_mode(cfg, state)

    raw_px = getattr(tick, "px", None)
    now_e = int(getattr(tick, "epoch", 0))
    now_e = _normalize_epoch_seconds(now_e)

    if raw_px is None or str(raw_px).strip() == "":
        snap = DecisionSnapshot(symbol=symbol, ts=getattr(tick, "ts", ""), epoch=now_e, px=Decimal("0"), paused=paused)
        snap.stale = True
        snap.execution_mode = exec_mode

        sess = classify_session(now_e, cfg)
        snap.session = sess.session
        snap.session_labels = sess.labels
        snap.session_bonus = int(sess.bonus_points)
        snap.session_risk_mult = sess.risk_mult
        snap.session_reasons = sess.reason

        snap.action = "HOLD"
        snap.action_reason = "NO_PX"
        snap.cash_usd = state.ledger.cash_usd
        snap.position_qty = state.ledger.position_qty
        snap.avg_entry_px = state.ledger.avg_entry_px
        snap.equity_usd = state.ledger.equity_usd(Decimal("0"))
        snap.exposure_usd = Decimal("0")
        snap.unrl_pnl_usd = Decimal("0")
        snap.realized_pnl_usd = state.ledger.realized_pnl_usd
        snap.trades_today = getattr(state.risk, "trades_today", 0)
        snap.daily_realized_usd = getattr(state.risk, "daily_realized_pnl_usd", Decimal("0"))
        snap.lockout_until_epoch = int(getattr(state.risk, "lockout_until_epoch", 0) or 0)
        snap.cooldown_remaining_s = max(0, int(state.cooldown_until_epoch) - now_e)
        snap.next_poll_s = float(cfg["POLL_FAST_SECONDS"])
        return snap

    px = _as_decimal(raw_px, "0")

    _, closed_1m = state.candles_1m.on_tick(tick)
    _, closed_5m = state.candles_5m.on_tick(tick)
    _, closed_1h = state.candles_1h.on_tick(tick)
    _, closed_4h = state.candles_4h.on_tick(tick)

    if closed_1m is not None:
        state.last_candle_close_1m = closed_1m.close
        state.last_candle_start_1m = closed_1m.start_epoch

        v_close = getattr(closed_1m, "volume", None)
        if v_close is None:
            v_close = getattr(tick, "vol_1m", None)
        state.last_candle_volume_1m = v_close

        state.last_st_1m = state.strat_1m.on_candle_close(closed_1m.close)

    if closed_5m is not None:
        state.last_candle_close_5m = closed_5m.close
        state.last_candle_start_5m = closed_5m.start_epoch
        state.last_st_5m = state.strat_5m.on_candle_close(closed_5m.close)

    if closed_1h is not None:
        state.last_candle_close_1h = closed_1h.close
        state.last_candle_start_1h = closed_1h.start_epoch
        state.last_st_1h = state.strat_1h.on_candle_close(closed_1h.close)

    if closed_4h is not None:
        state.last_candle_close_4h = closed_4h.close
        state.last_candle_start_4h = closed_4h.start_epoch
        # Keep rolling buffer of last 50 closed 4h candles (covers ~8 days)
        buf = state.closed_candles_4h
        if buf is None:
            buf = []
            state.closed_candles_4h = buf
        buf.append(closed_4h)
        if len(buf) > 50:
            state.closed_candles_4h = buf[-50:]

    st_1m = state.last_st_1m
    candle_close_1m = getattr(state, "last_candle_close_1m", None)
    candle_start_1m = getattr(state, "last_candle_start_1m", None)

    sess = classify_session(now_e, cfg)

    stale = False
    backtest_mode = _bool_cfg(cfg, "BACKTEST_MODE", False)

    if backtest_mode:
        stale = False
        state.last_good_tick_epoch = now_e
        state.stale_logged = False
    else:
        if state.last_good_tick_epoch is None:
            state.last_good_tick_epoch = now_e

        if (now_e - int(state.last_good_tick_epoch)) > int(cfg["STALE_TICK_SECONDS"]):
            stale = True

        if not stale:
            state.last_good_tick_epoch = now_e
            state.stale_logged = False

    emit_actions = (not paused) and (not stale)

    snap = DecisionSnapshot(symbol=symbol, ts=tick.ts, epoch=now_e, px=px, paused=paused)
    snap.stale = stale
    snap.execution_mode = exec_mode

    snap.session = sess.session
    snap.session_labels = sess.labels
    snap.session_bonus = int(sess.bonus_points)
    snap.session_risk_mult = sess.risk_mult
    snap.session_reasons = sess.reason

    snap.cash_usd = state.ledger.cash_usd
    snap.position_qty = state.ledger.position_qty
    snap.avg_entry_px = state.ledger.avg_entry_px
    snap.equity_usd = state.ledger.equity_usd(px)
    snap.exposure_usd = state.ledger.exposure_usd(px)
    snap.unrl_pnl_usd = state.ledger.unrealized_pnl_usd(px, cfg)
    snap.realized_pnl_usd = state.ledger.realized_pnl_usd
    snap.trades_today = getattr(state.risk, "trades_today", 0)
    snap.daily_realized_usd = getattr(state.risk, "daily_realized_pnl_usd", Decimal("0"))
    snap.lockout_until_epoch = int(getattr(state.risk, "lockout_until_epoch", 0) or 0)
    snap.cooldown_remaining_s = max(0, int(state.cooldown_until_epoch) - now_e)

    if _force_test_override(
        state=state,
        tick=tick,
        cfg=cfg,
        snap=snap,
        px=px,
        now_e=now_e,
        emit_actions=emit_actions,
    ):
        return snap

    if st_1m is None or candle_close_1m is None or candle_start_1m is None:
        snap.action = "HOLD"
        snap.action_reason = "WARMUP_NO_1M_STATE"
        snap.next_poll_s = float(cfg["POLL_FAST_SECONDS"])
        return snap

    snap.candle_start_1m = candle_start_1m
    snap.candle_close_1m = candle_close_1m
    snap.candle_volume_1m = getattr(state, "last_candle_volume_1m", None)
    snap.ma_fast = getattr(st_1m, "ma_fast", None)
    snap.ma_slow = getattr(st_1m, "ma_slow", None)
    snap.ma50 = getattr(st_1m, "ma50", None)
    snap.ma200 = getattr(st_1m, "ma200", None)
    snap.reasons_1m = str(getattr(st_1m, "reasons", "") or "")

    vol, _ = _compute_volatility_fraction(state, cfg)
    snap.vol = vol
    _ensure_state_has_regime_inputs(st_1m, vol)

    try:
        if getattr(state, "regime_engine", None) is not None:
            rr = state.regime_engine.evaluate(st_1m)
            if rr is not None:
                state.regime = rr
    except Exception as e:
        _add_event(snap, "REGIME_EVAL_FAIL", str(e))

    if getattr(state, "regime", None) is not None:
        snap.regime = str(getattr(state.regime, "regime", "UNKNOWN"))
        snap.trend_strength = _as_decimal(getattr(state.regime, "trend_strength", None), "0")
        try:
            snap.vol = _as_decimal(getattr(state.regime, "vol", None), str(snap.vol or "0"))
        except Exception:
            pass
    else:
        snap.regime = "UNKNOWN"
        snap.trend_strength = None

    # BTC momentum gate: publish BTC trend state for other coins to read
    if "BTC" in symbol.upper() and state.regime is not None:
        try:
            write_btc_trend_state(
                cfg,
                regime=state.regime.regime,
                trend_strength=float(state.regime.trend_strength),
                vol=float(state.regime.vol),
            )
        except Exception:
            pass

    structure: Optional[StructureResult] = _compute_structure(
        state,
        px=px,
        closed_1m=closed_1m,
        cfg=cfg,
    )
    if structure is not None:
        snap.nearest_support = structure.nearest_support
        snap.nearest_resistance = structure.nearest_resistance
        snap.dist_support = structure.dist_support
        snap.dist_resistance = structure.dist_resistance

        snap.near_support = bool(structure.near_support)
        snap.near_resistance = bool(structure.near_resistance)

        snap.broke_up = bool(structure.broke_up)
        snap.broke_down = bool(structure.broke_down)
        snap.retest_ok = bool(structure.retest_ok)
        snap.failed_retest = bool(structure.failed_retest)

        snap.rejection_at_res = bool(structure.rejection_at_res)
        snap.rejection_at_sup = bool(structure.rejection_at_sup)

        snap.structure_reasons = str(structure.reasons or "")

    trendlines: Optional[TrendlineResult] = _compute_trendlines(
        state,
        px=px,
        closed_1h=closed_1h,
        cfg=cfg,
    )
    if trendlines is not None:
        snap.trendline_reasons = str(trendlines.reasons or "")
        snap.near_support_tl = bool(trendlines.near_support_tl)
        snap.near_resist_tl = bool(trendlines.near_resist_tl)
        snap.broke_above_resist_tl = bool(trendlines.broke_above_resist)
        snap.broke_below_support_tl = bool(trendlines.broke_below_support)
        snap.tl_proj_support = trendlines.proj_support
        snap.tl_proj_resist = trendlines.proj_resist

    atr_norm_fb: Optional[Decimal] = None
    try:
        if getattr(tick, "atr_norm", None) is not None:
            atr_norm_fb = _as_decimal(getattr(tick, "atr_norm", None), "0")
    except Exception:
        atr_norm_fb = None
    if atr_norm_fb is None:
        try:
            if getattr(state, "regime", None) is not None and getattr(state.regime, "vol", None) is not None:
                atr_norm_fb = _as_decimal(getattr(state.regime, "vol", None), "0")
        except Exception:
            atr_norm_fb = None
    if atr_norm_fb is None:
        atr_norm_fb = vol

    if atr_norm_fb is None:
        try:
            atrn = getattr(st_1m, "atr_norm", None)
            if atrn is not None:
                atr_norm_fb = _as_decimal(atrn, "0")
        except Exception:
            pass

    if atr_norm_fb is None:
        try:
            atr = getattr(st_1m, "atr", None)
            if atr is not None and px > 0:
                atr_norm_fb = _as_decimal(atr, "0") / px
        except Exception:
            pass

    liq = _compute_liquidity(
        state,
        tick=tick,
        px=px,
        closed_1m=closed_1m,
        cfg=cfg,
        atr_norm_fallback=atr_norm_fb,
    )

    if liq is None:
        try:
            entry_signal_ok_dbg = bool(st_1m.trend_ok and st_1m.signal == 1)
        except Exception:
            entry_signal_ok_dbg = False

        bid_dbg = getattr(tick, "bid", None)
        ask_dbg = getattr(tick, "ask", None)
        vol_dbg = getattr(tick, "vol_1m", None)

        if entry_signal_ok_dbg and bid_dbg is not None and ask_dbg is not None:
            le_dbg = getattr(state, "liquidity_engine", None)
            _add_event(
                snap,
                "LIQ_DEBUG_MISSING",
                (
                    f"liq=None | USE_LIQUIDITY_FILTERS={int(_bool_cfg(cfg,'USE_LIQUIDITY_FILTERS',False))} "
                    f"USE_LIQUIDITY={int(_bool_cfg(cfg,'USE_LIQUIDITY',False))} "
                    f"has_liquidity_engine={int(le_dbg is not None)} "
                    f"tick_bid={safe_str(bid_dbg)} tick_ask={safe_str(ask_dbg)} tick_vol_1m={safe_str(vol_dbg)} "
                    f"atr_norm_fb={safe_str(atr_norm_fb)}"
                ),
            )

    if liq is not None:
        snap.liq_ok = bool(getattr(liq, "ok", True))
        snap.liq_spread_bps = getattr(liq, "spread_bps", None)
        snap.liq_vol_1m = getattr(liq, "vol_1m", None)

        snap.liq_vol_baseline = getattr(liq, "vol_baseline", None)
        if snap.liq_vol_baseline is None:
            try:
                le = getattr(state, "liquidity_engine", None)
                vb = getattr(le, "vol_baseline", None)
                if callable(vb):
                    snap.liq_vol_baseline = vb()
                else:
                    snap.liq_vol_baseline = vb
                if snap.liq_vol_baseline is None and le is not None:
                    snap.liq_vol_baseline = getattr(le, "_vol_baseline", None)
                if snap.liq_vol_baseline is None and le is not None:
                    snap.liq_vol_baseline = getattr(le, "baseline", None)
            except Exception:
                pass

        snap.liq_atr_norm = getattr(liq, "atr_norm", None)
        if snap.liq_atr_norm is None:
            snap.liq_atr_norm = atr_norm_fb

        snap.liq_mode = str(getattr(liq, "mode", "") or "")
        snap.liq_penalty_points = int(getattr(liq, "penalty_points", 0) or 0)
        snap.liq_reasons = str(getattr(liq, "reasons", "") or "")

    payload = state.risk.ensure_day(now_e, cfg)
    if payload and not payload.get("auto_synced") and not payload.get("ignored_rewind"):
        old_day = payload.get("old_day", "?")
        old_trades = payload.get("old_trades_today", 0)
        old_pnl = payload.get("old_daily_realized", "0")
        try:
            pnl_f = float(old_pnl)
            pnl_sign = "+" if pnl_f >= 0 else ""
            notify_body = (
                f"{snap.symbol} | Day: {old_day} | Trades: {old_trades} | "
                f"Daily P&L: {pnl_sign}${pnl_f:.2f}"
            )
        except Exception:
            notify_body = payload["msg"]
        _add_event(
            snap, "RISK_DAY_RESET", payload["msg"],
            notify_title="Daily Reset",
            notify_body=notify_body,
        )
    elif payload:
        _add_event(snap, "RISK_DAY_RESET", payload["msg"])

    cooldown_remaining = max(0, int(state.cooldown_until_epoch) - now_e)

    st_1m_tf = _to_tf_state(state.last_st_1m, "1m")
    st_5m_tf = _to_tf_state(state.last_st_5m, "5m")
    st_1h_tf = _to_tf_state(state.last_st_1h, "1h")

    _verify_tf_snapshots(snap, st_1m_tf=st_1m_tf, st_5m_tf=st_5m_tf, st_1h_tf=st_1h_tf, cfg=cfg)

    st_1m_tf, st_5m_tf, st_1h_tf, forced_regime_label, forced_liq_block = _argus_apply_feature_damage(
        cfg=cfg,
        snap=snap,
        st_1m_tf=st_1m_tf,
        st_5m_tf=st_5m_tf,
        st_1h_tf=st_1h_tf,
    )

    if forced_regime_label:
        snap.regime = forced_regime_label

    base_conf = None
    try:
        base_conf = state.confluence.evaluate(
            st_1m=st_1m_tf,
            st_5m=st_5m_tf,
            st_1h=st_1h_tf,
            structure=structure,
            trendlines=trendlines,
        )
        state.last_conf = base_conf
    except Exception as e:
        state.last_conf = None
        _add_event(snap, "CONFLUENCE_EVAL_FAIL", str(e))

    require_confluence = _bool_cfg(cfg, "REQUIRE_CONFLUENCE", True)
    confluence_min_score = int(cfg.get("CONFLUENCE_MIN_SCORE", cfg.get("CONFLUENCE_TRADE_SCORE", 80)))

    base_score: Optional[int] = None
    base_gate: str = ""
    base_reasons: str = ""
    if base_conf is not None:
        base_score = int(getattr(base_conf, "confluence_score", 0) or 0)
        base_gate = str(getattr(base_conf, "gate", "") or "")
        base_reasons = str(getattr(base_conf, "reasons", "") or "")

    use_adaptive = _bool_cfg(cfg, "USE_ADAPTIVE_CONFLUENCE", False)
    eff_score: Optional[int] = base_score
    eff_gate: str = base_gate
    eff_reason: str = base_reasons

    if require_confluence:
        if base_conf is None:
            eff_score = None
            eff_gate = "NONE"
            eff_reason = "NO_CONFLUENCE_SNAPSHOT"
        else:
            if use_adaptive and getattr(state, "adaptive_confluence", None) is not None:
                try:
                    adj = state.adaptive_confluence.adjust(
                        base_conf,
                        regime=getattr(state, "regime", None),
                        trend_strength=snap.trend_strength,
                        vol=snap.vol,
                    )

                    snap.ac_base_score = int(getattr(adj, "base_score", base_score or 0))
                    snap.ac_base_gate = str(getattr(adj, "base_gate", base_gate))
                    snap.ac_adjusted_score = int(getattr(adj, "adjusted_score", base_score or 0))
                    snap.ac_adjusted_gate = str(getattr(adj, "adjusted_gate", base_gate))
                    snap.ac_delta = int(getattr(adj, "score_delta", 0))
                    snap.ac_reason = str(getattr(adj, "reason", ""))

                    eff_score = int(snap.ac_adjusted_score)
                    eff_gate = str(snap.ac_adjusted_gate)
                    eff_reason = str(snap.ac_reason)
                except Exception as e:
                    _add_event(snap, "ADAPTIVE_CONFLUENCE_FAIL", str(e))
                    eff_score = base_score
                    eff_gate = base_gate
                    eff_reason = base_reasons
            else:
                snap.ac_base_score = base_score
                snap.ac_base_gate = base_gate
                snap.ac_adjusted_score = base_score
                snap.ac_adjusted_gate = base_gate
                snap.ac_delta = 0
                snap.ac_reason = "ADAPTIVE_DISABLED"

    if _bool_cfg(cfg, "USE_SESSION_MODIFIERS", False):
        before = eff_score
        eff_score = apply_session_to_score(score=eff_score, session_info=sess, cfg=cfg)
        if before is not None and eff_score is not None and eff_score != before:
            delta = int(eff_score) - int(before)
            sign = "+" if delta >= 0 else ""
            eff_reason = (
                (eff_reason + " | " if eff_reason else "")
                + f"session_bonus={sign}{delta} ({sess.session}:{sess.labels})"
            )

    eff_score, eff_gate, eff_reason, liq_blocks = _apply_liquidity_overlay_to_confluence(
        eff_score=eff_score,
        eff_gate=eff_gate,
        eff_reason=eff_reason,
        liq=liq,
    )

    if forced_liq_block:
        liq_blocks = list(liq_blocks) + ["BLOCK:ARGUS_FORCED_LIQ"]
        eff_reason = (eff_reason + " | " if eff_reason else "") + "liq:ARGUS_FORCED_BLOCK"

    if require_confluence and eff_gate not in ("NONE", "BLOCK"):
        eff_gate = _gate_from_score(eff_score, confluence_min_score)

    snap.confluence_score = None if eff_score is None else int(eff_score)
    snap.confluence_gate = str(eff_gate or "")
    snap.confluence_reasons = str(eff_reason or "")

    conf_gate = str(snap.confluence_gate or "")
    conf_present = conf_gate not in ("", "NONE")
    conf_blocked = conf_gate in ("BLOCK",)

    conf_size_mult = _as_decimal(cfg.get("CONFLUENCE_SIZE_MULT_WHEN_NOT_TRADE", "0.25"), "0.25")
    conf_veto = bool(require_confluence and (not conf_present or conf_blocked))

    conf_ok = (not conf_veto)
    if liq_blocks:
        conf_ok = False

    if closed_1m is not None and state.last_st_1m is not None:
        exit_level = ma200_exit_level(
            state.last_st_1m.ma200,
            _as_decimal(cfg.get("MA200_BAND_PCT", "0.001"), "0.001"),
        )
        if state.ledger.in_pos() and exit_level is not None:
            if closed_1m.close < exit_level:
                state.trend_below_count += 1
            else:
                state.trend_below_count = 0
        else:
            state.trend_below_count = 0

    equity = state.ledger.equity_usd(px)
    exposure = state.ledger.exposure_usd(px)
    unrl_pnl = state.ledger.unrealized_pnl_usd(px, cfg)
    realized_pnl = state.ledger.realized_pnl_usd

    # Drawdown circuit breaker — update rolling equity tracker
    _dd_reason = state.risk.update_equity(equity, now_e, cfg)
    if _dd_reason:
        _add_event(
            snap, "DRAWDOWN_CIRCUIT_BREAKER", _dd_reason,
            notify_title="DRAWDOWN BREACH",
            notify_body=_dd_reason,
        )

    take_profit = stop_loss = trail_stop = None
    hold_s = 0

    if state.ledger.in_pos() and state.ledger.avg_entry_px is not None:
        state.peak_price = px if state.peak_price is None else max(state.peak_price, px)

        take_profit_pct = _as_decimal(cfg.get("TAKE_PROFIT_PCT", "0.03"), "0.03")
        stop_loss_pct = _as_decimal(cfg.get("STOP_LOSS_PCT", "0.02"), "0.02")
        trail_stop_pct = _as_decimal(cfg.get("TRAIL_STOP_PCT", "0.015"), "0.015")

        # Dynamic regime exits: adjust TP/SL/hold based on current regime
        if _bool_cfg(cfg, "USE_DYNAMIC_REGIME_EXITS", False) and state.regime is not None:
            _regime_key = state.regime.regime.upper()  # TREND_UP, TREND_DOWN, RANGE, VOLATILE_RANGE
            _tp_mult = _as_decimal(cfg.get(f"EXIT_TP_MULT_{_regime_key}", "1.0"), "1.0")
            _sl_mult = _as_decimal(cfg.get(f"EXIT_SL_MULT_{_regime_key}", "1.0"), "1.0")
            take_profit_pct = take_profit_pct * _tp_mult
            stop_loss_pct = stop_loss_pct * _sl_mult

        take_profit = state.ledger.avg_entry_px * (Decimal("1") + take_profit_pct)
        stop_loss = state.ledger.avg_entry_px * (Decimal("1") - stop_loss_pct)
        trail_stop = state.peak_price * (Decimal("1") - trail_stop_pct)
    else:
        state.peak_price = None
        state.entry_epoch = None

    if state.ledger.in_pos() and state.entry_epoch is not None:
        hold_s = max(0, now_e - state.entry_epoch)

    track_mfe_mae = _bool_cfg(cfg, "TRACK_MFE_MAE", True)
    if track_mfe_mae and state.ledger.in_pos() and state.ledger.avg_entry_px is not None:
        entry_px = state.ledger.avg_entry_px
        state.high_since_entry = px if state.high_since_entry is None else max(state.high_since_entry, px)
        state.low_since_entry = px if state.low_since_entry is None else min(state.low_since_entry, px)

        if entry_px > 0 and state.high_since_entry is not None:
            cur_mfe = (state.high_since_entry - entry_px) / entry_px
            if cur_mfe > state.mfe_pct:
                state.mfe_pct = cur_mfe

        if entry_px > 0 and state.low_since_entry is not None:
            cur_mae = (state.low_since_entry - entry_px) / entry_px
            if cur_mae < state.mae_pct:
                state.mae_pct = cur_mae

    dist_ma200 = pct_dist(px, st_1m.ma200)
    dist_tp = pct_dist(px, take_profit)
    dist_sl = pct_dist(px, stop_loss)
    dist_trail = pct_dist(px, trail_stop)

    dists: List[Decimal] = [d for d in [dist_ma200, dist_tp, dist_sl, dist_trail] if d is not None]
    min_dist = min(dists) if dists else None

    action = "HOLD"
    action_reason = ""
    risk_blocked_reason = ""

    if state.ledger.in_pos() and state.ledger.avg_entry_px is not None:
        would_sell_action = "WOULD_SELL"

        # --- Partial Take-Profit at 1R + Breakeven Stop ---
        use_exit_intel = _bool_cfg(cfg, "USE_EXIT_INTEL", False)
        partial_1r_frac = _as_decimal(cfg.get("EXIT_PARTIAL_1R_FRACTION", "0.50"), "0.50")
        be_offset_pct = _as_decimal(cfg.get("EXIT_BREAKEVEN_OFFSET_PCT", "0.001"), "0.001")

        if use_exit_intel and not state.partial_tp_taken and stop_loss is not None:
            stop_loss_pct_val = _as_decimal(cfg.get("STOP_LOSS_PCT", "0.02"), "0.02")
            one_r_target = state.ledger.avg_entry_px * (Decimal("1") + stop_loss_pct_val)
            if px >= one_r_target and partial_1r_frac > 0 and emit_actions:
                partial_qty = (state.ledger.position_qty * partial_1r_frac).quantize(
                    Decimal("0.00000001"), rounding=ROUND_DOWN
                )
                if partial_qty > 0 and not _adapter_mode_enabled(cfg, state):
                    eff_sell, proceeds, realized_partial = state.ledger.sell(
                        px=px, epoch=now_e, cfg=cfg, qty=partial_qty
                    )
                    state.partial_tp_taken = True
                    state.breakeven_stop = state.ledger.avg_entry_px * (Decimal("1") + be_offset_pct)

                    _add_event(
                        snap,
                        "PARTIAL_TP_1R",
                        (
                            f"PARTIAL_TP_1R | px={px} qty={partial_qty} proceeds={proceeds:.2f} "
                            f"realized={realized_partial:.2f} | breakeven_stop={state.breakeven_stop:.2f}"
                        ),
                        notify_title="PARTIAL_TP",
                        notify_body=(
                            f"{symbol} PARTIAL TP at 1R\npx={px}\nqty={partial_qty}\n"
                            f"proceeds={proceeds:.2f}\nbreakeven={state.breakeven_stop:.2f}"
                        ),
                    )
                elif partial_qty > 0 and _adapter_mode_enabled(cfg, state):
                    # In ADAPTER mode, flag partial TP as intent for runner_live
                    state.partial_tp_taken = True
                    state.breakeven_stop = state.ledger.avg_entry_px * (Decimal("1") + be_offset_pct)
                    snap.intent_id = _new_intent_id(symbol, "SELL", now_e)
                    snap.exit_intent_id = snap.intent_id
                    snap.client_order_id = _new_client_order_id(symbol, "SELL", now_e)
                    snap.execution_qty = partial_qty
                    snap.execution_status = "PENDING_SUBMIT"
                    snap.execution_reason = "PARTIAL_TP_1R"
                    _add_event(
                        snap,
                        "PARTIAL_TP_1R",
                        (
                            f"PARTIAL_TP_1R_INTENT | px={px} qty={partial_qty} "
                            f"breakeven_stop={state.breakeven_stop:.2f}"
                        ),
                        notify_title="PARTIAL_TP",
                        notify_body=(
                            f"{symbol} PARTIAL TP intent at 1R\npx={px}\nqty={partial_qty}"
                        ),
                        client_order_id=snap.client_order_id,
                    )
                    action = "SELL"
                    action_reason = "PARTIAL_TP_1R_INTENT"

        # Standalone breakeven stop: activate when price reaches trigger % above entry
        be_trigger_pct = _as_decimal(cfg.get("EXIT_BREAKEVEN_TRIGGER_PCT", "0"), "0")
        if (
            be_trigger_pct > 0
            and state.breakeven_stop is None
            and not state.partial_tp_taken
            and state.ledger.avg_entry_px is not None
            and state.high_since_entry is not None
        ):
            be_trigger_px = state.ledger.avg_entry_px * (Decimal("1") + be_trigger_pct)
            if state.high_since_entry >= be_trigger_px:
                state.breakeven_stop = state.ledger.avg_entry_px * (Decimal("1") + be_offset_pct)

        # Use breakeven stop if partial TP was taken OR standalone trigger activated
        effective_stop = stop_loss
        if state.breakeven_stop is not None:
            effective_stop = state.breakeven_stop

        if effective_stop is not None and px <= effective_stop and action == "HOLD":
            action = would_sell_action
            if state.breakeven_stop is not None:
                action_reason = f"BREAKEVEN_STOP (px={px} <= be={effective_stop})"
            else:
                action_reason = "STOP_LOSS"
        elif action == "HOLD":
            if hold_s < int(cfg["MIN_HOLD_SECONDS"]):
                action = "HOLD"
                action_reason = f"MIN_HOLD ({hold_s}s<{cfg['MIN_HOLD_SECONDS']}s)"
            else:
                max_hold = int(cfg["MAX_HOLD_SECONDS"])
                # Dynamic regime hold time adjustment
                if _bool_cfg(cfg, "USE_DYNAMIC_REGIME_EXITS", False) and state.regime is not None:
                    _hold_mult = float(_as_decimal(cfg.get(f"EXIT_HOLD_MULT_{state.regime.regime.upper()}", "1.0"), "1.0"))
                    max_hold = int(max_hold * _hold_mult)
                if max_hold > 0 and hold_s >= max_hold:
                    action = would_sell_action
                    action_reason = f"TIME_STOP ({hold_s}s>={max_hold}s)"
                elif state.trend_below_count >= int(cfg["TREND_INVALIDATION_CLOSES"]):
                    action = would_sell_action
                    action_reason = f"TREND_INVALIDATION ({state.trend_below_count} closes < MA200_band)"
                elif trail_stop is not None and px <= trail_stop:
                    action = would_sell_action
                    action_reason = "TRAIL_STOP"
                elif take_profit is not None and px >= take_profit:
                    action = would_sell_action
                    action_reason = "TAKE_PROFIT"

        if action == would_sell_action and emit_actions:
            ev_name = _map_trade_event(cfg, "WOULD_SELL")

            if _adapter_mode_enabled(cfg, state):
                snap.intent_id = _new_intent_id(symbol, "SELL", now_e)
                snap.exit_intent_id = snap.intent_id
                snap.client_order_id = _new_client_order_id(symbol, "SELL", now_e)
                snap.execution_qty = _as_decimal(state.ledger.position_qty, "0")
                snap.execution_status = "PENDING_SUBMIT"
                snap.execution_reason = action_reason

                _add_event(
                    snap,
                    ev_name,
                    (
                        f"EXIT_INTENT | reason={action_reason} px={px} qty=ALL "
                        f"conf_score={safe_str(snap.confluence_score)} gate={snap.confluence_gate} "
                        f"regime={snap.regime} struct={safe_str(getattr(snap,'structure_reasons',''))} "
                        f"liq={safe_str(getattr(snap,'liq_reasons',''))}"
                    ),
                    notify_title="SELL",
                    notify_body=(
                        f"{symbol} SELL intent\n"
                        f"reason={action_reason}\npx={px}\n"
                        f"client_order_id={snap.client_order_id}"
                    ),
                    client_order_id=snap.client_order_id,
                )

                action = "SELL"
                action_reason = "EXIT_INTENT"

            else:
                eff_sell, proceeds, realized_trade = state.ledger.sell_all(px, now_e, cfg)
                state.risk.record_exit(realized_trade, now_e, cfg)

                msg = (
                    f"{action_reason} | px={px} eff_sell={eff_sell:.2f} qty=ALL proceeds={proceeds:.2f} "
                    f"realized_trade={realized_trade:.2f} daily_realized={state.risk.daily_realized_pnl_usd:.2f} "
                    f"score={st_1m.score} {st_1m.reasons} | "
                    f"conf_score={safe_str(snap.confluence_score)} gate={snap.confluence_gate} | "
                    f"regime={snap.regime} | struct={safe_str(getattr(snap,'structure_reasons',''))} | "
                    f"liq={safe_str(getattr(snap,'liq_reasons',''))}"
                )
                _add_event(
                    snap,
                    ev_name,
                    msg,
                    notify_title="SELL",
                    notify_body=(
                        f"{symbol} | {action_reason}\npx={px}\nproceeds={proceeds:.2f}\n"
                        f"realized_trade={realized_trade:.2f}\n"
                        f"daily_realized={state.risk.daily_realized_pnl_usd:.2f}\nscore={st_1m.score} {st_1m.reasons}\n"
                        f"confluence={safe_str(snap.confluence_score)} gate={snap.confluence_gate}\n"
                        f"regime={snap.regime}\nstructure={safe_str(getattr(snap,'structure_reasons',''))}\n"
                        f"liquidity={safe_str(getattr(snap,'liq_reasons',''))}"
                    ),
                )

                action = ev_name
                # Preserve MFE/MAE for snapshot BEFORE resetting state
                _final_mfe = state.mfe_pct
                _final_mae = state.mae_pct
                state.cooldown_until_epoch = now_e + int(cfg["COOLDOWN_SECONDS"])
                state.trend_below_count = 0
                state.peak_price = None
                state.entry_epoch = None
                state.mfe_pct = Decimal("0")
                state.mae_pct = Decimal("0")
                state.high_since_entry = None
                state.low_since_entry = None
                state.partial_tp_taken = False
                state.breakeven_stop = None
                # Stash final MFE/MAE so snapshot (built later) captures pre-reset values
                state._sell_mfe_pct = _final_mfe
                state._sell_mae_pct = _final_mae

    if (not state.ledger.in_pos()) and emit_actions:
        entry_signal_ok = bool(st_1m.trend_ok and st_1m.signal == 1)

        if entry_signal_ok:
            # --- ML Governor: evaluate EARLY so every entry signal gets a prediction ---
            # This stamps snap.governor_* for ALL potential entries (even those blocked
            # by regime/liq/risk gates), enabling data collection in LOG_ONLY and
            # accurate scoring in GATE mode.  The GATE block still happens downstream.
            _gov_result = ml_governor.evaluate(snap, cfg)
            snap.governor_win_prob = _gov_result.win_prob
            snap.governor_recommendation = _gov_result.recommendation
            snap.governor_score_modifier = _gov_result.score_modifier

            _regime_block_list = [
                s.strip().upper()
                for s in str(cfg.get("REGIME_ENTRY_BLOCK_LIST", "")).split(",")
                if s.strip()
            ]
            if snap.regime in _regime_block_list:
                _emit_missed_buy(
                    snap,
                    event="MISSED_BUY_REGIME",
                    prefix="REGIME_BLOCK",
                    px=px,
                    confluence_min_score=confluence_min_score,
                    cooldown_remaining=int(cooldown_remaining),
                    equity=equity,
                    exposure=exposure,
                    extra=f"regime={safe_str(snap.regime)}",
                )

            elif snap.session and snap.session in [
                s.strip().upper()
                for s in str(cfg.get("SESSION_ENTRY_BLOCK_LIST", "")).split(",")
                if s.strip()
            ]:
                _emit_missed_buy(
                    snap,
                    event="MISSED_BUY_SESSION",
                    prefix="SESSION_BLOCK",
                    px=px,
                    confluence_min_score=confluence_min_score,
                    cooldown_remaining=int(cooldown_remaining),
                    equity=equity,
                    exposure=exposure,
                    extra=f"session={safe_str(snap.session)}",
                )

            elif cooldown_remaining > 0:
                _emit_missed_buy(
                    snap,
                    event="MISSED_BUY_COOLDOWN",
                    prefix="COOLDOWN_BLOCK",
                    px=px,
                    confluence_min_score=confluence_min_score,
                    cooldown_remaining=int(cooldown_remaining),
                    equity=equity,
                    exposure=exposure,
                    extra=f"cooldown_until_epoch={int(state.cooldown_until_epoch)}",
                )

            elif liq is not None:
                mode_u = str(getattr(liq, "mode", "") or "").upper()
                ok_b = bool(getattr(liq, "ok", True))
                if mode_u == "BLOCK" and (not ok_b):
                    _emit_missed_buy(
                        snap,
                        event="MISSED_BUY_LIQUIDITY",
                        prefix="LIQUIDITY_BLOCK",
                        px=px,
                        confluence_min_score=confluence_min_score,
                        cooldown_remaining=int(cooldown_remaining),
                        equity=equity,
                        exposure=exposure,
                    )
                else:
                    if require_confluence and (not conf_ok):
                        _emit_missed_buy(
                            snap,
                            event="MISSED_BUY_CONFLUENCE",
                            prefix="CONFLUENCE_VETO",
                            px=px,
                            confluence_min_score=confluence_min_score,
                            cooldown_remaining=int(cooldown_remaining),
                            equity=equity,
                            exposure=exposure,
                            extra=f"gate={safe_str(conf_gate)}",
                        )
                    else:
                        qty_cap, qty_reason = _compute_buy_qty_and_reason(state.ledger, px, cfg)

                        qty, sizing_note, vol_used, qty_vol = _apply_phase4_vol_sizing(
                            state=state,
                            px=px,
                            qty_cap=_as_decimal(qty_cap, "0"),
                            cfg=cfg,
                        )

                        watch_gated = False
                        if require_confluence and conf_gate != "TRADE" and qty > 0:
                            try:
                                pre_mult_qty = qty
                                qty = qty * conf_size_mult
                                if qty <= 0 < pre_mult_qty:
                                    watch_gated = True
                            except Exception:
                                pass

                        if qty <= 0:
                            ev = "MISSED_BUY_WATCH_GATED" if watch_gated else _missed_buy_event_from_qty_reason(qty_reason)
                            _emit_missed_buy(
                                snap,
                                event=ev,
                                prefix="SIZE_BLOCK",
                                px=px,
                                confluence_min_score=confluence_min_score,
                                cooldown_remaining=int(cooldown_remaining),
                                equity=equity,
                                exposure=exposure,
                                extra=f"qty_reason={safe_str(qty_reason)}{' watch_gate='+conf_gate if watch_gated else ''}",
                            )
                        else:
                            if hasattr(state.risk, "can_enter_with_context"):
                                allowed, why = state.risk.can_enter_with_context(
                                    now_epoch=now_e,
                                    cfg=cfg,
                                    spread_bps=getattr(snap, "liq_spread_bps", None),
                                    proposed_qty=qty,
                                    current_qty=state.ledger.position_qty,
                                    px=px,
                                    current_exposure_usd=exposure,
                                    stale=bool(stale),
                                    last_market_data_epoch=getattr(state, "last_good_tick_epoch", None),
                                    symbol=symbol,
                                )
                            else:
                                allowed, why = state.risk.can_enter(now_e, cfg)

                            if not allowed:
                                ev = _missed_buy_event_from_risk_reason(why)
                                lockout_until = int(getattr(state.risk, "lockout_until_epoch", 0) or 0)
                                lockout_fmt = _fmt_lockout(now_e, lockout_until)

                                _emit_missed_buy(
                                    snap,
                                    event=ev,
                                    prefix="RISK_BLOCK",
                                    px=px,
                                    confluence_min_score=confluence_min_score,
                                    cooldown_remaining=int(cooldown_remaining),
                                    equity=equity,
                                    exposure=exposure,
                                    extra=f"risk_reason={safe_str(why)} lockout_until={lockout_fmt}",
                                )
                                risk_blocked_reason = str(why or "")
                            else:
                                # ML Governor GATE check (evaluation already done above)
                                gov = _gov_result  # reuse early evaluation
                                gov_mode = str(cfg.get("ML_GOVERNOR_MODE", "LOG_ONLY")).upper()
                                if gov_mode == "GATE" and gov.recommendation == "BLOCK" and gov.model_loaded:
                                    _emit_missed_buy(
                                        snap,
                                        event="MISSED_BUY_GOVERNOR",
                                        prefix="ML_GOVERNOR_BLOCK",
                                        px=px,
                                        confluence_min_score=confluence_min_score,
                                        cooldown_remaining=int(cooldown_remaining),
                                        equity=equity,
                                        exposure=exposure,
                                        extra=f"win_prob={gov.win_prob:.3f} threshold={cfg.get('ML_GOVERNOR_THRESHOLD', '0.30')}",
                                    )
                                else:
                                    if gov_mode == "SCORE_MODIFY" and gov.model_loaded and gov.score_modifier != 0:
                                        old_score = snap.confluence_score
                                        if old_score is not None:
                                            snap.confluence_score = max(0, min(100, old_score + gov.score_modifier))

                                    # Cross-coin correlation guard
                                    cc_ok, cc_reason = can_enter_cross_coin(symbol, cfg)
                                    if not cc_ok:
                                        _emit_missed_buy(
                                            snap,
                                            event="MISSED_BUY_CROSS_COIN",
                                            prefix="CROSS_COIN_BLOCK",
                                            px=px,
                                            confluence_min_score=confluence_min_score,
                                            cooldown_remaining=int(cooldown_remaining),
                                            equity=equity,
                                            exposure=exposure,
                                            extra=f"cross_coin={cc_reason}",
                                        )
                                    else:
                                        # BTC momentum gate — block alt entries when BTC trends down
                                        btc_ok, btc_reason = check_btc_momentum(symbol, cfg)
                                        if not btc_ok:
                                            _emit_missed_buy(
                                                snap,
                                                event="MISSED_BUY_BTC_MOMENTUM",
                                                prefix="BTC_MOMENTUM_BLOCK",
                                                px=px,
                                                confluence_min_score=confluence_min_score,
                                                cooldown_remaining=int(cooldown_remaining),
                                                equity=equity,
                                                exposure=exposure,
                                                extra=f"btc_momentum={btc_reason}",
                                            )
                                        else:
                                            ev_name = _map_trade_event(cfg, "WOULD_BUY")
                                            snap.vol_used = vol_used
                                            snap.vol_sizing_qty = qty_vol
                                            snap.execution_qty = qty
                                            snap.vol_reason = qty_reason
                                            snap.sizing_note = sizing_note

                                            if _adapter_mode_enabled(cfg, state):
                                                snap.intent_id = _new_intent_id(symbol, "BUY", now_e)
                                                snap.entry_intent_id = snap.intent_id
                                                snap.client_order_id = _new_client_order_id(symbol, "BUY", now_e)
                                                snap.execution_status = "PENDING_SUBMIT"
                                                snap.execution_reason = f"{action_reason or 'ENTRY_INTENT'} | {sizing_note}"

                                                _add_event(
                                                    snap,
                                                    ev_name,
                                                    (
                                                        f"ENTRY_INTENT | px={px} qty={qty} "
                                                        f"client_order_id={snap.client_order_id} | "
                                                        f"{sizing_note} | conf_gate={safe_str(conf_gate)}"
                                                    ),
                                                    notify_title="BUY",
                                                    notify_body=(
                                                        f"{symbol} BUY intent\n"
                                                        f"px={px}\nqty={qty}\n"
                                                        f"client_order_id={snap.client_order_id}"
                                                    ),
                                                    client_order_id=snap.client_order_id,
                                                )

                                                action = "BUY"
                                                action_reason = "ENTRY_INTENT"
                                            else:
                                                fill_px, total_cost = state.ledger.buy(qty, px, now_e, cfg)
                                                state.risk.record_entry(now_e, cfg)

                                                state.entry_epoch = now_e
                                                state.peak_price = px
                                                state.trend_below_count = 0
                                                state.mfe_pct = Decimal("0")
                                                state.mae_pct = Decimal("0")
                                                state.high_since_entry = px
                                                state.low_since_entry = px

                                                _add_event(
                                                    snap,
                                                    ev_name,
                                                    (
                                                        f"ENTRY_FILLED | px={px} fill_px={fill_px} qty={qty} total_cost={total_cost} | "
                                                        f"{sizing_note} | conf_gate={safe_str(conf_gate)}"
                                                    ),
                                                    notify_title="BUY",
                                                    notify_body=f"{symbol} BUY px={px} qty={qty}",
                                                )

                                                action = ev_name
                                                action_reason = "ENTRY_FILLED"

            else:
                if require_confluence and (not conf_ok):
                    _emit_missed_buy(
                        snap,
                        event="MISSED_BUY_CONFLUENCE",
                        prefix="CONFLUENCE_VETO",
                        px=px,
                        confluence_min_score=confluence_min_score,
                        cooldown_remaining=int(cooldown_remaining),
                        equity=equity,
                        exposure=exposure,
                        extra=f"gate={safe_str(conf_gate)}",
                    )
                else:
                    qty_cap, qty_reason = _compute_buy_qty_and_reason(state.ledger, px, cfg)
                    qty, sizing_note, vol_used, qty_vol = _apply_phase4_vol_sizing(
                        state=state,
                        px=px,
                        qty_cap=_as_decimal(qty_cap, "0"),
                        cfg=cfg,
                    )
                    watch_gated2 = False
                    if require_confluence and conf_gate != "TRADE" and qty > 0:
                        try:
                            pre_mult_qty2 = qty
                            qty = qty * conf_size_mult
                            if qty <= 0 < pre_mult_qty2:
                                watch_gated2 = True
                        except Exception:
                            pass

                    if qty <= 0:
                        ev = "MISSED_BUY_WATCH_GATED" if watch_gated2 else _missed_buy_event_from_qty_reason(qty_reason)
                        _emit_missed_buy(
                            snap,
                            event=ev,
                            prefix="SIZE_BLOCK",
                            px=px,
                            confluence_min_score=confluence_min_score,
                            cooldown_remaining=int(cooldown_remaining),
                            equity=equity,
                            exposure=exposure,
                            extra=f"qty_reason={safe_str(qty_reason)}{' watch_gate='+conf_gate if watch_gated2 else ''}",
                        )
                    else:
                        if hasattr(state.risk, "can_enter_with_context"):
                            allowed, why = state.risk.can_enter_with_context(
                                now_epoch=now_e,
                                cfg=cfg,
                                spread_bps=getattr(snap, "liq_spread_bps", None),
                                proposed_qty=qty,
                                current_qty=state.ledger.position_qty,
                                px=px,
                                current_exposure_usd=exposure,
                                stale=bool(stale),
                                last_market_data_epoch=getattr(state, "last_good_tick_epoch", None),
                                symbol=symbol,
                            )
                        else:
                            allowed, why = state.risk.can_enter(now_e, cfg)

                        if not allowed:
                            ev = _missed_buy_event_from_risk_reason(why)
                            lockout_until = int(getattr(state.risk, "lockout_until_epoch", 0) or 0)
                            lockout_fmt = _fmt_lockout(now_e, lockout_until)

                            _emit_missed_buy(
                                snap,
                                event=ev,
                                prefix="RISK_BLOCK",
                                px=px,
                                confluence_min_score=confluence_min_score,
                                cooldown_remaining=int(cooldown_remaining),
                                equity=equity,
                                exposure=exposure,
                                extra=f"risk_reason={safe_str(why)} lockout_until={lockout_fmt}",
                            )
                            risk_blocked_reason = str(why or "")
                        else:
                            # Cross-coin correlation guard
                            cc_ok2, cc_reason2 = can_enter_cross_coin(symbol, cfg)
                            if not cc_ok2:
                                _emit_missed_buy(
                                    snap,
                                    event="MISSED_BUY_CROSS_COIN",
                                    prefix="CROSS_COIN_BLOCK",
                                    px=px,
                                    confluence_min_score=confluence_min_score,
                                    cooldown_remaining=int(cooldown_remaining),
                                    equity=equity,
                                    exposure=exposure,
                                    extra=f"cross_coin={cc_reason2}",
                                )
                            else:
                                ev_name = _map_trade_event(cfg, "WOULD_BUY")
                                snap.vol_used = vol_used
                                snap.vol_sizing_qty = qty_vol
                                snap.execution_qty = qty
                                snap.vol_reason = qty_reason
                                snap.sizing_note = sizing_note

                                if _adapter_mode_enabled(cfg, state):
                                    snap.intent_id = _new_intent_id(symbol, "BUY", now_e)
                                    snap.entry_intent_id = snap.intent_id
                                    snap.client_order_id = _new_client_order_id(symbol, "BUY", now_e)
                                    snap.execution_status = "PENDING_SUBMIT"
                                    snap.execution_reason = f"{action_reason or 'ENTRY_INTENT'} | {sizing_note}"

                                    _add_event(
                                        snap,
                                        ev_name,
                                        (
                                            f"ENTRY_INTENT | px={px} qty={qty} "
                                            f"client_order_id={snap.client_order_id} | "
                                            f"{sizing_note} | conf_gate={safe_str(conf_gate)}"
                                        ),
                                        notify_title="BUY",
                                        notify_body=(
                                            f"{symbol} BUY intent\n"
                                            f"px={px}\nqty={qty}\n"
                                            f"client_order_id={snap.client_order_id}"
                                        ),
                                        client_order_id=snap.client_order_id,
                                    )

                                    action = "BUY"
                                    action_reason = "ENTRY_INTENT"
                                else:
                                    fill_px, total_cost = state.ledger.buy(qty, px, now_e, cfg)
                                    state.risk.record_entry(now_e, cfg)

                                    state.entry_epoch = now_e
                                    state.peak_price = px
                                    state.trend_below_count = 0
                                    state.mfe_pct = Decimal("0")
                                    state.mae_pct = Decimal("0")
                                    state.high_since_entry = px
                                    state.low_since_entry = px

                                    _add_event(
                                        snap,
                                        ev_name,
                                        (
                                            f"ENTRY_FILLED | px={px} fill_px={fill_px} qty={qty} total_cost={total_cost} | "
                                            f"{sizing_note} | conf_gate={safe_str(conf_gate)}"
                                        ),
                                        notify_title="BUY",
                                        notify_body=f"{symbol} BUY px={px} qty={qty}",
                                    )

                                    action = ev_name
                                    action_reason = "ENTRY_FILLED"

    track_holds = _bool_cfg(cfg, "TRACK_HOLD_REASONS", True)
    if track_holds and (not state.ledger.in_pos()):
        entry_signal_ok_now = bool(st_1m.trend_ok and st_1m.signal == 1)

        hold_key, hold_detail = _classify_hold_reason(
            paused=paused,
            stale=stale,
            cooldown_remaining=int(cooldown_remaining),
            entry_signal_ok=entry_signal_ok_now,
            conf_ok=bool(conf_ok),
            conf_score=(None if snap.confluence_score is None else int(snap.confluence_score)),
            conf_min=int(confluence_min_score),
            st_1m=st_1m,
        )

        if action == "HOLD" and (not action_reason):
            action_reason = hold_key

        if (
            action == "HOLD"
            and conf_gate == "TRADE"
            and liq_blocks
            and cooldown_remaining == 0
            and (not paused)
            and (not stale)
        ):
            action_reason = "LIQUIDITY_BLOCK"

        if _should_bump_hold(state, now_e, cfg):
            tracker_enabled = _bool_cfg(cfg, "TRADE_TRACKER_ENABLED", True)
            disable_in_bt = _bool_cfg(cfg, "DISABLE_TRADE_TRACKER_IN_BACKTEST", True) and _bool_cfg(
                cfg, "BACKTEST_MODE", False
            )
            if tracker_enabled and (not disable_in_bt) and getattr(state, "tracker", None) is not None:
                state.tracker.bump(
                    hold_key,
                    f"{hold_detail} | px={px} | {snap.confluence_reasons} | "
                    f"struct={safe_str(getattr(snap,'structure_reasons',''))} | "
                    f"liq={safe_str(getattr(snap,'liq_reasons',''))}",
                )

    next_poll = choose_poll_seconds(cfg, in_pos=state.ledger.in_pos(), min_dist=min_dist)
    if st_1m.ma200 is None:
        next_poll = float(cfg["POLL_FAST_SECONDS"])

    snap.sig_1m = int(st_1m.signal)
    snap.trend_ok_1m = bool(st_1m.trend_ok)
    snap.score_1m = int(st_1m.score)

    snap.score_5m = None if state.last_st_5m is None else int(state.last_st_5m.score)
    snap.score_1h = None if state.last_st_1h is None else int(state.last_st_1h.score)

    snap.cash_usd = state.ledger.cash_usd
    snap.position_qty = state.ledger.position_qty
    snap.avg_entry_px = state.ledger.avg_entry_px
    snap.equity_usd = equity
    snap.exposure_usd = exposure
    snap.unrl_pnl_usd = unrl_pnl
    snap.realized_pnl_usd = realized_pnl

    snap.trades_today = getattr(state.risk, "trades_today", 0)
    snap.daily_realized_usd = getattr(state.risk, "daily_realized_pnl_usd", Decimal("0"))
    snap.lockout_until_epoch = int(getattr(state.risk, "lockout_until_epoch", 0) or 0)
    snap.cooldown_remaining_s = int(cooldown_remaining)

    snap.hold_s = int(hold_s)
    snap.trend_below_count = int(state.trend_below_count)

    snap.peak_price = state.peak_price
    snap.take_profit = take_profit
    snap.stop_loss = stop_loss
    snap.trail_stop = trail_stop

    snap.dist_ma200 = dist_ma200
    snap.dist_tp = dist_tp
    snap.dist_sl = dist_sl
    snap.dist_trail = dist_trail
    snap.min_dist = min_dist

    # Use stashed pre-reset MFE/MAE if this is a SELL tick (state was already reset)
    _stashed_mfe = getattr(state, "_sell_mfe_pct", None)
    _stashed_mae = getattr(state, "_sell_mae_pct", None)
    snap.mfe_pct = _stashed_mfe if _stashed_mfe is not None else state.mfe_pct
    snap.mae_pct = _stashed_mae if _stashed_mae is not None else state.mae_pct
    # Clear stash so next HOLD tick uses live values
    state._sell_mfe_pct = None
    state._sell_mae_pct = None

    snap.action = action
    snap.action_reason = action_reason
    snap.risk_blocked_reason = risk_blocked_reason
    snap.next_poll_s = float(next_poll)

    return snap