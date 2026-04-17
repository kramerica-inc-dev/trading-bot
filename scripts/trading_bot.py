#!/usr/bin/env python3
"""
Multi-Exchange Trading Bot - v2.3
Supports: Blofin, Coinbase (via exchange adapters)
Features: regime-aware strategy, private order WS, live profiles,
          time-based exits, performance tracking, reconciliation evidence,
          circuit breaker, server-side TP/SL, pending order lifecycle, WebSocket market data
"""

import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from config_utils import ConfigError, load_and_validate_config, generate_config_report
from exchange_adapter import create_exchange_adapter
from regime_timeframe import RegimeTimeframeResolver
from risk_utils import PositionSizingResult, calculate_risk_position_size
from trading_strategy import Signal, create_strategy

try:
    from market_data_stream import BlofinMarketDataStream
    _HAS_MARKET_STREAM = True
except ImportError:
    _HAS_MARKET_STREAM = False

try:
    from private_order_stream import BlofinPrivateOrderStream
    _HAS_ORDER_STREAM = True
except ImportError:
    _HAS_ORDER_STREAM = False

try:
    from live_profile_manager import refresh_live_profile
    _HAS_PROFILE_MANAGER = True
except ImportError:
    _HAS_PROFILE_MANAGER = False


class ReconciliationError(RuntimeError):
    """Raised when startup reconciliation finds unrecoverable state mismatch."""
    pass


class TradingBot:
    """Main trading bot orchestrator - Multi-Exchange Support"""

    def __init__(self, config_path: str = "config.json",
                 force_reconcile: bool = False):
        self._force_reconcile = force_reconcile
        requested_config = Path(config_path)
        self.base_dir = (requested_config.resolve().parent
                         if requested_config.is_absolute()
                         else Path(__file__).parent.parent)
        config_ref = (requested_config.name
                      if requested_config.is_absolute() else config_path)

        self.memory_dir = self.base_dir / "memory"
        self.memory_dir.mkdir(exist_ok=True)
        self.log_file = self.memory_dir / "trading-log.jsonl"
        self.positions_file = self.memory_dir / "positions.json"
        self.pending_orders_file = self.memory_dir / "pending-orders.json"
        self.state_file = self.memory_dir / "runtime-state.json"
        self.reconciliation_dir = self.memory_dir / "reconciliation"
        self.reconciliation_dir.mkdir(exist_ok=True)

        self.config = load_and_validate_config(config_ref, self.base_dir)
        self.exchange_name = self.config["exchange"]
        self.exchange_config = self.config.get(self.exchange_name, {})
        self.api = create_exchange_adapter(self.exchange_name, self.exchange_config)

        self.inst_id = self.config["trading_pair"]
        self.risk_cfg = self.config["risk"]
        self.trading = self.config["trading"]
        self.protection = self.config.get("protection", {})
        self.breaker_cfg = self.config.get("circuit_breaker", {})
        self.market_data_cfg = self.config.get("market_data", {})
        self.execution_cfg = self.config.get("execution", {})
        self.trailing_cfg = self.config.get("trailing_stop", {})
        self.risk_per_trade = float(self.risk_cfg["risk_per_trade_pct"])
        self.margin_mode = self.risk_cfg.get("margin_mode", "isolated")
        self.position_side_mode = self.trading.get("position_side_mode", "hedge")

        self.dry_run = bool(self.config.get("dry_run", True))

        self.active_positions = self._load_json(self.positions_file, [])
        self.pending_orders = self._load_json(self.pending_orders_file, [])
        self.state = self._load_state()
        self.running = False
        self._protection_warnings_emitted = set()
        self.market_stream = None
        self._state_lock = threading.Lock()

        # Performance tracking
        self.performance_file = self.memory_dir / "performance.jsonl"

        # Live profile support
        self.profile_report_dir = self.memory_dir / "profile-refresh-reports"
        self.profile_report_dir.mkdir(exist_ok=True)
        self.parameter_selector_cfg = dict(self.config.get("parameter_selector", {}))
        self._base_strategy_cfg = dict(self.config.get("strategy", {}))
        self._base_strategy_cfg.setdefault("base_timeframe", self.config.get("timeframe", "5m"))
        self._last_profile_refresh_ts = 0.0

        # Private order stream
        self.order_stream = None
        self._last_private_update_sync_ts = 0.0

        # Build strategy (with live profile overlay if enabled)
        self.strategy = self._build_strategy_with_live_profiles(self._base_strategy_cfg)

        # Dynamic timeframe resolver (step 3, v2.6). Disabled by default,
        # in which case it is effectively a no-op and the bot uses the
        # static top-level "timeframe" from config.
        self.regime_tf_resolver = RegimeTimeframeResolver(
            self.config.get("regime_timeframes", {}))
        self._static_timeframe = str(self.config.get("timeframe", "5m"))
        self._static_interval = None  # set by start(interval=...)

        # Per-timeframe calibrated parameter profiles (step 5, v2.7).
        # If a profile file exists, load it into the strategy. The
        # strategy applies the profile matching the currently active TF
        # each cycle via set_active_timeframe().
        self._maybe_load_timeframe_profiles()

        if hasattr(self.strategy, 'min_confidence'):
            self.min_confidence = self.strategy.min_confidence
        else:
            self.min_confidence = float(self.config.get("min_confidence", 0.6))

        self._log("info", f"🤖 Bot initialized: {self.strategy.name}")
        self._log("info", f"Exchange: {self.exchange_name.upper()}")
        self._log("info", f"Mode: {'🔵 DRY RUN' if self.dry_run else '🔴 LIVE TRADING'}")
        self._log("info", f"Min confidence: {self.min_confidence:.0%}")
        self._log("info", f"Risk per trade: {self.risk_per_trade:.1f}%")
        self._log("info", "Risk sizing: stop-loss based (risk_utils)")
        if self.protection.get("use_server_side_tpsl"):
            self._log("info", "Server-side TP/SL: enabled")
        if self.breaker_cfg.get("enabled"):
            self._log("info", "Circuit breaker: enabled")
        if self.trailing_cfg.get("enabled"):
            self._log("info", "Trailing stop: enabled"
                      f" (breakeven={self.trailing_cfg.get('breakeven_enabled')},"
                      f" trail={self.trailing_cfg.get('trail_enabled')})")
        if self.active_positions:
            self._log("info", f"📦 Loaded {len(self.active_positions)} active positions from disk")
        if self.pending_orders:
            self._log("info", f"📦 Loaded {len(self.pending_orders)} pending orders from disk")

        if (_HAS_MARKET_STREAM
                and self.market_data_cfg.get("use_websocket")
                and self.exchange_name == "blofin"):
            self._start_market_stream()

        if (_HAS_ORDER_STREAM
                and self.execution_cfg.get("use_private_order_websocket")
                and self.exchange_name == "blofin"
                and not self.dry_run):
            self._start_order_stream()

        if not self.dry_run:
            self._reconcile_startup()

    # ==================== DYNAMIC TIMEFRAME ====================

    def _maybe_load_timeframe_profiles(self) -> None:
        """Load calibrated per-timeframe profiles into the strategy if available.

        Looks up timeframe_profiles.path in config; missing or empty
        files are silently ignored. The strategy applies the profile
        matching the currently active TF each cycle via
        set_active_timeframe().
        """
        tf_profile_cfg = self.config.get("timeframe_profiles", {}) or {}
        if not tf_profile_cfg.get("enabled", False):
            return
        path = tf_profile_cfg.get("path", "memory/timeframe_profiles.json")
        full_path = Path(path)
        if not full_path.is_absolute():
            full_path = self.base_dir / full_path
        if not hasattr(self.strategy, "load_timeframe_profiles"):
            return
        loaded = self.strategy.load_timeframe_profiles(str(full_path))
        if loaded > 0:
            self._log("info",
                f"\U0001f4cf Loaded {loaded} calibrated timeframe profile(s) "
                f"from {full_path.name}")
        else:
            self._log("warning",
                f"timeframe_profiles.enabled=true but no profiles loaded "
                f"from {full_path} — run backtest/calibrate_per_timeframe.py")

    def _active_timeframe(self) -> str:
        """Return the timeframe the bot should currently fetch candles for.

        If regime_timeframes is disabled, returns the static config timeframe.
        Otherwise returns the resolver's active timeframe, which reflects
        the most recent regime (with hysteresis applied).
        """
        if not self.regime_tf_resolver.enabled:
            return self._static_timeframe
        return self.regime_tf_resolver.state.active_timeframe

    def _active_check_interval(self) -> int:
        """Return the sleep interval (seconds) between main-loop cycles.

        Falls back to the interval passed to start() when dynamic
        timeframe selection is disabled.
        """
        if not self.regime_tf_resolver.enabled:
            return int(self._static_interval or 60)
        return int(self.regime_tf_resolver.state.active_check_interval)

    def _apply_regime_to_timeframe(self, regime: str) -> None:
        """Feed the detected regime into the resolver and log transitions."""
        if not self.regime_tf_resolver.enabled:
            return
        switched, reason = self.regime_tf_resolver.update(regime)
        if switched:
            snap = self.regime_tf_resolver.state.snapshot()
            self._log("info",
                f"\U0001f504 Timeframe switch: -> {snap['active_timeframe']}"
                f" (regime={snap['active_regime']},"
                f" interval={snap['active_check_interval']}s,"
                f" reason={reason})",
                snap)

    # ==================== LOGGING ====================

    def _log(self, level: str, message: str, data: Optional[Dict] = None):
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level.upper(),
            "message": message,
        }
        if data is not None:
            log_entry["data"] = data

        with open(self.log_file, "a") as f:
            f.write(json.dumps(log_entry) + "\n")

        color_map = {
            "info": "\033[0m", "success": "\033[92m",
            "warning": "\033[93m", "error": "\033[91m",
        }
        color = color_map.get(level.lower(), "\033[0m")
        print(f"{color}[{level.upper()}] {message}\033[0m")

    # ==================== PERSISTENCE ====================

    def _load_json(self, path: Path, default):
        if not path.exists():
            return default
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, type(default)) else default
        except Exception as e:
            self._log("error", f"Failed to load {path.name}: {e}")
            return default

    def _save_json(self, path: Path, data) -> None:
        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(path)
        except Exception as e:
            self._log("error", f"Failed to save {path.name}: {e}")

    def _save_positions(self) -> None:
        self._save_json(self.positions_file, self.active_positions)

    def _save_pending_orders(self) -> None:
        self._save_json(self.pending_orders_file, self.pending_orders)

    def _today(self) -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _default_state(self) -> Dict:
        return {
            "date": self._today(),
            "start_balance": None, "peak_balance": None, "last_balance": None,
            "loss_streak": 0, "error_streak": 0, "last_price": None,
            "circuit_breaker": {
                "active": False, "reason": "", "tripped_at": None, "until": None,
            },
        }

    def _load_state(self) -> Dict:
        if not self.state_file.exists():
            state = self._default_state()
            self._save_state(state)
            return state
        try:
            state = json.loads(self.state_file.read_text())
        except Exception:
            state = self._default_state()
        self._roll_state_if_needed(state)
        self._save_state(state)
        return state

    def _save_state(self, state: Optional[Dict] = None) -> None:
        payload = self.state if state is None else state
        try:
            tmp = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(self.state_file)
        except Exception as e:
            self._log("error", f"Failed to save state: {e}")

    def _roll_state_if_needed(self, state: Dict) -> None:
        today = self._today()
        if state.get("date") == today:
            return
        last_balance = state.get("last_balance")
        fresh = self._default_state()
        if last_balance is not None:
            fresh["start_balance"] = last_balance
            fresh["peak_balance"] = last_balance
            fresh["last_balance"] = last_balance
        state.clear()
        state.update(fresh)

    def _backup_snapshot(self, reason: str, payload: Dict) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.reconciliation_dir / f"{ts}-{reason}.json"
        path.write_text(json.dumps(payload, indent=2))
        self._rotate_reconciliation_snapshots()
        return path

    def _rotate_reconciliation_snapshots(self, max_files: int = 200) -> None:
        try:
            files = sorted(self.reconciliation_dir.glob("*.json"))
            if len(files) <= max_files:
                return
            for f in files[:-max_files]:
                f.unlink(missing_ok=True)
        except Exception:
            pass

    def _rotate_log_file(self, path: Path, max_bytes: int = 10_000_000) -> None:
        try:
            if not path.exists() or path.stat().st_size < max_bytes:
                return
            rotated = path.with_suffix(path.suffix + ".old")
            if rotated.exists():
                rotated.unlink()
            path.rename(rotated)
        except Exception:
            pass

    # ==================== WEBSOCKET ====================

    def _start_market_stream(self) -> None:
        if (not _HAS_MARKET_STREAM or self.market_stream is not None
                or self.exchange_name != "blofin"):
            return
        try:
            self.market_stream = BlofinMarketDataStream(
                inst_id=self.inst_id,
                timeframe=self.config.get("timeframe", "5m"),
                demo_mode=bool(self.exchange_config.get("demo_mode", False)),
                logger=self._log,
                max_candles=int(self.market_data_cfg.get("max_cached_candles", 200)),
                ping_interval=int(self.market_data_cfg.get("ping_interval", 20)),
                ping_timeout=int(self.market_data_cfg.get("ping_timeout", 10)),
                reconnect_delay=int(self.market_data_cfg.get("reconnect_delay_seconds", 5)),
            )
            self.market_stream.start()
            self._log("info", "WebSocket market data stream started")
        except Exception as e:
            self.market_stream = None
            self._log("warning", f"WebSocket start failed, using REST: {e}")

    def _stop_market_stream(self) -> None:
        if self.market_stream is not None:
            self.market_stream.stop()
            self.market_stream = None

    # ==================== PRIVATE ORDER STREAM ====================

    def _start_order_stream(self) -> None:
        if self.order_stream is not None or self.exchange_name != "blofin":
            return
        creds = self.exchange_config or {}
        if not all(creds.get(k) for k in ("api_key", "api_secret", "passphrase")):
            self._log("warning",
                      "Private order WS disabled: credentials incomplete")
            return
        try:
            self.order_stream = BlofinPrivateOrderStream(
                api_key=creds["api_key"],
                api_secret=creds["api_secret"],
                passphrase=creds["passphrase"],
                inst_id=self.inst_id,
                demo_mode=bool(creds.get("demo_mode", False)),
                logger=self._log,
                on_order_update=self._handle_private_order_update,
                ping_interval=int(
                    self.market_data_cfg.get("ping_interval", 20)),
                ping_timeout=int(
                    self.market_data_cfg.get("ping_timeout", 10)),
                reconnect_delay=int(
                    self.market_data_cfg.get("reconnect_delay_seconds", 5)),
            )
            self.order_stream.start()
        except Exception as exc:
            self.order_stream = None
            self._log("warning",
                f"Private order WS failed, falling back to REST: {exc}")

    def _stop_order_stream(self) -> None:
        if self.order_stream is not None:
            self.order_stream.stop()
            self.order_stream = None

    def _find_pending_order(self, update: Dict) -> Optional[Dict]:
        order_id = str(update.get("orderId") or "")
        client_order_id = str(update.get("clientOrderId") or "")
        for pending in list(self.pending_orders):
            if (order_id
                    and str(pending.get("order_id") or "") == order_id):
                return pending
            if (client_order_id
                    and str(pending.get("client_order_id") or "")
                    == client_order_id):
                return pending
        return None

    def _handle_private_order_update(self, update: Dict) -> None:
        with self._state_lock:
            pending = self._find_pending_order(update)
            if not pending:
                return
            state = str(
                update.get("state") or pending.get("state") or "").lower()
            filled_size = float(
                update.get("filledSize")
                or pending.get("filled_size") or 0)
            average_price = float(
                update.get("averagePrice")
                or pending.get("average_price")
                or pending.get("entry_price") or 0)
            pending["last_ws_update"] = (
                datetime.now(timezone.utc).isoformat())
            pending["ws_channel"] = update.get("channel")
            if state in {"live", "partially_filled"}:
                self._apply_partial_fill(
                    pending, filled_size=filled_size,
                    average_price=average_price, state=state)
            elif state:
                self._finalize_pending_order(pending, {
                    "filledSize": filled_size,
                    "averagePrice": average_price,
                    "state": state,
                })
        self._maybe_sync_after_private_update(
            reason=f"private_order_update:{state or 'unknown'}")

    def _maybe_sync_after_private_update(
            self, reason: str = "private_order_update") -> None:
        if (self.dry_run
                or not self.execution_cfg.get(
                    "sync_positions_after_private_update", True)):
            return
        now = time.time()
        cooldown = max(float(self.execution_cfg.get(
            "private_update_sync_cooldown_seconds", 3)), 0.0)
        if (now - self._last_private_update_sync_ts) < cooldown:
            return
        self._last_private_update_sync_ts = now
        try:
            current_price = None
            if self.market_stream is not None:
                snapshot = self.market_stream.get_snapshot()
                if snapshot:
                    current_price = float(snapshot[0])
            if current_price is None:
                ticker = self.api.get_ticker(self.inst_id)
                if (ticker.get("code") == "0" and ticker.get("data")):
                    current_price = float(ticker["data"][0]["last"])
            if current_price is not None:
                with self._state_lock:
                    self._sync_exchange_state(current_price)
                self._log("info",
                    f"Fast exchange sync completed after {reason}")
        except Exception as exc:
            self._log("warning",
                f"Fast exchange sync failed after {reason}: {exc}")

    # ==================== EXCHANGE NORMALIZATION ====================

    def _position_side_for_signal(self, action: str) -> str:
        if self.position_side_mode == "hedge":
            return "long" if action == "buy" else "short"
        return "net"

    def _position_identity(self, pos: Dict) -> Tuple[str, str]:
        return (
            str(pos.get("side") or ""),
            str(pos.get("position_side")
                or self._position_side_for_signal(pos.get("side", "buy"))),
        )

    def _positions_match(self, left: Dict, right: Dict) -> bool:
        return self._position_identity(left) == self._position_identity(right)

    def _normalize_remote_position(self, raw: Dict) -> Optional[Dict]:
        if str(raw.get("instId", self.inst_id)) != self.inst_id:
            return None
        try:
            size = abs(float(raw.get("positions", 0)))
        except (TypeError, ValueError):
            size = 0.0
        if size <= 0:
            return None

        raw_pos = raw.get("positions")
        position_side = str(raw.get("positionSide", "")).lower()
        if position_side == "long":
            side = "buy"
        elif position_side == "short":
            side = "sell"
        else:
            try:
                side = "buy" if float(raw_pos) > 0 else "sell"
            except (TypeError, ValueError):
                side = "buy"
            position_side = self._position_side_for_signal(side)

        entry_price = float(raw.get("averagePrice")
                            or raw.get("averageOpenPrice") or 0)
        ts_ms = raw.get("updateTime") or raw.get("createTime")
        timestamp = datetime.now(timezone.utc).isoformat()
        if ts_ms:
            try:
                timestamp = datetime.fromtimestamp(
                    int(ts_ms) / 1000.0, tz=timezone.utc).isoformat()
            except Exception:
                pass

        return {
            "source": "exchange", "inst_id": self.inst_id,
            "position_id": raw.get("positionId"),
            "side": side, "position_side": position_side,
            "position_type": "LONG" if side == "buy" else "SHORT",
            "entry_price": entry_price, "size": round(size, 8),
            "stop_loss": None, "take_profit": None,
            "server_side_tpsl": False, "timestamp": timestamp,
        }

    def _upsert_active_position(self, position: Dict) -> None:
        for idx, existing in enumerate(self.active_positions):
            if self._positions_match(existing, position):
                merged = dict(existing)
                merged.update(position)
                self.active_positions[idx] = merged
                self._save_positions()
                return
        self.active_positions.append(position)
        self._save_positions()

    def _fetch_exchange_positions(self) -> List[Dict]:
        response = self.api.get_positions(self.inst_id)
        if response.get("code") == "unsupported":
            return []
        if response.get("code") != "0":
            self._log("warning",
                      f"Failed to fetch positions: {response.get('msg')}")
            return []
        results = []
        for raw in response.get("data", []) or []:
            norm = self._normalize_remote_position(raw)
            if norm:
                results.append(norm)
        return results

    def _fetch_active_orders(self) -> List[Dict]:
        response = self.api.get_active_orders(self.inst_id)
        if response.get("code") in {"unsupported", None}:
            return []
        if response.get("code") != "0":
            return []
        return response.get("data", []) or []

    def _fetch_active_tpsl_orders(self) -> List[Dict]:
        response = self.api.get_active_tpsl_orders(self.inst_id)
        if response.get("code") in {"unsupported", None}:
            return []
        if response.get("code") != "0":
            return []
        return response.get("data", []) or []

    def _match_tpsl(self, position: Dict,
                    tpsl_orders: List[Dict]) -> Optional[Dict]:
        expected = (position.get("position_side")
                    or self._position_side_for_signal(position["side"]))
        for order in tpsl_orders:
            if str(order.get("instId", self.inst_id)) != self.inst_id:
                continue
            order_side = str(order.get("positionSide", "")).lower() or expected
            if order_side != expected:
                continue
            try:
                o_size = abs(float(order.get("size") or 0))
                p_size = abs(float(position.get("size") or 0))
            except Exception:
                continue
            if (o_size and p_size
                    and abs(o_size - p_size) > max(0.2, p_size * 0.05)):
                continue
            return order
        return None

    def _merge_remote_with_local(self, remote: List[Dict],
                                  local: List[Dict],
                                  tpsl: Optional[List[Dict]] = None) -> List[Dict]:
        merged = []
        unmatched = list(local)
        tpsl = tpsl or []

        for rpos in remote:
            prot = self._match_tpsl(rpos, tpsl)
            if prot:
                sl = prot.get("slTriggerPrice")
                tp = prot.get("tpTriggerPrice")
                if sl not in (None, ""):
                    rpos["stop_loss"] = float(sl)
                if tp not in (None, ""):
                    rpos["take_profit"] = float(tp)
                rpos["server_side_tpsl"] = True
                rpos["tpsl_id"] = prot.get("tpslId")

            match = None
            for lpos in unmatched:
                if self._positions_match(lpos, rpos):
                    match = lpos
                    break

            if match:
                if rpos.get("stop_loss") is None:
                    rpos["stop_loss"] = match.get("stop_loss")
                if rpos.get("take_profit") is None:
                    rpos["take_profit"] = match.get("take_profit")
                rpos["order_id"] = match.get("order_id")
                rpos["client_order_id"] = match.get("client_order_id")
                rpos["timestamp"] = match.get("timestamp", rpos["timestamp"])
                rpos["risk"] = match.get("risk")
                rpos["server_side_tpsl"] = bool(
                    rpos.get("server_side_tpsl") or match.get("server_side_tpsl"))
                rpos["source"] = "exchange+local"
                unmatched.remove(match)
            merged.append(rpos)

        if unmatched:
            self._backup_snapshot("stale-local-positions", {
                "reason": "local_without_exchange_match",
                "local": unmatched, "exchange": remote,
            })
            self._log("warning",
                      f"Dropped {len(unmatched)} stale local position(s)")

        return merged

    # ==================== RECONCILIATION (fail-closed) ====================

    def _reconcile_startup(self):
        """Check exchange vs local state at startup.

        Raises ReconciliationError on critical mismatches unless
        --force-reconcile is active.
        """
        try:
            local = list(self.active_positions)

            # Include pending order metadata
            for pending in self.pending_orders:
                if str(pending.get("inst_id", self.inst_id)) != self.inst_id:
                    continue
                side = pending.get("side")
                if side not in {"buy", "sell"}:
                    continue
                candidate = {
                    "source": "pending-order", "side": side,
                    "position_side": (pending.get("position_side")
                                      or self._position_side_for_signal(side)),
                    "position_type": "LONG" if side == "buy" else "SHORT",
                    "entry_price": float(pending.get("entry_price") or 0),
                    "size": float(pending.get("filled_size")
                                  or pending.get("size") or 0),
                    "stop_loss": pending.get("stop_loss"),
                    "take_profit": pending.get("take_profit"),
                    "server_side_tpsl": bool(pending.get("server_side_tpsl")),
                    "timestamp": pending.get("timestamp"),
                }
                if not any(self._positions_match(candidate, lp) for lp in local):
                    local.append(candidate)

            remote = self._fetch_exchange_positions()
            active_orders = self._fetch_active_orders()
            active_tpsl = self._fetch_active_tpsl_orders()

            snapshot = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "inst_id": self.inst_id,
                "local_positions": local,
                "pending_orders": list(self.pending_orders),
                "exchange_positions": remote,
                "active_orders": active_orders,
                "active_tpsl_orders": active_tpsl,
            }
            self._backup_snapshot("startup-reconciliation", snapshot)

            if not remote and not local:
                self.active_positions = []
                self._save_positions()
                self._log("info", "Reconciliation: clean state, no open positions")
                return

            if remote and not local:
                msg = (f"Exchange has {len(remote)} open position(s) but no "
                       "local SL/TP metadata — manual review required")
                self._log("error", msg)
                self._reconciliation_fail(msg, snapshot)

            if local and not remote:
                msg = ("Local state has positions but exchange has none "
                       "— state file stale")
                self._log("error", msg)
                self._reconciliation_fail(msg, snapshot)

            self.active_positions = self._merge_remote_with_local(
                remote, local, active_tpsl)
            self._save_positions()

            missing = [p for p in self.active_positions
                       if not p.get("stop_loss") or not p.get("take_profit")]
            if missing and self.protection.get("require_server_side_tpsl"):
                msg = (f"{len(missing)} position(s) missing SL/TP metadata "
                       "and require_server_side_tpsl is True")
                self._log("error", msg)
                self._reconciliation_fail(msg, snapshot)
            elif missing:
                self._log("warning",
                    f"{len(missing)} position(s) missing SL/TP metadata")

            if self.protection.get("require_server_side_tpsl"):
                unprotected = [p for p in self.active_positions
                               if not p.get("server_side_tpsl")]
                if unprotected:
                    self._log("warning",
                        f"{len(unprotected)} position(s) without "
                        "server-side TP/SL (will retry)")

            self._log("success",
                f"Reconciliation: {len(self.active_positions)} position(s) synced")

        except ReconciliationError:
            raise
        except Exception as e:
            self._log("warning", f"Startup reconciliation failed: {e}")

    def _reconciliation_fail(self, msg: str, snapshot: Dict) -> None:
        """Raise ReconciliationError unless --force-reconcile is active."""
        if self._force_reconcile:
            self._log("warning",
                f"FORCE-RECONCILE: continuing despite: {msg}")
            return
        raise ReconciliationError(msg)

    # ==================== CIRCUIT BREAKER ====================

    def _trip_circuit_breaker(self, reason: str) -> None:
        if not self.breaker_cfg.get("enabled", False):
            self._log("warning",
                      f"Circuit breaker would trip ({reason}) but is disabled")
            return
        until = (datetime.now(timezone.utc)
                 + timedelta(minutes=int(
                     self.breaker_cfg.get("cooldown_minutes", 30))))
        cb = self.state.setdefault("circuit_breaker", {})
        cb.update({
            "active": True, "reason": reason,
            "tripped_at": datetime.now(timezone.utc).isoformat(),
            "until": until.isoformat(),
        })
        self._save_state()
        self._log("error", f"🚨 Circuit breaker tripped: {reason}")

    def _clear_circuit_breaker_if_expired(self) -> None:
        cb = self.state.setdefault("circuit_breaker", {})
        if not cb.get("active") or not cb.get("until"):
            return
        try:
            expires = datetime.fromisoformat(cb["until"])
        except ValueError:
            cb.update({"active": False, "reason": "", "until": None})
            self._save_state()
            return
        if datetime.now(timezone.utc) >= expires:
            cb.update({"active": False, "reason": "", "until": None})
            self.state["error_streak"] = 0
            self._save_state()
            self._log("warning", "Circuit breaker cooldown elapsed")

    def _breaker_active(self) -> bool:
        if not self.breaker_cfg.get("enabled", False):
            return False
        self._clear_circuit_breaker_if_expired()
        return bool(self.state.get("circuit_breaker", {}).get("active"))

    def _record_error(self, message: str) -> None:
        self.state["error_streak"] = int(self.state.get("error_streak", 0)) + 1
        self._save_state()
        max_err = int(self.breaker_cfg.get("max_consecutive_errors", 5))
        if self.state["error_streak"] >= max_err:
            self._trip_circuit_breaker(
                f"{max_err} consecutive cycle/API errors")
        self._log("error", message)

    def _reset_error_streak(self) -> None:
        if self.state.get("error_streak"):
            self.state["error_streak"] = 0
            self._save_state()

    def _update_balance_state(self, balance: float) -> None:
        self._roll_state_if_needed(self.state)
        if self.state.get("start_balance") is None:
            self.state["start_balance"] = balance
        if (self.state.get("peak_balance") is None
                or balance > float(self.state["peak_balance"])):
            self.state["peak_balance"] = balance
        self.state["last_balance"] = balance

        peak = float(self.state.get("peak_balance") or balance)
        if peak > 0:
            dd = max(0.0, (peak - balance) / peak * 100.0)
            limit = float(self.breaker_cfg.get("daily_loss_limit_pct", 5.0))
            if dd >= limit:
                self._trip_circuit_breaker(
                    f"daily loss limit ({dd:.2f}% >= {limit:.2f}%)")
        self._save_state()

    def _check_price_jump(self, current_price: float) -> None:
        prev = self.state.get("last_price")
        self.state["last_price"] = current_price
        self._save_state()
        if prev in (None, 0):
            return
        move = abs(current_price - float(prev)) / float(prev) * 100.0
        limit = float(self.breaker_cfg.get("max_price_move_pct_per_cycle", 5.0))
        if move >= limit:
            self._trip_circuit_breaker(
                f"price jump {move:.2f}% >= {limit:.2f}%")

    def _append_trade_outcome(self, pnl: float, reason: str) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "inst_id": self.inst_id,
            "pnl": float(pnl),
            "reason": reason,
        }
        with open(self.performance_file, "a") as fh:
            fh.write(json.dumps(entry) + "\n")

    def get_recent_trade_outcomes(self, limit: int = 20) -> List[Dict]:
        if not self.performance_file.exists():
            return []
        rows: List[Dict] = []
        try:
            for line in self.performance_file.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except Exception:
            return []
        return rows[-max(int(limit), 0):]

    def _register_trade_outcome(self, pnl: float, reason: str) -> None:
        self._append_trade_outcome(pnl, reason)
        if pnl < 0:
            self.state["loss_streak"] = int(
                self.state.get("loss_streak", 0)) + 1
            self._log("warning", f"Trade closed negative ({pnl:.2f}): {reason}")
        else:
            self.state["loss_streak"] = 0
            self._log("success", f"Trade closed ({pnl:+.2f}): {reason}")
        max_losses = int(self.breaker_cfg.get("max_consecutive_losses", 3))
        if int(self.state.get("loss_streak", 0)) >= max_losses:
            self._trip_circuit_breaker(
                f"loss streak reached {self.state['loss_streak']} trades")
        self._save_state()

    # ==================== PENDING ORDER LIFECYCLE ====================

    def _fetch_order_detail(self, *, order_id=None,
                            client_order_id=None) -> Optional[Dict]:
        resp = self.api.get_order_detail(
            self.inst_id, order_id=order_id,
            client_order_id=client_order_id)
        if resp.get("code") == "unsupported":
            return None
        if resp.get("code") != "0":
            return None
        data = resp.get("data")
        if isinstance(data, list):
            return data[0] if data else None
        return data

    def _normalize_order_fill(self, order: Dict,
                              pending: Dict) -> Tuple[float, float, str]:
        filled = float(order.get("filledSize") or 0)
        avg = float(order.get("averagePrice")
                    or pending.get("entry_price") or 0)
        state = str(order.get("state")
                    or pending.get("state") or "").lower()
        return filled, avg, state

    def _pending_to_position(self, pending: Dict, *, filled_size: float,
                              average_price: float, state: str) -> Dict:
        return {
            "source": "pending-order", "inst_id": self.inst_id,
            "order_id": pending.get("order_id"),
            "client_order_id": pending.get("client_order_id"),
            "side": pending["side"],
            "position_side": (pending.get("position_side")
                              or self._position_side_for_signal(pending["side"])),
            "position_type": "LONG" if pending["side"] == "buy" else "SHORT",
            "entry_price": average_price or float(pending.get("entry_price") or 0),
            "size": round(filled_size, 8),
            "stop_loss": pending.get("stop_loss"),
            "take_profit": pending.get("take_profit"),
            "server_side_tpsl": bool(pending.get("server_side_tpsl")),
            "timestamp": (pending.get("timestamp")
                          or datetime.now(timezone.utc).isoformat()),
            "opened_on_timeframe": pending.get("opened_on_timeframe"),
            "risk": pending.get("risk"),
            "order_state": state,
            "max_hold_bars": pending.get("max_hold_bars"),
            "stale_trade_bars": pending.get("stale_trade_bars"),
            "atr": pending.get("atr"),
            "regime": pending.get("regime"),
            "partial_fill": state in {
                "partially_filled", "partially_canceled",
                "partially_cancelled"},
        }

    def _apply_partial_fill(self, pending: Dict, *, filled_size: float,
                             average_price: float, state: str) -> None:
        pending["filled_size"] = round(filled_size, 8)
        pending["average_price"] = average_price
        pending["state"] = state
        self._save_pending_orders()

        if filled_size <= 0:
            return

        position = self._pending_to_position(
            pending, filled_size=filled_size,
            average_price=average_price, state=state)

        # Verify position size matches actual filled size
        if abs(position["size"] - filled_size) > 1e-8:
            self._log("warning",
                f"Position size mismatch: position={position['size']}, "
                f"filled={filled_size} — correcting")
            position["size"] = round(filled_size, 8)

        self._upsert_active_position(position)
        self._log("info",
            f"Entry order update: state={state}, "
            f"filled={filled_size}/{pending.get('size')}")

        if (self.protection.get("use_server_side_tpsl")
                and not position.get("server_side_tpsl")):
            position["server_side_tpsl"] = self._ensure_server_side_tpsl(
                position)
            self._upsert_active_position(position)
            pending["server_side_tpsl"] = position["server_side_tpsl"]
            self._save_pending_orders()
            if not position["server_side_tpsl"]:
                if self.protection.get("require_server_side_tpsl"):
                    self._log("error",
                        "CRITICAL: Server-side TP/SL failed and "
                        "require_server_side_tpsl is True — "
                        "closing unprotected position")
                    self._close_position(
                        position,
                        "TP/SL placement failed, emergency close",
                        average_price)
                    return
                self._log("warning",
                    "Position filled but server-side TP/SL placement failed")

    def _finalize_pending_order(self, pending: Dict, detail: Dict) -> None:
        filled, avg, state = self._normalize_order_fill(detail, pending)
        if filled > 0:
            self._apply_partial_fill(
                pending, filled_size=filled, average_price=avg, state=state)
        terminal = {"filled", "canceled", "cancelled",
                     "partially_canceled", "partially_cancelled"}
        if state in terminal:
            self._remove_pending_order(pending)
            if state in {"canceled", "cancelled"} and filled <= 0:
                self._log("warning",
                    f"Entry order canceled: {pending.get('order_id')}")
            elif "partial" in state:
                self._log("warning",
                    f"Entry partially canceled: {filled}/{pending.get('size')}")
            else:
                self._log("success",
                    f"✅ Entry filled: {filled} contracts")

    def _remove_pending_order(self, pending: Dict) -> None:
        if pending in self.pending_orders:
            self.pending_orders.remove(pending)
            self._save_pending_orders()

    def _pending_order_is_stale(self, pending: Dict) -> bool:
        created = pending.get("timestamp")
        if not created:
            return False
        try:
            ts = datetime.fromisoformat(created)
        except ValueError:
            return False
        age = datetime.now(timezone.utc) - ts
        return age > timedelta(minutes=int(
            self.execution_cfg.get("max_pending_order_age_minutes", 30)))

    def _reconcile_pending_orders(self) -> None:
        if self.dry_run:
            return
        if not self.execution_cfg.get(
                "reconcile_pending_orders_each_cycle", False):
            return
        if not self.pending_orders:
            return
        if (self.order_stream is not None
                and self.order_stream.is_healthy()
                and self.execution_cfg.get(
                    "prefer_private_order_websocket", True)):
            return

        for pending in list(self.pending_orders):
            if self._pending_order_is_stale(pending):
                self._log("warning",
                    f"Stale pending order: {pending.get('order_id')}")
                # Check for partial fills before canceling
                try:
                    detail = self._fetch_order_detail(
                        order_id=pending.get("order_id"),
                        client_order_id=pending.get("client_order_id"))
                    if detail:
                        filled, avg, state = self._normalize_order_fill(
                            detail, pending)
                        if filled > 0:
                            self._log("warning",
                                f"Stale order has partial fill: "
                                f"{filled}/{pending.get('size')}")
                            self._apply_partial_fill(
                                pending, filled_size=filled,
                                average_price=avg, state=state)
                except Exception as e:
                    self._log("warning",
                        f"Stale order detail fetch failed: {e}")
                # Cancel the (remaining) order
                try:
                    if pending.get("order_id"):
                        self.api.cancel_order(
                            self.inst_id, pending["order_id"])
                except Exception as e:
                    self._log("warning",
                        f"Cancel stale order failed: {e}")
                if pending in self.pending_orders:
                    self.pending_orders.remove(pending)
                self._save_json(
                    self.pending_orders_file, self.pending_orders)
                continue
            try:
                detail = self._fetch_order_detail(
                    order_id=pending.get("order_id"),
                    client_order_id=pending.get("client_order_id"))
            except Exception as e:
                self._record_error(f"Pending reconciliation failed: {e}")
                continue
            if not detail:
                continue
            filled, avg, state = self._normalize_order_fill(detail, pending)
            if state in {"live", "partially_filled"}:
                self._apply_partial_fill(
                    pending, filled_size=filled,
                    average_price=avg, state=state)
                continue
            self._finalize_pending_order(pending, detail)

    # ==================== MARKET DATA ====================

    def get_balance(self) -> Optional[float]:
        currency = "USDT" if self.exchange_name == "blofin" else "USD"
        result = self.api.get_balance("futures", currency)
        if result.get("code") != "0":
            self._log("error", f"💰 Balance fetch failed: {result.get('msg')}")
            return None

        balances = result.get("data", []) or []
        if not balances:
            self._log("warning", "💰 Balance API returned empty data")
            return None

        available = float(balances[0].get("available", 0))
        self._log("info", f"💰 Balance: {available:.2f} {currency}")
        return available

    def _fetch_market_data_rest(self) -> tuple:
        ticker_result = self.api.get_ticker(self.inst_id)
        if ticker_result.get("code") != "0":
            raise RuntimeError(f"Ticker failed: {ticker_result.get('msg')}")

        current_price = float(ticker_result["data"][0]["last"])

        candles = self.api.get_candles(
            inst_id=self.inst_id,
            bar=self._active_timeframe(),
            limit=300)

        if not candles or not isinstance(candles, list):
            raise RuntimeError("Candles fetch failed or returned empty")

        if self.market_stream is not None:
            self.market_stream.seed_snapshot(current_price, candles)

        return current_price, candles

    def get_market_data(self) -> tuple:
        if self.market_stream is not None:
            staleness = float(
                self.market_data_cfg.get("max_staleness_seconds", 30))
            snapshot = self.market_stream.get_snapshot()
            if snapshot and self.market_stream.is_healthy(staleness):
                return snapshot
            rest_data = self._fetch_market_data_rest()
            deadline = time.time() + int(
                self.market_data_cfg.get("warmup_timeout_seconds", 8))
            while time.time() < deadline:
                snapshot = self.market_stream.get_snapshot()
                if snapshot and self.market_stream.is_healthy(staleness):
                    return snapshot
                time.sleep(0.25)
            return rest_data
        return self._fetch_market_data_rest()

    # ==================== POSITION SIZING ====================

    def _position_count(self) -> int:
        return len(self.active_positions) + len(self.pending_orders)

    def _calculate_position_size(self, signal: Signal, balance: float,
                                  current_price: float):
        """Returns PositionSizingResult or None."""
        risk_multiplier = max(float(
            getattr(signal, "risk_multiplier", 1.0) or 0.0), 0.0)
        effective_risk_pct = self.risk_per_trade * risk_multiplier
        if effective_risk_pct <= 0:
            self._log("info",
                "Signal skipped: risk allocation resolved to zero")
            return None

        if signal.stop_loss:
            sizing = calculate_risk_position_size(
                balance=balance,
                entry_price=current_price,
                stop_loss=float(signal.stop_loss),
                risk_percent=effective_risk_pct,
                contract_size=float(self.risk_cfg.get("contract_size", 0.001)),
                contract_step=float(self.risk_cfg.get("contract_step", 0.1)),
                min_contracts=float(self.risk_cfg.get("min_contracts", 0.1)),
                leverage=float(self.risk_cfg.get("leverage", 1.0)),
                max_position_notional_pct=float(
                    self.risk_cfg.get("max_position_notional_pct", 100.0)),
                slippage_buffer_pct=float(
                    self.risk_cfg.get("slippage_buffer_pct", 0.0)),
            )
            if sizing.contracts <= 0:
                self._log("warning", f"⚠️ Position sizing: {sizing.reason}")
                return None
            return sizing

        if not self.risk_cfg.get("allow_without_stop_loss", False):
            self._log("warning",
                      "⚠️ Signal skipped: requires stop_loss for sizing")
            return None

        # Legacy fallback for signals without stop_loss
        if hasattr(self.strategy, 'calculate_position_size'):
            contracts = float(self.strategy.calculate_position_size(
                balance, current_price, effective_risk_pct))
            if contracts <= 0:
                return None
            return PositionSizingResult(
                contracts=contracts,
                risk_amount=balance * (effective_risk_pct / 100.0),
                stop_distance=0.0,
                effective_stop_distance=0.0,
                risk_per_contract=0.0,
                estimated_loss=0.0,
                notional_value=(contracts * current_price
                                * float(self.risk_cfg.get("contract_size", 0.001))),
                reason="legacy_sizing_no_stop_loss",
            )
        return None

    # ==================== SERVER-SIDE TP/SL ====================

    def _build_entry_order_kwargs(self, signal: Signal) -> Dict:
        kwargs = {
            "position_side": self._position_side_for_signal(signal.action),
            "client_order_id": f"entry-{uuid4().hex[:24]}",
        }
        if (self.execution_cfg.get("attach_tpsl_on_entry", False)
                and self.protection.get("use_server_side_tpsl")
                and signal.stop_loss is not None
                and signal.take_profit is not None):
            kwargs.update({
                "tp_trigger_price": f"{float(signal.take_profit):.10f}",
                "tp_order_price": str(
                    self.protection.get("tp_order_price", "-1")),
                "sl_trigger_price": f"{float(signal.stop_loss):.10f}",
                "sl_order_price": str(
                    self.protection.get("sl_order_price", "-1")),
            })
        return kwargs

    def _ensure_server_side_tpsl(self, position: Dict) -> bool:
        if self.dry_run or not self.protection.get("use_server_side_tpsl"):
            return True
        if not position.get("stop_loss") or not position.get("take_profit"):
            return False
        resp = self.api.place_tpsl_order(
            inst_id=self.inst_id,
            margin_mode=self.margin_mode,
            position_side=(position.get("position_side")
                           or self._position_side_for_signal(position["side"])),
            side="sell" if position["side"] == "buy" else "buy",
            size=str(position["size"]),
            tp_trigger_price=f"{float(position['take_profit']):.10f}",
            tp_order_price=str(self.protection.get("tp_order_price", "-1")),
            sl_trigger_price=f"{float(position['stop_loss']):.10f}",
            sl_order_price=str(self.protection.get("sl_order_price", "-1")),
            client_order_id=f"tpsl-{uuid4().hex[:24]}",
            reduce_only=True,
        )
        if resp.get("code") != "0":
            self._log("error",
                f"Failed to place server-side TP/SL: {resp.get('msg')}")
            return False
        data = resp.get("data") or {}
        if isinstance(data, dict):
            position["tpsl_id"] = data.get("algoId") or data.get("tpslId")
        return True

    # ==================== SIGNAL EXECUTION ====================

    def execute_signal(self, signal: Signal, balance: float,
                       current_price: float):
        if signal.action == "hold":
            self._log("info", f"📊 {signal.action.upper()} - {signal.reason}")
            return

        if self._breaker_active():
            self._log("warning",
                f"⛔ Entry blocked by circuit breaker: "
                f"{self.state['circuit_breaker'].get('reason')}")
            return

        trading = self.config.get("trading", {})
        if signal.action == "buy" and not trading.get("allow_long", True):
            self._log("info", "⏭️ Long signal skipped (allow_long=false)")
            return
        if signal.action == "sell" and not trading.get("allow_short", True):
            self._log("info", "⏭️ Short signal skipped (allow_short=false)")
            return

        if balance <= 0:
            self._log("warning", "Cannot trade: zero balance")
            return

        max_pos = int(trading.get("max_positions", 1))
        if self._position_count() >= max_pos:
            self._log("info",
                f"⏭️ Signal skipped: max_positions ({self._position_count()})")
            return

        sizing = self._calculate_position_size(signal, balance, current_price)
        if sizing is None:
            return

        position_type = "LONG" if signal.action == "buy" else "SHORT"
        emoji = "🟢" if signal.action == "buy" else "🔴"

        self._log("success",
            f"{emoji} {position_type} ENTRY @ ${current_price:.2f} "
            f"(conf: {signal.confidence:.0%})")
        self._log("info", f"   └─ {signal.reason}")
        self._log("info",
            f"   └─ Size: {sizing.contracts} contracts | "
            f"Notional: ${sizing.notional_value:.2f} | "
            f"Est. loss: ${sizing.estimated_loss:.2f}")
        if sizing.capped_by_notional:
            self._log("warning",
                "   └─ Size capped by max_position_notional_pct")

        if signal.stop_loss and signal.take_profit:
            sl_pct = abs((signal.stop_loss - current_price)
                         / current_price * 100)
            tp_pct = abs((signal.take_profit - current_price)
                         / current_price * 100)
            self._log("info",
                f"   └─ SL: ${signal.stop_loss:.2f} (-{sl_pct:.1f}%), "
                f"TP: ${signal.take_profit:.2f} (+{tp_pct:.1f}%)")

        if self.dry_run:
            self._log("info", "💤 Dry run - order not placed")
            return

        # Place order with entry kwargs
        try:
            entry_kwargs = self._build_entry_order_kwargs(signal)
            result = self.api.place_order(
                inst_id=self.inst_id,
                side=signal.action,
                order_type="market",
                size=str(sizing.contracts),
                margin_mode=self.margin_mode,
                **entry_kwargs,
            )
        except Exception as e:
            self._record_error(f"Order exception: {e}")
            return

        if result.get("code") != "0":
            self._record_error(f"❌ Order failed: {result.get('msg')}")
            return

        self._reset_error_streak()
        self._log("success", "✅ Order placed", result.get("data"))

        data = result.get("data") or []
        first = (data[0] if isinstance(data, list) and data
                 else data if isinstance(data, dict) else {})

        # Register as pending order
        pending = {
            "inst_id": self.inst_id,
            "order_id": first.get("orderId"),
            "client_order_id": entry_kwargs.get("client_order_id"),
            "side": signal.action,
            "position_side": entry_kwargs.get("position_side"),
            "position_type": position_type,
            "entry_price": current_price,
            "size": sizing.contracts,
            "filled_size": 0.0,
            "average_price": current_price,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "server_side_tpsl": bool(
                self.protection.get("use_server_side_tpsl")
                and self.execution_cfg.get("attach_tpsl_on_entry", False)),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "opened_on_timeframe": self._active_timeframe(),
            "state": "submitted",
            "risk": {
                "risk_amount": sizing.risk_amount,
                "estimated_loss": sizing.estimated_loss,
                "notional_value": sizing.notional_value,
            },
            "max_hold_bars": int(
                getattr(signal, "max_hold_bars", 0) or 0),
            "stale_trade_bars": int(
                self.config.get("strategy", {}).get("time_exit", {}).get(
                    f"{getattr(signal, 'regime', 'range')}_stale_bars", 0)
                or 0),
            "regime_confidence": float(
                getattr(signal, "regime_confidence", 0.0) or 0.0),
            "risk_multiplier": float(
                getattr(signal, "risk_multiplier", 1.0) or 1.0),
            "quality_score": float(
                getattr(signal, "quality_score", 0.0) or 0.0),
            "atr": float(getattr(signal, "atr", 0.0) or 0.0),
            "regime": getattr(signal, "regime", None),
        }
        self.pending_orders.append(pending)
        self._save_pending_orders()

        # Immediately poll for fill
        try:
            detail = self._fetch_order_detail(
                order_id=pending.get("order_id"),
                client_order_id=pending.get("client_order_id"))
        except Exception:
            detail = None

        if detail:
            filled, avg, state = self._normalize_order_fill(detail, pending)
            if state in {"live", "partially_filled"}:
                self._apply_partial_fill(
                    pending, filled_size=filled,
                    average_price=avg, state=state)
            else:
                self._finalize_pending_order(pending, detail)

        if self.protection.get("require_server_side_tpsl"):
            protected = any(
                self._positions_match(pos, pending)
                and pos.get("server_side_tpsl")
                for pos in self.active_positions
            ) or pending.get("server_side_tpsl")
            if not protected:
                self._trip_circuit_breaker(
                    "entry without confirmed server-side TP/SL")

    # ==================== TIME-BASED EXITS ====================

    def _timeframe_minutes(self, timeframe: Optional[str] = None) -> int:
        """Convert a timeframe string to minutes.

        Args:
            timeframe: timeframe to convert. If None, uses the currently
                active timeframe (which may differ from the static config
                when dynamic timeframe selection is enabled).
        """
        tf = str(timeframe if timeframe is not None else self._active_timeframe()).strip().lower()
        if tf.endswith("m"):
            return max(int(tf[:-1]), 1)
        if tf.endswith("h"):
            return max(int(tf[:-1]) * 60, 1)
        if tf.endswith("d"):
            return max(int(tf[:-1]) * 1440, 1)
        return 5

    def _check_time_based_exits(self, current_price: float) -> None:
        now = datetime.now(timezone.utc)
        stale_progress_atr = float(
            self.config.get("strategy", {}).get(
                "time_exit", {}).get("stale_progress_atr", 0.18))
        for position in list(self.active_positions):
            ts = position.get("timestamp")
            if not ts:
                continue
            try:
                opened = datetime.fromisoformat(ts)
            except Exception:
                continue
            # Use the timeframe the position was opened on, not the
            # currently active timeframe. This keeps exit logic stable
            # across regime switches (design decision: positions live on
            # their own timeframe until exit).
            position_tf = position.get("opened_on_timeframe")
            tf_minutes = self._timeframe_minutes(position_tf)
            bars_held = max(int(
                (now - opened).total_seconds()
                // max(tf_minutes * 60, 1)), 0)
            max_hold_bars = int(position.get("max_hold_bars") or 0)
            if max_hold_bars > 0 and bars_held >= max_hold_bars:
                self._close_position(
                    position,
                    f"Time exit after {bars_held} bars",
                    current_price)
                continue
            stale_bars = int(position.get("stale_trade_bars") or 0)
            atr = float(position.get("atr") or 0.0)
            if stale_bars > 0 and atr > 0 and bars_held >= stale_bars:
                entry = float(
                    position.get("entry_price") or current_price)
                progress = ((current_price - entry)
                            if position.get("side") == "buy"
                            else (entry - current_price))
                if progress < atr * stale_progress_atr:
                    self._close_position(
                        position,
                        f"Stale trade exit after {bars_held} bars",
                        current_price)

    # ==================== TRAILING STOP / BREAKEVEN ====================

    def _trailing_config_for_regime(self, regime: Optional[str]) -> Dict:
        """Merge base trailing_stop config with regime-specific overrides."""
        base = dict(self.trailing_cfg)
        if regime and regime in base.get("regime_overrides", {}):
            override = base["regime_overrides"][regime]
            merged = dict(base)
            merged.update(override)
            return merged
        return base

    def _should_update_trailing(self, position: Dict) -> bool:
        """Rate-limit trailing stop updates."""
        min_interval = float(
            self.trailing_cfg.get("min_update_interval_seconds", 30))
        last_update = position.get("_trailing_last_update_ts", 0.0)
        return (time.time() - last_update) >= min_interval

    def _update_server_side_sl(self, position: Dict, new_sl: float) -> bool:
        """Cancel existing TP/SL and place new one with updated SL."""
        if self.dry_run:
            return True

        # Cancel existing TP/SL order if we have one
        tpsl_id = position.get("tpsl_id")
        if tpsl_id:
            try:
                self.api.cancel_tpsl_orders([{
                    "instId": self.inst_id,
                    "tpslId": tpsl_id,
                }])
            except Exception as e:
                self._log("warning", f"Cancel old TP/SL failed: {e}")
                # Continue anyway — we'll place the new one

        # Place new TP/SL with updated stop loss
        tp = position.get("take_profit")
        resp_kwargs = {
            "inst_id": self.inst_id,
            "margin_mode": self.margin_mode,
            "position_side": (position.get("position_side")
                              or self._position_side_for_signal(position["side"])),
            "side": "sell" if position["side"] == "buy" else "buy",
            "size": str(position["size"]),
            "sl_trigger_price": f"{new_sl:.10f}",
            "sl_order_price": str(self.protection.get("sl_order_price", "-1")),
            "client_order_id": f"trail-{uuid4().hex[:24]}",
            "reduce_only": True,
        }
        if tp is not None:
            resp_kwargs["tp_trigger_price"] = f"{float(tp):.10f}"
            resp_kwargs["tp_order_price"] = str(
                self.protection.get("tp_order_price", "-1"))

        resp = self.api.place_tpsl_order(**resp_kwargs)
        if resp.get("code") != "0":
            self._log("error",
                      f"Failed to update trailing SL: {resp.get('msg')}")
            return False

        data = resp.get("data") or {}
        if isinstance(data, dict):
            position["tpsl_id"] = data.get("algoId") or data.get("tpslId")
        return True

    def _update_trailing_stops(self, current_price: float) -> None:
        """Check and update trailing stops / breakeven for all positions."""
        if not self.trailing_cfg.get("enabled"):
            return

        for position in list(self.active_positions):
            atr = float(position.get("atr") or 0)
            if atr <= 0:
                continue

            entry = float(position.get("entry_price") or 0)
            if entry <= 0:
                continue

            regime = position.get("regime")
            tcfg = self._trailing_config_for_regime(regime)
            side = position.get("side", "buy")
            current_sl = float(position.get("stop_loss") or 0)

            # Calculate distance from entry in ATR units
            if side == "buy":
                progress_atr = (current_price - entry) / atr
            else:
                progress_atr = (entry - current_price) / atr

            if progress_atr <= 0:
                # Price hasn't moved in our favor
                continue

            new_sl = None

            # --- Breakeven logic ---
            if (tcfg.get("breakeven_enabled")
                    and not position.get("_breakeven_applied")):
                trigger = float(tcfg.get("breakeven_trigger_atr", 1.0))
                offset = float(tcfg.get("breakeven_offset_atr", 0.05))
                if progress_atr >= trigger:
                    if side == "buy":
                        be_sl = entry + (atr * offset)
                        if current_sl < be_sl:
                            new_sl = be_sl
                    else:
                        be_sl = entry - (atr * offset)
                        if current_sl == 0 or current_sl > be_sl:
                            new_sl = be_sl

                    if new_sl is not None:
                        position["_breakeven_applied"] = True
                        self._log("info",
                            f"📐 Breakeven activated: SL moved to "
                            f"${new_sl:.2f} (entry ${entry:.2f}, "
                            f"progress {progress_atr:.1f}× ATR)")

            # --- Trailing stop logic ---
            if tcfg.get("trail_enabled"):
                activation = float(tcfg.get("trail_activation_atr", 1.5))
                trail_dist = float(tcfg.get("trail_distance_atr", 1.5))
                if progress_atr >= activation:
                    if side == "buy":
                        trail_sl = current_price - (atr * trail_dist)
                        if trail_sl > current_sl and trail_sl > entry:
                            new_sl = trail_sl
                    else:
                        trail_sl = current_price + (atr * trail_dist)
                        if (current_sl == 0 or trail_sl < current_sl) and trail_sl < entry:
                            new_sl = trail_sl

                    if new_sl is not None:
                        # Optionally remove TP to let the trend run
                        if tcfg.get("remove_tp_on_trail") and position.get("take_profit"):
                            self._log("info",
                                "📐 TP removed — trailing stop takes over")
                            position["take_profit"] = None

            # --- Apply the new SL ---
            if new_sl is not None and self._should_update_trailing(position):
                old_sl = current_sl
                position["stop_loss"] = round(new_sl, 8)
                position["_trailing_last_update_ts"] = time.time()

                # Update server-side TP/SL if active
                if (self.protection.get("use_server_side_tpsl")
                        and position.get("server_side_tpsl")
                        and not self.dry_run):
                    ok = self._update_server_side_sl(position, new_sl)
                    if not ok:
                        self._log("warning",
                            "Trailing SL update failed on exchange, "
                            "local SL updated as fallback")

                self._save_positions()
                sl_type = "trail" if position.get("_breakeven_applied") else "breakeven"
                self._log("info",
                    f"📐 {sl_type.title()} SL: ${old_sl:.2f} → "
                    f"${new_sl:.2f} (price ${current_price:.2f}, "
                    f"+{progress_atr:.1f}× ATR)")

    # ==================== POSITION MANAGEMENT ====================

    def check_positions(self, current_price: float):
        if not self.active_positions:
            return

        self._check_time_based_exits(current_price)
        self._update_trailing_stops(current_price)

        # Determine which positions need local SL/TP checks
        if self.protection.get("use_server_side_tpsl") and not self.dry_run:
            positions_to_check = []
            for pos in self.active_positions:
                if not pos.get("server_side_tpsl"):
                    # Retry placing server-side TP/SL for unprotected positions
                    if pos.get("stop_loss") and pos.get("take_profit"):
                        placed = self._ensure_server_side_tpsl(pos)
                        if placed:
                            pos["server_side_tpsl"] = True
                            self._save_positions()
                            self._log("info",
                                "Server-side TP/SL placed on retry")
                            continue
                    if self.protection.get("require_server_side_tpsl"):
                        self._log("error",
                            "CRITICAL: Server-side TP/SL retry failed "
                            "and require_server_side_tpsl is True — "
                            "closing unprotected position")
                        self._close_position(
                            pos,
                            "TP/SL retry failed, emergency close",
                            current_price)
                        continue
                    key = (pos.get("position_id")
                           or pos.get("order_id")
                           or pos.get("timestamp"))
                    if key not in self._protection_warnings_emitted:
                        self._protection_warnings_emitted.add(key)
                        self._log("warning",
                            "Position lacks server-side TP/SL, "
                            "using local fallback", pos)
                    positions_to_check.append(pos)
        else:
            positions_to_check = list(self.active_positions)

        for position in positions_to_check:
            should_close = False
            close_reason = ""

            if position.get("stop_loss"):
                sl = float(position["stop_loss"])
                if position["side"] == "buy" and current_price <= sl:
                    should_close = True
                    close_reason = f"🛑 Stop-loss hit @ ${current_price:.2f}"
                elif position["side"] == "sell" and current_price >= sl:
                    should_close = True
                    close_reason = f"🛑 Stop-loss hit @ ${current_price:.2f}"

            if position.get("take_profit") and not should_close:
                tp = float(position["take_profit"])
                if position["side"] == "buy" and current_price >= tp:
                    should_close = True
                    close_reason = f"🎉 Take-profit @ ${current_price:.2f}"
                elif position["side"] == "sell" and current_price <= tp:
                    should_close = True
                    close_reason = f"🎉 Take-profit @ ${current_price:.2f}"

            if should_close:
                self._close_position(position, close_reason, current_price)

    # ==================== RECONCILIATION EVIDENCE ====================

    def _history_window_bounds(
            self, lookback_hours: Optional[float] = None) -> Tuple[str, str]:
        hours = float(lookback_hours if lookback_hours is not None
                       else self.execution_cfg.get(
                           "history_reconciliation_lookback_hours", 48))
        end_dt = datetime.now(timezone.utc)
        begin_dt = end_dt - timedelta(hours=max(hours, 1.0))
        return (str(int(begin_dt.timestamp() * 1000)),
                str(int(end_dt.timestamp() * 1000)))

    def _history_limit(self) -> int:
        return max(int(self.execution_cfg.get(
            "history_reconciliation_limit", 50)), 1)

    def _fetch_orders_history(
            self, *, order_id: Optional[str] = None) -> List[Dict]:
        begin, end = self._history_window_bounds()
        response = self.api.get_orders_history(
            self.inst_id, begin=begin, end=end,
            limit=self._history_limit())
        if response.get("code") == "unsupported":
            return []
        if response.get("code") != "0":
            self._log("warning",
                f"Orders history fetch failed: {response.get('msg')}")
            return []
        data = response.get("data") or []
        if order_id:
            data = [r for r in data
                    if str(r.get("orderId") or "") == str(order_id)]
        return data

    def _fetch_fills_history(
            self, *, order_id: Optional[str] = None) -> List[Dict]:
        begin, end = self._history_window_bounds()
        response = self.api.get_fills_history(
            self.inst_id, order_id=order_id,
            begin=begin, end=end, limit=self._history_limit())
        if response.get("code") == "unsupported":
            return []
        if response.get("code") != "0":
            self._log("warning",
                f"Fills history fetch failed: {response.get('msg')}")
            return []
        return response.get("data") or []

    def _fetch_positions_history(
            self, *, position_id: Optional[str] = None) -> List[Dict]:
        begin, end = self._history_window_bounds()
        response = self.api.get_positions_history(
            self.inst_id, position_id=position_id,
            begin=begin, end=end, limit=self._history_limit())
        if response.get("code") == "unsupported":
            return []
        if response.get("code") != "0":
            self._log("warning",
                f"Positions history fetch failed: {response.get('msg')}")
            return []
        return response.get("data") or []

    def _build_close_evidence(
            self, position: Optional[Dict] = None,
            *, order_id: Optional[str] = None) -> Dict:
        evidence = {
            "orders_history": [], "fills_history": [],
            "positions_history": [],
        }
        try:
            evidence["orders_history"] = self._fetch_orders_history(
                order_id=order_id)
        except Exception as exc:
            self._log("warning",
                f"Orders history fetch failed: {exc}")
        try:
            evidence["fills_history"] = self._fetch_fills_history(
                order_id=order_id)
        except Exception as exc:
            self._log("warning",
                f"Fills history fetch failed: {exc}")
        try:
            pid = (str(position.get("position_id"))
                   if position and position.get("position_id")
                   else None)
            evidence["positions_history"] = (
                self._fetch_positions_history(position_id=pid))
        except Exception as exc:
            self._log("warning",
                f"Positions history fetch failed: {exc}")
        return evidence

    def _archive_reconciliation_event(
            self, reason: str, payload: Dict) -> Path:
        payload = dict(payload)
        payload.setdefault(
            "timestamp", datetime.now(timezone.utc).isoformat())
        return self._backup_snapshot(reason, payload)

    def _handle_disappeared_position(
            self, position: Dict, current_price: float,
            reason: str) -> None:
        pnl = self._estimate_pnl(position, current_price)
        evidence = self._build_close_evidence(
            position=position,
            order_id=position.get("order_id"))
        report_path = self._archive_reconciliation_event(
            "position-history-reconciliation", {
                "reason": reason,
                "position": position,
                "estimated_close_price": current_price,
                "estimated_pnl": pnl,
                "evidence": evidence,
            })
        self._register_trade_outcome(pnl, reason)
        self._log("info",
            f"Archived reconciliation evidence to {report_path}")

    def _estimate_pnl(self, position: Dict, close_price: float) -> float:
        entry = float(position.get("entry_price") or 0)
        size = float(position.get("size") or 0)
        cs = float(self.risk_cfg.get("contract_size", 1.0))
        direction = 1 if position.get("side") == "buy" else -1
        return (close_price - entry) * direction * size * cs

    def _close_position(self, position: Dict, reason: str,
                        current_price: float) -> None:
        self._log("warning" if "Stop" in reason else "success",
                  reason, position)
        pnl = self._estimate_pnl(position, current_price)

        if self.dry_run:
            if position in self.active_positions:
                self.active_positions.remove(position)
            self._save_positions()
            self._register_trade_outcome(pnl, reason)
            return

        close_side = "sell" if position["side"] == "buy" else "buy"
        try:
            result = self.api.place_order(
                inst_id=self.inst_id,
                side=close_side,
                order_type="market",
                size=str(position["size"]),
                margin_mode=self.margin_mode,
                position_side=(position.get("position_side")
                               or self._position_side_for_signal(
                                   position["side"])),
                reduce_only=True,
                client_order_id=f"close-{uuid4().hex[:24]}",
            )
        except Exception as e:
            self._record_error(f"Close exception: {e}")
            return

        if result.get("code") != "0":
            self._record_error(f"❌ Close failed: {result.get('msg')}")
            return

        self._reset_error_streak()
        if position in self.active_positions:
            self.active_positions.remove(position)
        self._save_positions()

        # Archive reconciliation evidence
        evidence = self._build_close_evidence(
            position=position,
            order_id=position.get("order_id"))
        self._archive_reconciliation_event(
            "close-order-reconciliation", {
                "reason": reason, "position": position,
                "close_result": result,
                "estimated_pnl": pnl, "evidence": evidence,
            })

        self._register_trade_outcome(pnl, reason)
        self._log("success", "✅ Position closed")

    # ==================== EXCHANGE SYNC ====================

    def _sync_exchange_state(self, current_price: float) -> None:
        if self.dry_run:
            return
        if not self.protection.get("sync_exchange_each_cycle", False):
            return

        remote = self._fetch_exchange_positions()
        tpsl = self._fetch_active_tpsl_orders()
        previous = list(self.active_positions)
        merged = self._merge_remote_with_local(remote, previous, tpsl)

        for local in previous:
            if not any(self._positions_match(p, local) for p in merged):
                self._handle_disappeared_position(
                    local, current_price,
                    "position closed on exchange (manual or TP/SL)")

        self.active_positions = merged
        self._save_positions()

        if self.protection.get("require_server_side_tpsl"):
            unprotected = [p for p in self.active_positions
                           if not p.get("server_side_tpsl")]
            if (unprotected
                    and self.breaker_cfg.get("trip_on_missing_protection", True)):
                self._trip_circuit_breaker(
                    "exchange position without server-side TP/SL")

    # ==================== LIVE PROFILE SUPPORT ====================

    def _resolve_relative_path(self, path_str: str) -> Path:
        path = Path(path_str)
        return path if path.is_absolute() else self.base_dir / path

    def _build_strategy_with_live_profiles(self, strategy_cfg: Dict):
        cfg = dict(strategy_cfg)
        selector_cfg = dict(self.parameter_selector_cfg)
        profile_path = selector_cfg.get("live_profile_path")
        if selector_cfg.get("enabled") and profile_path:
            try:
                profile_file = self._resolve_relative_path(profile_path)
                if profile_file.exists():
                    payload = json.loads(profile_file.read_text())
                    cfg["regime_live_profiles"] = payload.get(
                        "regime_profiles", {})
                    cfg["live_profile_metadata"] = {
                        k: v for k, v in payload.items()
                        if k != "regime_profiles"}
            except Exception as exc:
                self._log("warning",
                    f"Failed to load live regime profile: {exc}")
        return create_strategy(
            self.config.get("strategy_name", "advanced"), cfg)

    def _maybe_refresh_live_profile(self) -> None:
        if not _HAS_PROFILE_MANAGER:
            return
        selector_cfg = dict(self.parameter_selector_cfg)
        if (not selector_cfg.get("enabled")
                or not selector_cfg.get("auto_refresh_enabled")):
            return
        now = time.time()
        interval = max(int(
            selector_cfg.get("refresh_interval_minutes", 60)) * 60, 1)
        if (now - self._last_profile_refresh_ts) < interval:
            return
        candidate_path = selector_cfg.get("candidate_profile_path")
        live_path = selector_cfg.get("live_profile_path")
        if not candidate_path or not live_path:
            self._last_profile_refresh_ts = now
            return
        candidate_file = self._resolve_relative_path(candidate_path)
        live_file = self._resolve_relative_path(live_path)
        if not candidate_file.exists():
            self._last_profile_refresh_ts = now
            return
        try:
            report = refresh_live_profile(
                live_file, candidate_file,
                min_regime_overlap=int(
                    selector_cfg.get("min_regime_overlap", 1)),
                max_param_drift=float(
                    selector_cfg.get("max_param_drift", 0.35)),
                require_improvement=bool(
                    selector_cfg.get("require_improvement", True)),
                report_dir=str(self.profile_report_dir),
            )
            self._last_profile_refresh_ts = now
            if report.get("accepted"):
                self.strategy = self._build_strategy_with_live_profiles(
                    self._base_strategy_cfg)
                self._log("success",
                    "Live regime profile refreshed", report)
            else:
                self._log("info",
                    "Candidate regime profile rejected", report)
        except Exception as exc:
            self._last_profile_refresh_ts = now
            self._log("warning",
                f"Live profile refresh failed: {exc}")

    # ==================== MAIN LOOP ====================

    def run_once(self):
        try:
            self._rotate_log_file(self.log_file)
            self._rotate_log_file(self.performance_file)
            self._maybe_refresh_live_profile()

            balance = self.get_balance()
            if balance is None:
                self._record_error("Balance fetch failed, skipping cycle")
                return
            self._update_balance_state(balance)

            current_price, candles = self.get_market_data()
            self._reset_error_streak()
            self._check_price_jump(current_price)

            self._log("info", f"📈 {self.inst_id}: ${current_price:.2f}")

            with self._state_lock:
                self._reconcile_pending_orders()
                self._sync_exchange_state(current_price)
                self.check_positions(current_price)

            if hasattr(self.strategy, 'analyze'):
                signal = self.strategy.analyze(candles, current_price)
            else:
                signal = self.strategy.generate_signal(candles, current_price)

            # Feed detected regime back into the timeframe resolver so
            # subsequent cycles fetch the right candles and sleep the
            # right amount. No-op when regime_timeframes.enabled=false.
            detected_regime = getattr(signal, "regime", None)
            if detected_regime:
                self._apply_regime_to_timeframe(detected_regime)

            # Inform the strategy which timeframe is currently active so
            # it can apply the matching calibrated parameter profile on
            # the *next* analyze() call. No-op if no profiles are loaded.
            if hasattr(self.strategy, "set_active_timeframe"):
                self.strategy.set_active_timeframe(self._active_timeframe())

            # Compact signal log with regime context
            regime = getattr(signal, "regime", None) or ""
            regime_conf = getattr(signal, "regime_confidence", 0.0) or 0.0
            risk_mult = getattr(signal, "risk_multiplier", 1.0) or 1.0
            quality = getattr(signal, "quality_score", 0.0) or 0.0

            if hasattr(signal, "indicators") and signal.indicators:
                core_keys = [
                    "rsi_value", "macd_hist", "volume",
                    "atr_pct", "efficiency_ratio"]
                ind_items = [
                    (k, signal.indicators[k]) for k in core_keys
                    if k in signal.indicators]
                if not ind_items:
                    ind_items = list(signal.indicators.items())[:5]
                ind_str = ", ".join(
                    f"{k}:{v:.2f}" for k, v in ind_items)
                self._log("info",
                    f"📊 {signal.action.upper()} "
                    f"conf={signal.confidence:.0%} "
                    f"regime={regime}({regime_conf:.0%}) "
                    f"risk_mult={risk_mult:.2f} "
                    f"quality={quality:.2f} | {ind_str}")
            else:
                self._log("info",
                    f"📊 {signal.action.upper()} "
                    f"conf={signal.confidence:.0%} | {signal.reason}")

            if signal.confidence < self.min_confidence:
                return

            with self._state_lock:
                self.execute_signal(signal, balance, current_price)

        except Exception as e:
            self._record_error(f"Cycle error: {e}")

    def start(self, interval: int = 60):
        self.running = True
        self._static_interval = int(interval)
        dyn = "dynamic" if self.regime_tf_resolver.enabled else "static"
        self._log("success",
            f"🚀 Bot started! Timeframe={self._active_timeframe()}, "
            f"interval={self._active_check_interval()}s ({dyn})")
        try:
            while self.running:
                self.run_once()
                time.sleep(self._active_check_interval())
        except KeyboardInterrupt:
            self._log("warning", "🛑 Bot stopped by user")
        finally:
            self.running = False
            self._stop_market_stream()
            self._stop_order_stream()

    def preflight(self) -> int:
        """Run preflight checks. Returns 0 on success, 1 on failure."""
        checks_passed = 0
        checks_failed = 0

        def _check(name: str, fn):
            nonlocal checks_passed, checks_failed
            try:
                ok, detail = fn()
                status = "PASS" if ok else "FAIL"
                if ok:
                    checks_passed += 1
                else:
                    checks_failed += 1
                print(f"  [{status}] {name}: {detail}")
            except Exception as e:
                checks_failed += 1
                print(f"  [FAIL] {name}: {e}")

        print("Preflight checks:")

        _check("Config loaded", lambda: (True, f"exchange={self.exchange_name}"))

        _check("Dry run mode", lambda: (
            True, f"{'DRY RUN' if self.dry_run else 'LIVE'}"))

        def _check_exchange():
            result = self.api.get_ticker(self.inst_id)
            ok = result.get("code") == "0"
            return ok, f"ticker={'OK' if ok else result.get('msg', 'error')}"
        _check("Exchange connectivity", _check_exchange)

        def _check_balance():
            bal = self.get_balance()
            return bal is not None, f"balance={bal}"
        _check("Balance accessible", _check_balance)

        def _check_state_dir():
            test_file = self.memory_dir / ".preflight_test"
            try:
                test_file.write_text("ok")
                test_file.unlink()
                return True, str(self.memory_dir)
            except Exception as e:
                return False, str(e)
        _check("State dir writable", _check_state_dir)

        def _check_state_files():
            issues = []
            for f in [self.positions_file, self.pending_orders_file,
                      self.state_file]:
                if f.exists():
                    try:
                        json.loads(f.read_text())
                    except Exception:
                        issues.append(f.name)
                tmp = f.with_suffix(f.suffix + ".tmp")
                if tmp.exists():
                    issues.append(f"{f.name}.tmp orphaned")
            if issues:
                return False, ", ".join(issues)
            return True, "all coherent"
        _check("State files coherent", _check_state_files)

        def _check_protection():
            use = self.protection.get("use_server_side_tpsl")
            req = self.protection.get("require_server_side_tpsl")
            return True, f"server_tpsl={use}, required={req}"
        _check("Protection config", _check_protection)

        def _check_capabilities():
            caps = self.api.get_capabilities()
            issues = []
            if self.protection.get("require_server_side_tpsl") and not caps.get("server_side_tpsl"):
                issues.append("config requires server-side TP/SL but adapter does not support it")
            if not issues:
                supported = [k for k, v in caps.items() if v]
                return True, f"{len(supported)} features: {', '.join(supported)}"
            return False, "; ".join(issues)
        _check("Exchange capabilities", _check_capabilities)

        # Config report + deprecation warnings
        report = generate_config_report(self.config)
        deprecations = report.get("deprecation_warnings", [])
        if deprecations:
            print(f"\nDeprecation warnings ({len(deprecations)}):")
            for w in deprecations:
                print(f"  [WARN] {w}")

        print(f"\nConfig summary:")
        print(f"  Exchange:       {report['exchange']}")
        print(f"  Trading pair:   {report['trading_pair']}")
        print(f"  Mode:           {'DRY RUN' if report['dry_run'] else 'LIVE'}")
        print(f"  Credentials:    {'OK' if report['credentials_present'] else 'MISSING'}")
        print(f"  Risk/trade:     {report['risk_per_trade_pct']}%")
        print(f"  Leverage:       {report['leverage']}x")
        print(f"  Server TP/SL:   {report['server_side_tpsl']} (required={report['require_tpsl']})")
        print(f"  Circuit breaker: {report['circuit_breaker_enabled']}")

        print(f"\nResult: {checks_passed} passed, {checks_failed} failed")
        return 0 if checks_failed == 0 else 1


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Multi-Exchange Trading Bot v2.4")
    parser.add_argument("--config", default="config.json",
                        help="Config file path")
    parser.add_argument("--interval", type=int, default=60,
                        help="Check interval (seconds)")
    parser.add_argument("--once", action="store_true",
                        help="Run once and exit")
    parser.add_argument("--preflight", action="store_true",
                        help="Run preflight checks and exit")
    parser.add_argument("--force-reconcile", action="store_true",
                        help="Continue despite reconciliation mismatches")

    args = parser.parse_args()

    try:
        bot = TradingBot(args.config,
                         force_reconcile=args.force_reconcile)

        if args.preflight:
            sys.exit(bot.preflight())
        elif args.once:
            bot.run_once()
        else:
            bot.start(args.interval)
    except ReconciliationError as exc:
        print(f"❌ Reconciliation failed: {exc}", file=sys.stderr)
        print("   Use --force-reconcile to bypass (dangerous)",
              file=sys.stderr)
        sys.exit(2)
    except (ConfigError, FileNotFoundError, RuntimeError) as exc:
        print(f"❌ Fatal: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
