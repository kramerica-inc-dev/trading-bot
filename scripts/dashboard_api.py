#!/usr/bin/env python3
"""
Trading Bot Dashboard API Server
Reads bot state files from memory/ and serves them as JSON.
Serves a static HTML dashboard on port 8080.
"""

import csv
import json
import math
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_PORT = 8080
BOT_DIR = Path(os.environ.get("BOT_DIR", "/opt/trading-bot"))
MEMORY_DIR = BOT_DIR / "memory"
CONFIG_PATH = BOT_DIR / "config.json"
DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"

# Plan E paths (multi-instance layout)
STATE_DIR = BOT_DIR / "state"
CONFIGS_DIR = BOT_DIR / "configs"
SHARED_CACHE = STATE_DIR / "shared_cache"
# Legacy single-instance paths, kept as fallbacks when multi-instance state
# has not yet been created.
LEGACY_PLAN_E_STATE = STATE_DIR / "plan_e_portfolio.json"
LEGACY_PLAN_E_TRADES = STATE_DIR / "plan_e_trades.log"
LEGACY_PLAN_E_CONFIG = BOT_DIR / "config.plan-e.json"
LEGACY_RUNNER_CACHE = STATE_DIR / "runner_cache"

DEFAULT_INSTANCE = "plan-e-base"

# Service control: allow the legacy unit plus any templated plan-e@<instance>
# where <instance> matches a simple allow-listed charset.
CONTROLLABLE_SERVICES = {"plan-e-runner"}
INSTANCE_NAME_RE = None  # compiled below
import re  # noqa: E402
INSTANCE_NAME_RE = re.compile(r"^plan-e-[a-z0-9]{1,16}$")

# Simple CSRF/auth token — generated once per process start, embedded in the
# served HTML, required on POST /api/control. Tailscale-private network so
# this is defence-in-depth, not primary auth.
DASHBOARD_TOKEN = secrets.token_urlsafe(24)

# Throttle control actions: reject if another action fired <N seconds ago
CONTROL_COOLDOWN_S = 3.0
_last_control_ts = 0.0
_control_lock = threading.Lock()

# Cache: re-read files at most every N seconds
CACHE_TTL = 3.0
_cache: Dict[str, Any] = {}
_cache_ts: Dict[str, float] = {}
_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# File readers
# ---------------------------------------------------------------------------

def _read_json_file(path: Path, default: Any = None) -> Any:
    """Read a JSON file with caching."""
    key = str(path)
    now = time.time()
    with _cache_lock:
        if key in _cache and (now - _cache_ts.get(key, 0)) < CACHE_TTL:
            return _cache[key]
    try:
        if not path.exists():
            return default
        data = json.loads(path.read_text())
        with _cache_lock:
            _cache[key] = data
            _cache_ts[key] = now
        return data
    except Exception:
        return default


def _read_jsonl_file(path: Path, max_lines: int = 200) -> List[Dict]:
    """Read last N lines from a JSONL file."""
    key = f"{path}:jsonl:{max_lines}"
    now = time.time()
    with _cache_lock:
        if key in _cache and (now - _cache_ts.get(key, 0)) < CACHE_TTL:
            return _cache[key]
    try:
        if not path.exists():
            return []
        lines = path.read_text().strip().splitlines()
        lines = lines[-max_lines:]
        rows = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        with _cache_lock:
            _cache[key] = rows
            _cache_ts[key] = now
        return rows
    except Exception:
        return []


def _safe_config() -> Dict:
    """Read config but strip credentials."""
    cfg = _read_json_file(CONFIG_PATH, {})
    safe = dict(cfg)
    # Remove credentials
    for exch in ("blofin", "coinbase"):
        if exch in safe:
            section = dict(safe[exch])
            for k in ("api_key", "api_secret", "passphrase"):
                if k in section:
                    section[k] = "***"
            safe[exch] = section
    return safe


# ---------------------------------------------------------------------------
# Compute derived stats
# ---------------------------------------------------------------------------

def _compute_trade_stats(trades: List[Dict]) -> Dict:
    if not trades:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "avg_pnl": 0, "avg_win": 0, "avg_loss": 0,
                "profit_factor": 0, "total_pnl": 0, "best": 0, "worst": 0}
    wins = [t for t in trades if t.get("pnl", 0) >= 0]
    losses = [t for t in trades if t.get("pnl", 0) < 0]
    total_win = sum(t["pnl"] for t in wins) if wins else 0
    total_loss = sum(t["pnl"] for t in losses) if losses else 0
    return {
        "total": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "avg_pnl": round(sum(t.get("pnl", 0) for t in trades) / len(trades), 2),
        "avg_win": round(total_win / len(wins), 2) if wins else 0,
        "avg_loss": round(total_loss / len(losses), 2) if losses else 0,
        "profit_factor": round(abs(total_win / total_loss), 2) if total_loss else 0,
        "total_pnl": round(total_win + total_loss, 2),
        "best": round(max((t.get("pnl", 0) for t in trades), default=0), 2),
        "worst": round(min((t.get("pnl", 0) for t in trades), default=0), 2),
    }


def _bot_service_status() -> Dict:
    """Check systemd service status."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "trading-bot"],
            capture_output=True, text=True, timeout=5)
        active = result.stdout.strip()
    except Exception:
        active = "unknown"
    uptime = ""
    try:
        result = subprocess.run(
            ["systemctl", "show", "trading-bot", "--property=ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=5)
        ts_str = result.stdout.strip().split("=", 1)[-1].strip()
        if ts_str:
            # Parse systemd timestamp
            from email.utils import parsedate_to_datetime
            try:
                started = datetime.fromisoformat(ts_str)
            except Exception:
                started = None
            if started:
                delta = datetime.now(timezone.utc) - started.astimezone(timezone.utc)
                hours = int(delta.total_seconds() // 3600)
                minutes = int((delta.total_seconds() % 3600) // 60)
                uptime = f"{hours}h {minutes}m"
    except Exception:
        pass
    return {"status": active, "uptime": uptime}


# ---------------------------------------------------------------------------
# API endpoint builder
# ---------------------------------------------------------------------------

def build_api_response() -> Dict:
    """Build the full dashboard API response."""
    positions = _read_json_file(MEMORY_DIR / "positions.json", [])
    pending = _read_json_file(MEMORY_DIR / "pending-orders.json", [])
    state = _read_json_file(MEMORY_DIR / "runtime-state.json", {})
    config = _safe_config()
    trades = _read_jsonl_file(MEMORY_DIR / "performance.jsonl", 500)
    logs = _read_jsonl_file(MEMORY_DIR / "trading-log.jsonl", 100)
    service = _bot_service_status()

    # Extract latest indicator snapshot from logs
    latest_indicators = {}
    latest_regime = "unknown"
    latest_regime_confidence = 0.0
    latest_price = 0.0
    latest_cycle_time = ""
    mtf_states = {}

    for entry in reversed(logs):
        msg = entry.get("message", "")
        data = entry.get("data", {})
        # Find the most recent indicator log line
        if "HOLD" in msg or "BUY" in msg or "SELL" in msg:
            if "conf=" in msg and "regime=" in msg:
                # Parse: "📊 HOLD conf=0% regime=chop(42%) ..."
                try:
                    parts = msg.split()
                    for p in parts:
                        if p.startswith("conf="):
                            pass  # confidence in signal
                        if p.startswith("regime="):
                            regime_part = p.replace("regime=", "")
                            if "(" in regime_part:
                                latest_regime = regime_part.split("(")[0]
                                conf_str = regime_part.split("(")[1].rstrip("%)")
                                latest_regime_confidence = float(conf_str) / 100
                except Exception:
                    pass
                # Parse indicators from the | part
                if "|" in msg:
                    ind_part = msg.split("|", 1)[1].strip()
                    for pair in ind_part.split(","):
                        pair = pair.strip()
                        if ":" in pair:
                            k, v = pair.split(":", 1)
                            try:
                                latest_indicators[k.strip()] = float(v.strip())
                            except ValueError:
                                pass
                if not latest_cycle_time:
                    latest_cycle_time = entry.get("timestamp", "")
                break
        if "📈" in msg and "$" in msg:
            try:
                price_str = msg.split("$")[1].split()[0].replace(",", "")
                latest_price = float(price_str)
            except Exception:
                pass

    # Balance from runtime state (reliable), fall back to USDT log parsing
    latest_balance = state.get("last_balance")
    if not latest_balance:
        for entry in reversed(logs):
            msg = entry.get("message", "")
            if "💰 Balance:" in msg and "USDT" in msg:
                try:
                    bal_str = msg.split("Balance:")[1].strip().split()[0]
                    latest_balance = float(bal_str)
                except Exception:
                    pass
                break


    # Build trade stats
    trade_stats = _compute_trade_stats(trades)

    # Circuit breaker
    cb = state.get("circuit_breaker", {})

    # Risk config (safe)
    risk_cfg = config.get("risk", {})
    protection = config.get("protection", {})
    circuit_cfg = config.get("circuit_breaker", {})
    trading_cfg = config.get("trading", {})

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "bot": {
            "status": service["status"],
            "uptime": service["uptime"],
            "version": config.get("_version", "2.4"),
            "exchange": config.get("exchange", "blofin"),
            "trading_pair": config.get("trading_pair", "BTC-USDT"),
            "dry_run": config.get("dry_run", True),
            "timeframe": config.get("timeframe", "5m"),
            "last_cycle": latest_cycle_time,
        },
        "market": {
            "price": latest_price,
            "regime": latest_regime,
            "regime_confidence": latest_regime_confidence,
            "indicators": latest_indicators,
            "mtf": mtf_states,
        },
        "account": {
            "balance": latest_balance,
            "start_balance": state.get("start_balance"),
            "peak_balance": state.get("peak_balance"),
        },
        "positions": positions,
        "pending_orders": pending,
        "circuit_breaker": {
            "active": cb.get("active", False),
            "reason": cb.get("reason", ""),
            "tripped_at": cb.get("tripped_at"),
            "until": cb.get("until"),
            "loss_streak": state.get("loss_streak", 0),
            "error_streak": state.get("error_streak", 0),
        },
        "trades": trades[-50:],  # last 50
        "trade_stats": trade_stats,
        "config": {
            "risk_per_trade_pct": risk_cfg.get("risk_per_trade_pct"),
            "leverage": risk_cfg.get("leverage"),
            "contract_size": risk_cfg.get("contract_size"),
            "margin_mode": risk_cfg.get("margin_mode"),
            "max_positions": trading_cfg.get("max_positions"),
            "position_side_mode": trading_cfg.get("position_side_mode"),
            "allow_long": trading_cfg.get("allow_long"),
            "allow_short": trading_cfg.get("allow_short"),
            "server_side_tpsl": protection.get("use_server_side_tpsl"),
            "require_tpsl": protection.get("require_server_side_tpsl"),
            "sync_each_cycle": protection.get("sync_exchange_each_cycle"),
            "circuit_breaker_enabled": circuit_cfg.get("enabled"),
            "daily_loss_limit_pct": circuit_cfg.get("daily_loss_limit_pct"),
            "max_consecutive_losses": circuit_cfg.get("max_consecutive_losses"),
            "cooldown_minutes": circuit_cfg.get("cooldown_minutes"),
        },
        "logs": logs[-30:],  # last 30 log entries
    }


# ---------------------------------------------------------------------------
# Plan E readers
# ---------------------------------------------------------------------------

def _read_csv_last_close(path: Path) -> Optional[float]:
    """Return the last `close` value from a CSV, or None if not readable."""
    try:
        if not path.exists():
            return None
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
            if not rows:
                return None
            val = rows[-1].get("close")
            return float(val) if val is not None else None
    except Exception:
        return None


def _next_rebalance_ts(rebalance_hour_utc: int) -> str:
    """ISO timestamp of the next UTC rebalance hour after now."""
    now = datetime.now(timezone.utc)
    candidate = now.replace(hour=rebalance_hour_utc, minute=0, second=0, microsecond=0)
    if candidate <= now:
        # advance to tomorrow
        from datetime import timedelta
        candidate = candidate + timedelta(days=1)
    return candidate.isoformat()


def _service_status(unit: str) -> Dict[str, Any]:
    """systemctl is-active + ActiveEnterTimestamp for a unit."""
    try:
        r = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=5)
        active = r.stdout.strip() or r.stderr.strip() or "unknown"
    except Exception:
        active = "unknown"
    uptime = ""
    try:
        r = subprocess.run(
            ["systemctl", "show", unit, "--property=ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=5)
        ts_str = r.stdout.strip().split("=", 1)[-1].strip()
        if ts_str and ts_str != "0":
            started = datetime.strptime(ts_str, "%a %Y-%m-%d %H:%M:%S %Z")
            started = started.replace(tzinfo=timezone.utc)
            delta = datetime.now(timezone.utc) - started
            h = int(delta.total_seconds() // 3600)
            m = int((delta.total_seconds() % 3600) // 60)
            uptime = f"{h}h {m}m"
    except Exception:
        pass
    return {"status": active, "uptime": uptime}


def _list_instances() -> List[str]:
    """Enumerate available instances from configs/ directory.

    Falls back to [DEFAULT_INSTANCE] when no configs exist yet (legacy deploy).
    """
    out: List[str] = []
    if CONFIGS_DIR.exists():
        for p in sorted(CONFIGS_DIR.glob("plan-e-*.json")):
            name = p.stem  # e.g. "plan-e-base"
            if INSTANCE_NAME_RE and INSTANCE_NAME_RE.match(name):
                out.append(name)
    if not out:
        out = [DEFAULT_INSTANCE]
    return out


def _instance_paths(instance: str) -> Dict[str, Path]:
    """Resolve per-instance state/config/trades paths.

    Falls back to legacy single-instance paths when the instance name matches
    the default and the new layout does not yet exist.
    """
    inst_dir = STATE_DIR / instance
    state_path = inst_dir / "portfolio.json"
    trades_path = inst_dir / "trades.log"
    cfg_path = CONFIGS_DIR / f"{instance}.json"

    # Fallback to legacy paths if instance == default and multi-instance
    # state hasn't been created yet.
    if instance == DEFAULT_INSTANCE:
        if not state_path.exists() and LEGACY_PLAN_E_STATE.exists():
            state_path = LEGACY_PLAN_E_STATE
        if not trades_path.exists() and LEGACY_PLAN_E_TRADES.exists():
            trades_path = LEGACY_PLAN_E_TRADES
        if not cfg_path.exists() and LEGACY_PLAN_E_CONFIG.exists():
            cfg_path = LEGACY_PLAN_E_CONFIG

    # Cache is shared across instances (new layout) but may still live at the
    # legacy runner_cache/ path on older deployments.
    cache_dir = SHARED_CACHE if SHARED_CACHE.exists() else LEGACY_RUNNER_CACHE

    return {
        "state": state_path,
        "trades": trades_path,
        "config": cfg_path,
        "cache": cache_dir,
    }


def _instance_service_name(instance: str) -> str:
    """Resolve the systemd unit name for an instance.

    New layout uses templated units: plan-e@base, plan-e@c, etc.
    Legacy single-instance deploy uses plan-e-runner.
    """
    # plan-e-base -> plan-e@base ; plan-e-c -> plan-e@c ; etc.
    suffix = instance.removeprefix("plan-e-")
    return f"plan-e@{suffix}"


def _plan_e_signals_from_cache(
    cache_dir: Path, universe: List[str], lookback_h: int, sign: int,
) -> Dict[str, Any]:
    """Compute signals from an arbitrary cache directory."""
    out: Dict[str, Any] = {}
    for sym in universe:
        path = cache_dir / f"{sym}_1H.csv"
        if not path.exists():
            continue
        try:
            with open(path, newline="") as f:
                rows = list(csv.DictReader(f))
            if len(rows) <= lookback_h:
                continue
            latest = float(rows[-1]["close"])
            past = float(rows[-1 - lookback_h]["close"])
            if latest > 0 and past > 0 and math.isfinite(latest) and math.isfinite(past):
                signal = sign * math.log(latest / past)
                out[sym] = {
                    "signal": signal,
                    "last_price": latest,
                    "past_price": past,
                    "bar_ts": rows[-1].get("timestamp"),
                }
        except Exception:
            continue
    return out


def build_plan_e_response(instance: str = DEFAULT_INSTANCE) -> Dict:
    """Full Plan E dashboard payload for a single instance."""
    if INSTANCE_NAME_RE and not INSTANCE_NAME_RE.match(instance):
        raise ValueError(f"invalid instance name: {instance}")
    paths = _instance_paths(instance)
    state = _read_json_file(paths["state"], {})
    cfg = _read_json_file(paths["config"], {})
    universe = cfg.get("universe") or []
    lookback_h = int(cfg.get("lookback_hours", 72))
    k_exit = int(cfg.get("k_exit", 6))
    sign = int(cfg.get("signal_sign", -1))
    rebal_hour = int(cfg.get("rebalance_hour_utc", 0))
    initial_balance = float(cfg.get("initial_balance", 5000.0))

    # Signals + mark-to-market positions
    sig_map = _plan_e_signals_from_cache(paths["cache"], universe, lookback_h, sign)

    # Build ranked list (same order as runner): desc by signal
    ranked = sorted(
        sig_map.items(), key=lambda kv: kv[1]["signal"], reverse=True,
    )
    rank_order = [s for s, _ in ranked]
    long_band = set(rank_order[:k_exit])
    short_band = set(rank_order[-k_exit:]) if k_exit else set()
    target_long_n = int(cfg.get("long_n", 3))
    target_short_n = int(cfg.get("short_n", 3))
    # Target longs/shorts for display (fresh, no hysteresis context)
    target_longs_fresh = rank_order[:target_long_n]
    target_shorts_fresh = rank_order[-target_short_n:][::-1] if target_short_n else []

    # Mark equity with current cache prices (cash + unrealized P&L)
    positions_raw = state.get("positions", {}) or {}
    cash = float(state.get("cash", initial_balance))
    live_equity = cash
    positions_view = []
    for sym, pos in positions_raw.items():
        last_price = sig_map.get(sym, {}).get("last_price")
        if last_price is None:
            last_price = _read_csv_last_close(paths["cache"] / f"{sym}_1H.csv")
        entry = float(pos.get("entry_price", 0.0) or 0.0)
        notional = float(pos.get("notional", 0.0) or 0.0)
        side = pos.get("side", "long")
        qty = (notional / entry) if entry > 0 else 0.0
        if last_price is None or entry <= 0:
            upnl = 0.0
        elif side == "long":
            upnl = qty * (last_price - entry)
        else:
            upnl = qty * (entry - last_price)
        upnl_pct = (upnl / notional * 100.0) if notional > 0 else 0.0
        live_equity += upnl
        positions_view.append({
            "symbol": sym,
            "side": side,
            "entry_price": entry,
            "last_price": last_price,
            "notional": notional,
            "unrealized_pnl": upnl,
            "unrealized_pnl_pct": upnl_pct,
            "entered_ts": pos.get("entered_ts"),
            "in_keep_band": (
                sym in long_band if side == "long" else sym in short_band
            ),
        })
    # Stable display order: longs by rank, then shorts
    positions_view.sort(key=lambda p: (
        0 if p["side"] == "long" else 1,
        rank_order.index(p["symbol"]) if p["symbol"] in rank_order else 99,
    ))

    # Trade log + equity curve
    trades = _read_jsonl_file(paths["trades"], 500)
    equity_curve = [
        {"ts": t.get("ts"), "equity": t.get("equity_after"),
         "cash": t.get("cash_after"), "fees": t.get("fees_paid", 0.0)}
        for t in trades if t.get("action") == "rebalance"
    ]
    total_fees = sum(t.get("fees_paid", 0.0) for t in trades
                     if t.get("action") == "rebalance")
    skips = [t for t in trades if t.get("action") == "skip"]

    # Ranked signal rows for UI (includes band membership + whether held)
    held = {p["symbol"]: p["side"] for p in positions_view}
    ranked_view = []
    for sym, s in ranked:
        ranked_view.append({
            "symbol": sym,
            "signal": s["signal"],
            "last_price": s["last_price"],
            "in_long_band": sym in long_band,
            "in_short_band": sym in short_band,
            "held": held.get(sym),  # "long" | "short" | None
        })

    svc_name = _instance_service_name(instance)
    service = _service_status(svc_name)
    # If the templated unit is missing (legacy deploy), fall back to
    # plan-e-runner so the UI still reports something.
    if service["status"] in ("unknown",) and instance == DEFAULT_INSTANCE:
        legacy = _service_status("plan-e-runner")
        if legacy["status"] not in ("unknown",):
            service = legacy
            svc_name = "plan-e-runner"
    started_ts = state.get("started_ts")

    # Total P&L since started
    pnl_total = live_equity - initial_balance
    pnl_pct = (pnl_total / initial_balance * 100.0) if initial_balance else 0.0

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "instance": instance,
        "service": {
            "name": svc_name,
            "status": service["status"],   # "active" | "inactive" | ...
            "uptime": service["uptime"],
            "mode": "paper",
        },
        "config": {
            "universe_size": len(universe),
            "lookback_hours": lookback_h,
            "k_exit": k_exit,
            "signal_sign": sign,
            "signal_label": "REV" if sign < 0 else "MOM",
            "long_n": target_long_n,
            "short_n": target_short_n,
            "leg_notional_pct": cfg.get("leg_notional_pct"),
            "fee_rate": cfg.get("fee_rate"),
            "slippage_rate": cfg.get("slippage_rate"),
            "rebalance_hour_utc": rebal_hour,
            "rebalance_interval_hours": int(cfg.get("rebalance_interval_hours", 24)),
            "initial_balance": initial_balance,
            "vol_halt": cfg.get("vol_halt", {"enabled": False}),
            "breadth_skip": cfg.get("breadth_skip", {"enabled": False}),
            "outlier_exclude": cfg.get("outlier_exclude", {"enabled": False}),
        },
        "account": {
            "cash": cash,
            "equity_persisted": state.get("equity"),
            "equity_live": live_equity,
            "pnl_total": pnl_total,
            "pnl_pct": pnl_pct,
            "rebalances_total": state.get("rebalances_total", 0),
            "skips_total": state.get("skips_total", 0),
            "last_rebalance_ts": state.get("last_rebalance_ts"),
            "next_rebalance_ts": _next_rebalance_ts(rebal_hour),
            "started_ts": started_ts,
            "total_fees_paid": total_fees,
            "funding_paid_total": state.get("funding_paid_total", 0.0),
            "last_funding_ts": state.get("last_funding_ts"),
            "skipped_min_notional_total": state.get("skipped_min_notional_total", 0),
            "circuit_breaker": {
                "state": state.get("cb_state", "normal"),
                "peak_equity": state.get("peak_equity"),
                "peak_equity_ts": state.get("peak_equity_ts"),
                "dd_pct": (
                    (state.get("peak_equity") - live_equity) / state.get("peak_equity")
                    if state.get("peak_equity") else 0.0
                ),
                "tripped_ts": state.get("cb_tripped_ts"),
                "tripped_equity": state.get("cb_tripped_equity"),
                "tripped_peak": state.get("cb_tripped_peak"),
                "halts_total": state.get("cb_halts_total", 0),
            },
        },
        "positions": positions_view,
        "ranked_signals": ranked_view,
        "equity_curve": equity_curve,
        "trades": trades[-30:],  # last 30 rebalance events, newest last
        "skips": skips[-10:],
    }


# ---------------------------------------------------------------------------
# Service control (start/stop Plan E runner)
# ---------------------------------------------------------------------------

_TEMPLATE_UNIT_RE = re.compile(r"^plan-e@[a-z0-9]{1,16}$")


def _unit_allowed(unit: str) -> bool:
    if unit in CONTROLLABLE_SERVICES:
        return True
    return bool(_TEMPLATE_UNIT_RE.match(unit))


def _do_service_control(unit: str, action: str) -> Dict[str, Any]:
    """Run `systemctl <action> <unit>` via sudo. Returns {ok, stdout, stderr}."""
    global _last_control_ts
    if not _unit_allowed(unit):
        return {"ok": False, "error": f"unit not allowed: {unit}"}
    if action not in ("start", "stop", "restart"):
        return {"ok": False, "error": f"action not allowed: {action}"}

    with _control_lock:
        now = time.time()
        if now - _last_control_ts < CONTROL_COOLDOWN_S:
            wait = CONTROL_COOLDOWN_S - (now - _last_control_ts)
            return {"ok": False, "error": f"cooldown: wait {wait:.1f}s"}
        _last_control_ts = now

    try:
        r = subprocess.run(
            ["sudo", "-n", "systemctl", action, unit],
            capture_output=True, text=True, timeout=15,
        )
        return {
            "ok": r.returncode == 0,
            "returncode": r.returncode,
            "stdout": r.stdout.strip(),
            "stderr": r.stderr.strip(),
            "action": action,
            "unit": unit,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

class DashboardHandler(SimpleHTTPRequestHandler):
    """Serves dashboard HTML and API endpoints."""

    def log_message(self, format, *args):
        # Suppress access logs to keep journal clean
        pass

    def _send_json(self, data: Any, status: int = 200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path: Path):
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            self.send_error(404, "Dashboard HTML not found")
            return
        # Inject the session token into the served HTML so the JS can attach
        # it to POST /api/control. Placeholder: {{DASHBOARD_TOKEN}}
        try:
            body = body.replace(b"{{DASHBOARD_TOKEN}}", DASHBOARD_TOKEN.encode())
        except Exception:
            pass
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


    def _parse_path(self) -> Tuple[str, Dict[str, str]]:
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        return u.path, q

    def _instance_from_query(self, q: Dict[str, str]) -> str:
        inst = q.get("instance", DEFAULT_INSTANCE)
        if INSTANCE_NAME_RE and not INSTANCE_NAME_RE.match(inst):
            raise ValueError(f"invalid instance: {inst}")
        return inst

    def do_GET(self):
        path, q = self._parse_path()

        if path == "/api/status":
            try:
                data = build_api_response()
                self._send_json(data)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/logs":
            logs = _read_jsonl_file(MEMORY_DIR / "trading-log.jsonl", 200)
            self._send_json(logs)

        elif path == "/api/trades":
            trades = _read_jsonl_file(MEMORY_DIR / "performance.jsonl", 500)
            self._send_json(trades)

        elif path == "/api/plan-e/instances":
            try:
                instances = _list_instances()
                summaries = []
                for inst in instances:
                    paths = _instance_paths(inst)
                    state = _read_json_file(paths["state"], {})
                    cfg = _read_json_file(paths["config"], {})
                    svc = _service_status(_instance_service_name(inst))
                    peak = state.get("peak_equity")
                    eq_now = state.get("equity") or 0.0
                    dd_pct = (
                        (peak - eq_now) / peak if peak and peak > 0 else 0.0
                    )
                    summaries.append({
                        "instance": inst,
                        "service": svc,
                        "equity": state.get("equity"),
                        "rebalances_total": state.get("rebalances_total", 0),
                        "skips_total": state.get("skips_total", 0),
                        "started_ts": state.get("started_ts"),
                        "last_rebalance_ts": state.get("last_rebalance_ts"),
                        "rebalance_interval_hours":
                            int(cfg.get("rebalance_interval_hours", 24)),
                        "leg_notional_pct": cfg.get("leg_notional_pct"),
                        "fee_rate": cfg.get("fee_rate"),
                        "slippage_rate": cfg.get("slippage_rate"),
                        "cb_state": state.get("cb_state", "normal"),
                        "cb_dd_pct": dd_pct,
                        "cb_halts_total": state.get("cb_halts_total", 0),
                        "funding_paid_total": state.get("funding_paid_total", 0.0),
                        "skipped_min_notional_total": state.get("skipped_min_notional_total", 0),
                        "flags": {
                            "vol_halt": bool(
                                (cfg.get("vol_halt") or {}).get("enabled")),
                            "breadth_skip": bool(
                                (cfg.get("breadth_skip") or {}).get("enabled")),
                            "outlier_exclude": bool(
                                (cfg.get("outlier_exclude") or {}).get("enabled")),
                            "stop_loss": bool(
                                (cfg.get("stop_loss") or {}).get("enabled")),
                        },
                    })
                self._send_json({"instances": summaries})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/plan-e/status":
            try:
                inst = self._instance_from_query(q)
                self._send_json(build_plan_e_response(inst))
            except ValueError as e:
                self._send_json({"error": str(e)}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/plan-e/trades":
            try:
                inst = self._instance_from_query(q)
                paths = _instance_paths(inst)
                self._send_json(_read_jsonl_file(paths["trades"], 500))
            except ValueError as e:
                self._send_json({"error": str(e)}, 400)

        elif path == "/api/plan-e/equity_curves":
            # Cross-instance equity curves for comparison chart.
            try:
                out = {}
                for inst in _list_instances():
                    paths = _instance_paths(inst)
                    trades = _read_jsonl_file(paths["trades"], 1000)
                    out[inst] = [
                        {"ts": t.get("ts"), "equity": t.get("equity_after")}
                        for t in trades if t.get("action") == "rebalance"
                    ]
                self._send_json(out)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/api/health":
            self._send_json({"ok": True, "ts": datetime.now(timezone.utc).isoformat()})

        elif path == "/" or path == "/dashboard":
            self._send_html(DASHBOARD_HTML)

        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/api/control":
            self.send_error(404)
            return

        # Token check
        token = self.headers.get("X-Dashboard-Token", "")
        if not secrets.compare_digest(token, DASHBOARD_TOKEN):
            self._send_json({"ok": False, "error": "invalid token"}, 403)
            return

        # Body
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception as e:
            self._send_json({"ok": False, "error": f"bad json: {e}"}, 400)
            return

        unit = payload.get("unit", "plan-e-runner")
        action = payload.get("action", "")
        result = _do_service_control(unit, action)
        self._send_json(result, 200 if result.get("ok") else 400)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Dashboard-Token")
        self.end_headers()


def main():
    port = int(os.environ.get("DASHBOARD_PORT", API_PORT))
    server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
    server.daemon_threads = True
    print(f"Dashboard API running on http://0.0.0.0:{port}")
    print(f"  Bot dir:   {BOT_DIR}")
    print(f"  Memory:    {MEMORY_DIR}")
    print(f"  Dashboard: {DASHBOARD_HTML}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
