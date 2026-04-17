#!/usr/bin/env python3
"""Configuration loading, normalization, and validation utilities."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, List


class ConfigError(ValueError):
    """Raised when the trading bot configuration is invalid."""


PLACEHOLDER_MARKERS = (
    "YOUR_",
    "REPLACE_WITH",
    "CHANGE_ME",
    "<",
    "PLACEHOLDER",
)

DEFAULT_CONTRACT_SIZES = {
    "BTC-USDT": 0.001,
}

SUPPORTED_EXCHANGES = {"blofin", "coinbase"}
SUPPORTED_TIMEFRAMES = {"1m", "5m", "15m", "1h", "4h", "1d"}


def load_and_validate_config(config_path: str, base_dir: Path) -> Dict[str, Any]:
    raw = _read_json(base_dir / config_path)
    normalized = _normalize_config(raw)
    _validate_config(normalized)
    return normalized


def normalize_and_validate_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize and validate a raw config dict without reading from disk."""
    normalized = _normalize_config(raw)
    _validate_config(normalized)
    return normalized


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")

    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        location = f"line {exc.lineno}, column {exc.colno}"
        raise ConfigError(
            f"Invalid JSON in {path.name} at {location}: {exc.msg}. "
            "Fix the config before starting live trading."
        ) from exc


def _normalize_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    config = copy.deepcopy(raw)
    deprecations: List[str] = []

    exchange = str(config.get("exchange", "blofin")).lower().strip()
    config["exchange"] = exchange

    trading = dict(config.get("trading", {}))
    risk = dict(config.get("risk", {}))
    strategy = dict(config.get("strategy", {}))

    # Backwards compatibility for older flat configs.
    if exchange == "blofin" and "blofin" not in config:
        for _key in ("api_key", "api_secret", "passphrase"):
            if _key in config:
                deprecations.append(
                    f"Top-level '{_key}' is deprecated; use 'blofin.{_key}' instead")
        config["blofin"] = {
            "api_key": config.get("api_key", ""),
            "api_secret": config.get("api_secret", ""),
            "passphrase": config.get("passphrase", ""),
            "demo_mode": config.get("demo_mode", False),
        }

    if exchange == "coinbase" and "coinbase" not in config:
        for _key in ("api_key", "api_secret"):
            if _key in config:
                deprecations.append(
                    f"Top-level '{_key}' is deprecated; use 'coinbase.{_key}' instead")
        config["coinbase"] = {
            "api_key": config.get("api_key", ""),
            "api_secret": config.get("api_secret", ""),
        }

    _apply_exchange_env_overrides(config, exchange)

    allow_shorts = config.get("allow_shorts")
    if allow_shorts is not None:
        deprecations.append(
            "Top-level 'allow_shorts' is deprecated; use 'trading.allow_short' instead")
    if allow_shorts is not None and "allow_short" in trading and bool(allow_shorts) != bool(trading["allow_short"]):
        raise ConfigError("Conflict: allow_shorts and trading.allow_short disagree")
    if allow_shorts is not None and "allow_short" not in trading:
        trading["allow_short"] = bool(allow_shorts)

    flat_risk_per_trade = config.get("risk_per_trade_pct")
    nested_risk_per_trade = risk.get("risk_per_trade_pct")
    if flat_risk_per_trade is not None:
        deprecations.append(
            "Top-level 'risk_per_trade_pct' is deprecated; use 'risk.risk_per_trade_pct' instead")
    if flat_risk_per_trade is not None and nested_risk_per_trade is not None:
        if float(flat_risk_per_trade) != float(nested_risk_per_trade):
            raise ConfigError(
                "Conflict: top-level risk_per_trade_pct and risk.risk_per_trade_pct disagree"
            )
    elif flat_risk_per_trade is not None:
        risk["risk_per_trade_pct"] = float(flat_risk_per_trade)

    risk.setdefault("risk_per_trade_pct", 1.0)
    risk.setdefault("max_drawdown_pct", 25.0)
    risk.setdefault("leverage", 1.0)
    risk.setdefault("margin_mode", "isolated")
    risk.setdefault("contract_step", 0.1)
    risk.setdefault("min_contracts", 0.1)
    risk.setdefault("max_position_notional_pct", 100.0)
    risk.setdefault("slippage_buffer_pct", 0.1)
    risk.setdefault("allow_without_stop_loss", False)

    inst_id = str(config.get("trading_pair", "BTC-USDT"))
    if "contract_size" not in risk:
        inferred = DEFAULT_CONTRACT_SIZES.get(inst_id)
        if inferred is not None:
            risk["contract_size"] = inferred

    trading.setdefault("allow_long", True)
    trading.setdefault("allow_short", True)
    trading.setdefault("max_positions", 1)
    trading.setdefault("position_side_mode", "hedge")

    min_confidence = strategy.get("min_confidence", config.get("min_confidence", 0.6))
    config["min_confidence"] = float(min_confidence)
    config["trading"] = trading
    config["risk"] = risk
    config["strategy"] = strategy
    config["risk_per_trade_pct"] = float(risk["risk_per_trade_pct"])
    config["trading_pair"] = inst_id
    config.setdefault("timeframe", "5m")
    config.setdefault("strategy_name", "rsi")
    config.setdefault("dry_run", True)

    # --- New v2.2 sections (all disabled by default) ---
    protection = dict(config.get("protection", {}))
    protection.setdefault("use_server_side_tpsl", True)
    protection.setdefault("require_server_side_tpsl", True)
    protection.setdefault("tp_order_price", "-1")
    protection.setdefault("sl_order_price", "-1")
    protection.setdefault("sync_exchange_each_cycle", False)
    config["protection"] = protection

    circuit_breaker = dict(config.get("circuit_breaker", {}))
    circuit_breaker.setdefault("enabled", False)
    circuit_breaker.setdefault("daily_loss_limit_pct", 5.0)
    circuit_breaker.setdefault("max_consecutive_losses", 3)
    circuit_breaker.setdefault("max_consecutive_errors", 5)
    circuit_breaker.setdefault("max_price_move_pct_per_cycle", 5.0)
    circuit_breaker.setdefault("cooldown_minutes", 30)
    circuit_breaker.setdefault("trip_on_missing_protection", True)
    config["circuit_breaker"] = circuit_breaker

    market_data = dict(config.get("market_data", {}))
    market_data.setdefault("use_websocket", False)
    market_data.setdefault("max_staleness_seconds", 30)
    market_data.setdefault("warmup_timeout_seconds", 8)
    market_data.setdefault("max_cached_candles", 200)
    market_data.setdefault("ping_interval", 20)
    market_data.setdefault("ping_timeout", 10)
    market_data.setdefault("reconnect_delay_seconds", 5)
    config["market_data"] = market_data

    execution = dict(config.get("execution", {}))
    execution.setdefault("reconcile_pending_orders_each_cycle", False)
    execution.setdefault("max_pending_order_age_minutes", 30)
    execution.setdefault("attach_tpsl_on_entry", False)
    execution.setdefault("fallback_place_tpsl_after_partial_fill", False)
    execution.setdefault("use_private_order_websocket", False)
    execution.setdefault("prefer_private_order_websocket", True)
    execution.setdefault("sync_positions_after_private_update", True)
    execution.setdefault("private_update_sync_cooldown_seconds", 3)
    execution.setdefault("history_reconciliation_lookback_hours", 48)
    execution.setdefault("history_reconciliation_limit", 50)
    config["execution"] = execution

    parameter_selector = dict(config.get("parameter_selector", {}))
    parameter_selector.setdefault("enabled", False)
    parameter_selector.setdefault("live_profile_path", "memory/live_regime_profile.json")
    parameter_selector.setdefault("candidate_profile_path", "memory/live_regime_profile.candidate.json")
    parameter_selector.setdefault("auto_refresh_enabled", False)
    parameter_selector.setdefault("refresh_interval_minutes", 60)
    parameter_selector.setdefault("min_regime_overlap", 1)
    parameter_selector.setdefault("max_param_drift", 0.35)
    parameter_selector.setdefault("require_improvement", True)
    config["parameter_selector"] = parameter_selector

    # --- Trailing stop (v2.5) ---
    trailing = dict(config.get("trailing_stop", {}))
    trailing.setdefault("enabled", False)
    trailing.setdefault("breakeven_enabled", True)
    trailing.setdefault("breakeven_trigger_atr", 1.0)
    trailing.setdefault("breakeven_offset_atr", 0.05)
    trailing.setdefault("trail_enabled", True)
    trailing.setdefault("trail_activation_atr", 1.5)
    trailing.setdefault("trail_distance_atr", 1.5)
    trailing.setdefault("remove_tp_on_trail", False)
    trailing.setdefault("min_update_interval_seconds", 30)
    trailing.setdefault("regime_overrides", {})
    config["trailing_stop"] = trailing

    # --- Dynamic timeframe selection (v2.6, step 3) ---
    rtf = dict(config.get("regime_timeframes", {}))
    rtf.setdefault("enabled", False)
    rtf.setdefault("confirmation_bars", 3)
    rtf.setdefault("fallback_regime", "unclear")
    rtf.setdefault("timeframes", {
        "bull_trend": "15m",
        "bear_trend": "15m",
        "range": "5m",
        "chop": "1h",
        "unclear": "1h",
    })
    rtf.setdefault("check_intervals", {
        "bull_trend": 300,
        "bear_trend": 300,
        "range": 60,
        "chop": 900,
        "unclear": 900,
    })
    rtf.setdefault("urgency", {
        "chop": 3,
        "bear_trend": 2,
        "bull_trend": 2,
        "range": 1,
        "unclear": 0,
    })
    rtf.setdefault("history_cap", 50)
    config["regime_timeframes"] = rtf

    # --- Per-timeframe calibrated parameter profiles (v2.7, step 5) ---
    tfp = dict(config.get("timeframe_profiles", {}))
    tfp.setdefault("enabled", False)
    tfp.setdefault("path", "memory/timeframe_profiles.json")
    config["timeframe_profiles"] = tfp

    config["_deprecation_warnings"] = deprecations

    return config


def _apply_exchange_env_overrides(config: Dict[str, Any], exchange: str) -> None:
    env_map = {
        "blofin": {
            "api_key": "BLOFIN_API_KEY",
            "api_secret": "BLOFIN_API_SECRET",
            "passphrase": "BLOFIN_PASSPHRASE",
            "demo_mode": "BLOFIN_DEMO_MODE",
        },
        "coinbase": {
            "api_key": "COINBASE_API_KEY",
            "api_secret": "COINBASE_API_SECRET",
        },
    }

    section = dict(config.get(exchange, {}))
    for field, env_name in env_map.get(exchange, {}).items():
        env_value = os.getenv(env_name)
        if env_value is None or env_value == "":
            continue

        if field == "demo_mode":
            section[field] = env_value.lower() in {"1", "true", "yes", "on"}
        else:
            section[field] = env_value

    config[exchange] = section


def _validate_config(config: Dict[str, Any]) -> None:
    exchange = config["exchange"]
    if exchange not in SUPPORTED_EXCHANGES:
        raise ConfigError(f"Unsupported exchange: {exchange}")

    timeframe = str(config["timeframe"])
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ConfigError(f"Unsupported timeframe: {timeframe}")

    if not str(config.get("strategy_name", "")).strip():
        raise ConfigError("strategy_name is required")

    trading = config["trading"]
    risk = config["risk"]
    min_confidence = float(config["min_confidence"])

    if not trading["allow_long"] and not trading["allow_short"]:
        raise ConfigError("At least one of trading.allow_long or trading.allow_short must be true")

    if int(trading["max_positions"]) < 1:
        raise ConfigError("trading.max_positions must be >= 1")

    if not 0 <= min_confidence <= 1:
        raise ConfigError("min_confidence must be between 0 and 1")

    _require_positive_number(risk, "risk_per_trade_pct")
    _require_positive_number(risk, "leverage")
    _require_positive_number(risk, "contract_step")
    _require_positive_number(risk, "min_contracts")

    if float(risk["risk_per_trade_pct"]) > 100:
        raise ConfigError("risk.risk_per_trade_pct must be <= 100")

    if float(risk["leverage"]) > 20:
        raise ConfigError("risk.leverage must be <= 20")

    if float(risk["max_drawdown_pct"]) <= 0 or float(risk["max_drawdown_pct"]) > 100:
        raise ConfigError("risk.max_drawdown_pct must be > 0 and <= 100")

    if float(risk["max_position_notional_pct"]) <= 0:
        raise ConfigError("risk.max_position_notional_pct must be > 0")

    if config["exchange"] == "blofin" and "contract_size" not in risk:
        raise ConfigError(
            f"risk.contract_size is required for {config['trading_pair']} when exchange=blofin"
        )

    # --- Validate new v2.2 sections ---
    protection = config.get("protection", {})
    if protection.get("require_server_side_tpsl") and not protection.get("use_server_side_tpsl"):
        raise ConfigError(
            "protection.require_server_side_tpsl requires protection.use_server_side_tpsl to be true"
        )

    cb = config.get("circuit_breaker", {})
    if cb.get("enabled"):
        if float(cb.get("daily_loss_limit_pct", 5)) <= 0:
            raise ConfigError("circuit_breaker.daily_loss_limit_pct must be > 0")
        if int(cb.get("max_consecutive_losses", 3)) < 1:
            raise ConfigError("circuit_breaker.max_consecutive_losses must be >= 1")
        if int(cb.get("max_consecutive_errors", 5)) < 1:
            raise ConfigError("circuit_breaker.max_consecutive_errors must be >= 1")
        if float(cb.get("cooldown_minutes", 30)) <= 0:
            raise ConfigError("circuit_breaker.cooldown_minutes must be > 0")

    md = config.get("market_data", {})
    if float(md.get("max_staleness_seconds", 30)) <= 0:
        raise ConfigError("market_data.max_staleness_seconds must be > 0")

    execution = config.get("execution", {})
    if float(execution.get("private_update_sync_cooldown_seconds", 3)) < 0:
        raise ConfigError("execution.private_update_sync_cooldown_seconds must be >= 0")
    if float(execution.get("history_reconciliation_lookback_hours", 48)) < 1:
        raise ConfigError("execution.history_reconciliation_lookback_hours must be >= 1")

    ps = config.get("parameter_selector", {})
    if ps.get("enabled") and not str(ps.get("live_profile_path", "")).strip():
        raise ConfigError("parameter_selector.live_profile_path is required when enabled")
    if int(ps.get("refresh_interval_minutes", 60)) < 1:
        raise ConfigError("parameter_selector.refresh_interval_minutes must be >= 1")
    if float(ps.get("max_param_drift", 0.35)) < 0:
        raise ConfigError("parameter_selector.max_param_drift must be >= 0")

    ts = config.get("trailing_stop", {})
    if ts.get("enabled"):
        if float(ts.get("breakeven_trigger_atr", 1.0)) <= 0:
            raise ConfigError("trailing_stop.breakeven_trigger_atr must be > 0")
        if float(ts.get("trail_activation_atr", 1.5)) <= 0:
            raise ConfigError("trailing_stop.trail_activation_atr must be > 0")
        if float(ts.get("trail_distance_atr", 1.5)) <= 0:
            raise ConfigError("trailing_stop.trail_distance_atr must be > 0")
        if float(ts.get("min_update_interval_seconds", 30)) < 0:
            raise ConfigError("trailing_stop.min_update_interval_seconds must be >= 0")

    rtf = config.get("regime_timeframes", {})
    if rtf.get("enabled"):
        if int(rtf.get("confirmation_bars", 3)) < 1:
            raise ConfigError("regime_timeframes.confirmation_bars must be >= 1")
        required_regimes = {"bull_trend", "bear_trend", "range", "chop", "unclear"}
        tfs = rtf.get("timeframes", {}) or {}
        intervals = rtf.get("check_intervals", {}) or {}
        for regime in required_regimes:
            tf = tfs.get(regime)
            if tf is None:
                raise ConfigError(
                    f"regime_timeframes.timeframes.{regime} is required when enabled")
            if str(tf) not in SUPPORTED_TIMEFRAMES:
                raise ConfigError(
                    f"regime_timeframes.timeframes.{regime} has unsupported "
                    f"timeframe '{tf}' (supported: {sorted(SUPPORTED_TIMEFRAMES)})")
            interval = intervals.get(regime)
            if interval is None:
                raise ConfigError(
                    f"regime_timeframes.check_intervals.{regime} is required when enabled")
            if int(interval) < 10:
                raise ConfigError(
                    f"regime_timeframes.check_intervals.{regime} must be >= 10 seconds")
        fallback = str(rtf.get("fallback_regime", "unclear"))
        if fallback not in required_regimes:
            raise ConfigError(
                f"regime_timeframes.fallback_regime '{fallback}' must be one of {sorted(required_regimes)}")

    tfp = config.get("timeframe_profiles", {})
    if tfp.get("enabled"):
        path = str(tfp.get("path", "")).strip()
        if not path:
            raise ConfigError(
                "timeframe_profiles.path is required when enabled")

    if not config.get("dry_run", True):
        _validate_live_credentials(config)


def _validate_live_credentials(config: Dict[str, Any]) -> None:
    exchange = config["exchange"]
    section = config.get(exchange, {})

    required_fields = ["api_key", "api_secret"]
    if exchange == "blofin":
        required_fields.append("passphrase")

    missing = []
    for field in required_fields:
        value = str(section.get(field, "")).strip()
        if _is_placeholder(value):
            missing.append(field)

    if missing:
        raise ConfigError(
            f"Live trading requires real {exchange} credentials for: {', '.join(missing)}"
        )


def _is_placeholder(value: str) -> bool:
    if not value:
        return True
    upper = value.upper()
    return any(marker in upper for marker in PLACEHOLDER_MARKERS)


def _require_positive_number(section: Dict[str, Any], key: str) -> None:
    value = float(section[key])
    if value <= 0:
        raise ConfigError(f"{key} must be > 0")


def generate_config_report(config: Dict[str, Any]) -> Dict[str, Any]:
    """Generate a diagnostic report for preflight display."""
    exchange = config.get("exchange", "unknown")
    ex_section = config.get(exchange, {})
    creds_ok = all(
        not _is_placeholder(str(ex_section.get(f, "")))
        for f in (["api_key", "api_secret", "passphrase"] if exchange == "blofin"
                  else ["api_key", "api_secret"])
    )

    risk = config.get("risk", {})
    protection = config.get("protection", {})
    cb = config.get("circuit_breaker", {})

    return {
        "exchange": exchange,
        "trading_pair": config.get("trading_pair", ""),
        "dry_run": config.get("dry_run", True),
        "credentials_present": creds_ok,
        "risk_per_trade_pct": risk.get("risk_per_trade_pct"),
        "leverage": risk.get("leverage"),
        "contract_size": risk.get("contract_size"),
        "server_side_tpsl": protection.get("use_server_side_tpsl", False),
        "require_tpsl": protection.get("require_server_side_tpsl", False),
        "circuit_breaker_enabled": cb.get("enabled", False),
        "deprecation_warnings": config.get("_deprecation_warnings", []),
    }
