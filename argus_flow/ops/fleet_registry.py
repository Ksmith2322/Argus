"""Managed fleet registry helpers for the staged promotion pipeline.

Centralizes:
- which configs are part of the managed fleet
- which stage each config belongs to
- where each stage writes logs
- the default stage risk policy shown in QA/prod surfaces

This module is intentionally file-system driven so future additions can be made
by dropping in a config with a deployment block instead of editing every ops
surface.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONFIGS_DIR = REPO / "argus_flow" / "configs"
LOGS_DIR = REPO / "argus_flow" / "logs"
DEPLOYMENT_REGISTRY_FILE = LOGS_DIR / "deployment_registry.json"

STAGE_WATCHER = "watcher"
STAGE_DISCOVERY = "discovery"
STAGE_PAPER = "paper"
STAGE_REAL = "real"
STAGE_QUARANTINE = "quarantine"
STAGE_KILLED = "killed"
STAGE_ALIASES = {
    "qa": STAGE_PAPER,
    "prod": STAGE_REAL,
    "live": STAGE_REAL,
    "quarantined": STAGE_QUARANTINE,
}
STAGE_ORDER = {
    STAGE_DISCOVERY: 0,
    STAGE_WATCHER: 0,
    STAGE_PAPER: 1,
    STAGE_REAL: 2,
    STAGE_QUARANTINE: 3,
    STAGE_KILLED: 4,
}
try:
    from helio.fleet_sizing import get_initial_capital_usd as _fleet_anchor
    _MODEL_EQUITY = _fleet_anchor()
except Exception:
    _MODEL_EQUITY = 10000.0
RISK_POLICY_DEFAULTS = {
    "model_start_equity_usd": _MODEL_EQUITY,
    "base_risk_pct": 0.005,
    "earned_cap_pct": 0.03,
    "manual_step_up_required": False,
}


def _resolve_model_start_equity(raw_value) -> float:
    """Resolve model equity, including the fleet-anchor sentinel.

    Active paper configs use the string "fleet_anchor" so changing
    fleet_sizing.json moves Argus sizing without editing every config.
    """
    if isinstance(raw_value, str) and raw_value.strip().lower() == "fleet_anchor":
        return float(RISK_POLICY_DEFAULTS["model_start_equity_usd"])
    return float(raw_value or RISK_POLICY_DEFAULTS["model_start_equity_usd"])

# Legacy active fleet. Future additions can opt in by adding:
#   "deployment": {"managed": true, "stage": "watcher"}
LEGACY_MANAGED_CONFIGS: dict[str, dict[str, str]] = {
    # Active cohort (what runner_unified actually loads). Stages here are
    # fallbacks; the config file's own `stage` field wins via infer_stage.
    # GBPUSD + USDJPY are stage=paper (in cohort). CADJPY is stage=watcher
    # (trading but not yet qualified for formal cohort — 0 live trades).
    "gbpusd_range_paper_v1.json": {"stage": STAGE_PAPER, "name": "GBP/USD"},
    "usdjpy_mtf_paper_v1.json": {"stage": STAGE_PAPER, "name": "USD/JPY"},
    "cadjpy_mtf_paper_v1.json": {"stage": STAGE_WATCHER, "name": "CAD/JPY"},
    # Legacy / not currently present on disk — kept for registry continuity
    "eurusd_t4_paper_v1.json": {"stage": STAGE_PAPER, "name": "EUR/USD"},
    "eurjpy_t4_paper_v1.json": {"stage": STAGE_PAPER, "name": "EUR/JPY"},
    "gbpjpy_t4_paper_v1.json": {"stage": STAGE_WATCHER, "name": "GBP/JPY"},
    "cadjpy_t4_paper_v1.json": {"stage": STAGE_WATCHER, "name": "CAD/JPY"},
    "audjpy_t4_paper_v1.json": {"stage": STAGE_WATCHER, "name": "AUD/JPY"},
    "usdjpy_ny_paper_v1.json": {"stage": STAGE_WATCHER, "name": "USD/JPY"},
    "audusd_ny_paper_v1.json": {"stage": STAGE_WATCHER, "name": "AUD/USD"},
    "mes_range_paper_v1.json": {"stage": STAGE_WATCHER, "name": "MES"},
    "mnq_range_paper_v1.json": {"stage": STAGE_WATCHER, "name": "MNQ"},
    "mym_range_paper_v1.json": {"stage": STAGE_WATCHER, "name": "MYM"},
    "m2k_range_paper_v1.json": {"stage": STAGE_WATCHER, "name": "M2K"},
    "mgc_range_paper_v1.json": {"stage": STAGE_WATCHER, "name": "MGC"},
    "mcl_range_paper_v1.json": {"stage": STAGE_WATCHER, "name": "MCL"},
}


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_existing_stage_map() -> dict[str, str]:
    """Return sticky current_stage values from the last deployment registry."""
    report = _load_json(DEPLOYMENT_REGISTRY_FILE)
    if not isinstance(report, dict):
        return {}
    stage_map: dict[str, str] = {}
    for entry in report.get("runners", []):
        if not isinstance(entry, dict):
            continue
        config_file = str(entry.get("config_file", "") or "")
        current_stage = normalize_stage(entry.get("current_stage"))
        if config_file and current_stage:
            stage_map[config_file] = current_stage
    return stage_map


def normalize_stage(stage: object) -> str:
    value = str(stage or "").strip().lower()
    if value in STAGE_ALIASES:
        value = STAGE_ALIASES[value]
    if value in STAGE_ORDER:
        return value
    return ""


def stage_rank(stage: object) -> int:
    return STAGE_ORDER.get(normalize_stage(stage), -1)


def stage_display_label(stage: object) -> str:
    normalized = normalize_stage(stage)
    return {
        STAGE_DISCOVERY: "DISCOVERY",
        STAGE_WATCHER: "WATCHER",
        STAGE_PAPER: "QA",
        STAGE_REAL: "PROD",
        STAGE_QUARANTINE: "QUARANTINED",
        STAGE_KILLED: "KILLED",
    }.get(normalized, str(stage or "").upper())


def stage_account(stage: object) -> str:
    normalized = normalize_stage(stage)
    if normalized in (STAGE_REAL, STAGE_QUARANTINE):
        return "real"
    if normalized == STAGE_PAPER:
        return "paper"
    if normalized == STAGE_WATCHER:
        return "observer"
    return "none"


def stage_execution_mode(stage: object) -> str:
    normalized = normalize_stage(stage)
    if normalized == STAGE_PAPER:
        return "paper"
    if normalized in (STAGE_REAL, STAGE_QUARANTINE):
        return "real"
    if normalized == STAGE_WATCHER:
        return "observe"
    return "disabled"


def is_validation_stage(stage: object) -> bool:
    return normalize_stage(stage) == STAGE_PAPER


def is_real_money_stage(stage: object) -> bool:
    return normalize_stage(stage) in (STAGE_REAL, STAGE_QUARANTINE)


def is_launchable_stage(stage: object) -> bool:
    return normalize_stage(stage) in (STAGE_WATCHER, STAGE_PAPER, STAGE_REAL, STAGE_QUARANTINE)


def is_observer_stage(stage: object) -> bool:
    return normalize_stage(stage) in (STAGE_DISCOVERY, STAGE_WATCHER, STAGE_KILLED)


def default_display_name(symbol: str) -> str:
    symbol = (symbol or "").upper()
    if len(symbol) == 6 and symbol.isalpha():
        return f"{symbol[:3]}/{symbol[3:]}"
    return symbol


def _variant_suffix(deployment: dict, stage: str, config_path: Path | None = None) -> str:
    if normalize_stage(stage) != STAGE_WATCHER:
        return ""
    if not isinstance(deployment, dict):
        return ""
    raw = str(deployment.get("variant_type", "") or "").strip().lower()
    if not raw and config_path is not None and deployment.get("variant_of"):
        stem = config_path.stem.lower()
        if "_watcher_v1" in stem:
            raw = stem.replace("_watcher_v1", "")
    if not raw:
        return ""
    safe = "".join(ch if ch.isalnum() else "_" for ch in raw).strip("_")
    return safe


def strategy_display_name(strategy: str) -> str:
    text = str(strategy or "").strip()
    if not text:
        return "Unknown"
    aliases = {
        "range_accel": "Range + Accel",
        "t4_full_stack": "T4 Full Stack",
        "vol_burst": "Vol Burst",
    }
    key = text.lower()
    if key in aliases:
        return aliases[key]
    return text.replace("_", " ").title()


def unit_for_config(config: dict) -> str:
    return "pips" if str(config.get("instrument_type", "forex")).lower() == "forex" else "bps"


def mult_for_config(config: dict) -> int:
    if str(config.get("instrument_type", "forex")).lower() != "forex":
        return 1
    symbol = str(config.get("symbol", "")).upper()
    return 100 if "JPY" in symbol else 10000


def default_log_dir(symbol: str, stage: str, variant_suffix: str = "") -> str:
    stage = normalize_stage(stage)
    symbol = str(symbol or "").lower()
    if stage in (STAGE_REAL, STAGE_QUARANTINE):
        return f"argus_flow/logs/live_{symbol}"
    if stage == STAGE_WATCHER and variant_suffix:
        return f"argus_flow/logs/{symbol}_{variant_suffix}"
    return f"argus_flow/logs/{symbol}"


def resolve_log_dir(config: dict, config_path: Path) -> str:
    deployment = config.get("deployment", {}) if isinstance(config.get("deployment", {}), dict) else {}
    raw = str(deployment.get("log_dir", "") or "").strip()
    if raw:
        return raw.replace("\\", "/")
    stage = infer_stage(config, config_path)
    variant_suffix = _variant_suffix(deployment, stage, config_path)
    return default_log_dir(str(config.get("symbol", config_path.stem)), stage, variant_suffix=variant_suffix)


def is_live_config(config: dict, config_path: Path) -> bool:
    if bool(config.get("live", False)):
        return True
    version = str(config.get("version", "")).lower()
    if "live" in version:
        return True
    return config_path.stem.lower().endswith("_live_v1")


def is_managed_config(config: dict, config_path: Path) -> bool:
    deployment = config.get("deployment", {}) if isinstance(config.get("deployment", {}), dict) else {}
    if bool(deployment.get("managed", False)):
        return True
    if config_path.name in LEGACY_MANAGED_CONFIGS:
        return True
    # Configs with explicit stage field are managed
    if config.get("stage") in ("paper", "watcher", "killed", "real", "discovery"):
        return True
    return is_live_config(config, config_path)


def infer_stage(config: dict, config_path: Path, sticky_stage: str | None = None) -> str:
    deployment = config.get("deployment", {}) if isinstance(config.get("deployment", {}), dict) else {}
    # Top-level stage field takes priority (set by fleet management), then deployment.stage
    configured_stage = normalize_stage(config.get("stage")) or normalize_stage(deployment.get("stage"))
    if configured_stage:
        base_stage = configured_stage
    elif is_live_config(config, config_path):
        base_stage = STAGE_REAL
    else:
        base_stage = LEGACY_MANAGED_CONFIGS.get(config_path.name, {}).get("stage", STAGE_WATCHER)

    sticky = normalize_stage(sticky_stage)
    if sticky and stage_rank(sticky) > stage_rank(base_stage):
        return sticky
    return base_stage


def live_counterpart_path(config: dict) -> Path:
    symbol = str(config.get("symbol", "")).lower()
    return CONFIGS_DIR / f"{symbol}_live_v1.json"


def resolve_risk_policy(config: dict, config_path: Path, stage: str | None = None) -> dict:
    deployment = config.get("deployment", {}) if isinstance(config.get("deployment", {}), dict) else {}
    risk_policy = deployment.get("risk_policy", {}) if isinstance(deployment.get("risk_policy", {}), dict) else {}
    risk = config.get("risk", {}) if isinstance(config.get("risk", {}), dict) else {}
    resolved_stage = normalize_stage(stage) or infer_stage(config, config_path)

    base_risk = float(risk_policy.get("base_risk_pct", RISK_POLICY_DEFAULTS["base_risk_pct"]) or RISK_POLICY_DEFAULTS["base_risk_pct"])
    default_active = base_risk
    if resolved_stage in (STAGE_DISCOVERY, STAGE_WATCHER, STAGE_KILLED):
        default_active = 0.0
    elif resolved_stage == STAGE_QUARANTINE:
        default_active = min(base_risk, max(base_risk * 0.5, 0.0025))
    active_risk = float(risk_policy.get("active_risk_pct", default_active) or default_active)
    earned_cap = float(risk_policy.get("earned_cap_pct", RISK_POLICY_DEFAULTS["earned_cap_pct"]) or RISK_POLICY_DEFAULTS["earned_cap_pct"])

    return {
        "stage": resolved_stage,
        "base_risk_pct": base_risk,
        "active_risk_pct": active_risk,
        "configured_risk_pct": float(risk.get("risk_pct", 0.0) or 0.0),
        "earned_cap_pct": earned_cap,
        "manual_step_up_required": bool(risk_policy.get("manual_step_up_required", RISK_POLICY_DEFAULTS["manual_step_up_required"])),
        "model_start_equity_usd": _resolve_model_start_equity(
            risk_policy.get("model_start_equity_usd", RISK_POLICY_DEFAULTS["model_start_equity_usd"])
        ),
        "scale_state": str(risk_policy.get("scale_state", "BASE") or "BASE"),
    }


def validate_risk_policy_for_execution(config: dict, config_path: Path, stage: str | None = None) -> str | None:
    """Return an error message if risk policy is invalid for execution, or None if OK.

    Paper and watcher stages MUST have explicit model_start_equity_usd to prevent
    sizing against real broker equity.
    """
    resolved_stage = normalize_stage(stage) or infer_stage(config, config_path)
    if resolved_stage != STAGE_PAPER:
        return None  # only paper stage trades; watchers observe, real uses broker equity

    deployment = config.get("deployment", {}) if isinstance(config.get("deployment", {}), dict) else {}
    risk_policy = deployment.get("risk_policy", {}) if isinstance(deployment.get("risk_policy", {}), dict) else {}
    raw_value = risk_policy.get("model_start_equity_usd")

    if raw_value is None or raw_value == "" or raw_value == 0:
        return (
            f"model_start_equity_usd is required for {resolved_stage} stage but is "
            f"{'missing' if raw_value is None else f'zero/empty ({raw_value!r})'}. "
            f"Add deployment.risk_policy.model_start_equity_usd to {config_path.name}. "
            f"Use the string \"fleet_anchor\" to inherit from fleet_sizing.json, or "
            f"a positive number to override."
        )
    # Sentinel: "fleet_anchor" means inherit from helio/fleet_sizing config.
    # We still require the key to be present so an operator can't silently
    # forget to declare sizing intent.
    if isinstance(raw_value, str) and raw_value.strip().lower() == "fleet_anchor":
        return None
    try:
        val = float(raw_value)
        if val <= 0:
            return f"model_start_equity_usd must be positive, got {val} in {config_path.name}"
    except (TypeError, ValueError):
        return (
            f"model_start_equity_usd is not a valid number or the 'fleet_anchor' "
            f"sentinel: {raw_value!r} in {config_path.name}"
        )

    return None


def load_existing_registry_entry(config_file: str) -> dict | None:
    report = _load_json(DEPLOYMENT_REGISTRY_FILE)
    if not isinstance(report, dict):
        return None
    for entry in report.get("runners", []):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("config_file", "") or "") == config_file:
            return entry
    return None


def discover_managed_runners() -> list[dict]:
    """Return normalized managed-runner metadata for dashboard/ops surfaces."""
    sticky_map = load_existing_stage_map()
    runners: list[dict] = []
    for config_path in sorted(CONFIGS_DIR.glob("*.json")):
        if config_path.name == "hashes.json":
            continue
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if "instrument_type" not in config or not is_managed_config(config, config_path):
            continue

        symbol = str(config.get("symbol", config_path.stem)).upper()
        deployment = config.get("deployment", {}) if isinstance(config.get("deployment", {}), dict) else {}
        current_stage = infer_stage(config, config_path, sticky_stage=sticky_map.get(config_path.name))
        legacy = LEGACY_MANAGED_CONFIGS.get(config_path.name, {})
        log_dir = resolve_log_dir(config, config_path)
        risk_policy = resolve_risk_policy(config, config_path, stage=current_stage)
        live_path = live_counterpart_path(config)
        strategy_status = stage_display_label(current_stage)

        runners.append(
            {
                "id": f"{current_stage}:{config_path.stem.lower()}",
                "name": str(
                    config.get(
                        "name",
                        (
                            f"{default_display_name(symbol)} ({deployment.get('variant_label')})"
                            if deployment.get("variant_label")
                            else legacy.get("name", default_display_name(symbol))
                        ),
                    )
                ),
                "symbol": symbol,
                "strategy": strategy_display_name(str(config.get("strategy", ""))),
                "instrument_type": str(config.get("instrument_type", "forex")).lower(),
                "unit": unit_for_config(config),
                "mult": mult_for_config(config),
                "config_file": config_path.name,
                "config_path": str(config_path.relative_to(REPO)).replace("\\", "/"),
                "log_dir": log_dir,
                "current_stage": current_stage,
                "strategy_status": strategy_status,
                "stage_account": stage_account(current_stage),
                "execution_mode": stage_execution_mode(current_stage),
                "launch_enabled": is_launchable_stage(current_stage),
                "managed": True,
                "live": is_live_config(config, config_path),
                "version": str(config.get("version", "")),
                "risk_policy": risk_policy,
                "live_counterpart_config_file": live_path.name if live_path.exists() else "",
                "paper_source_config_file": str(deployment.get("paper_source", "") or ""),
            }
        )

    runners.sort(key=lambda item: (stage_rank(item["current_stage"]), item["name"]))
    return runners


def runners_for_stages(stages: set[str] | list[str] | tuple[str, ...]) -> list[dict]:
    normalized = {normalize_stage(stage) for stage in stages}
    return [runner for runner in discover_managed_runners() if runner["current_stage"] in normalized]
