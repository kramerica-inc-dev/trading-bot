#!/usr/bin/env python3
"""
Trading Bot Dashboard API Server
Reads bot state files from memory/ and serves them as JSON.
Serves a static HTML dashboard on port 8080.
"""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_PORT = 8080
BOT_DIR = Path(os.environ.get("BOT_DIR", "/opt/trading-bot"))
MEMORY_DIR = BOT_DIR / "memory"
CONFIG_PATH = BOT_DIR / "config.json"
DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"

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
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


    def do_GET(self):
        if self.path == "/api/status":
            try:
                data = build_api_response()
                self._send_json(data)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif self.path == "/api/logs":
            logs = _read_jsonl_file(MEMORY_DIR / "trading-log.jsonl", 200)
            self._send_json(logs)

        elif self.path == "/api/trades":
            trades = _read_jsonl_file(MEMORY_DIR / "performance.jsonl", 500)
            self._send_json(trades)

        elif self.path == "/api/health":
            self._send_json({"ok": True, "ts": datetime.now(timezone.utc).isoformat()})

        elif self.path == "/" or self.path == "/dashboard":
            self._send_html(DASHBOARD_HTML)

        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def main():
    port = int(os.environ.get("DASHBOARD_PORT", API_PORT))
    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
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
