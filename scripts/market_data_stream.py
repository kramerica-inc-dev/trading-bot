#!/usr/bin/env python3
"""Blofin public websocket market data stream with reconnect and local cache."""

from __future__ import annotations

import json
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

import websocket


def _default_logger(level: str, message: str, data: Optional[Dict] = None) -> None:
    del data
    print(f"[{level.upper()}] {message}")


class BlofinMarketDataStream:
    TIMEFRAME_TO_CHANNEL = {
        "1m": "candle1m",
        "3m": "candle3m",
        "5m": "candle5m",
        "15m": "candle15m",
        "30m": "candle30m",
        "1h": "candle1H",
        "2h": "candle2H",
        "4h": "candle4H",
        "6h": "candle6H",
        "8h": "candle8H",
        "12h": "candle12H",
        "1d": "candle1D",
        "1w": "candle1W",
        "1M": "candle1M",
    }

    def __init__(
        self,
        *,
        inst_id: str,
        timeframe: str,
        demo_mode: bool = False,
        logger: Optional[Callable[[str, str, Optional[Dict]], None]] = None,
        max_candles: int = 200,
        ping_interval: int = 20,
        ping_timeout: int = 10,
        reconnect_delay: int = 5,
    ) -> None:
        self.inst_id = inst_id
        self.timeframe = timeframe
        self.demo_mode = bool(demo_mode)
        self.logger = logger or _default_logger
        self.max_candles = max(20, int(max_candles))
        self.ping_interval = int(ping_interval)
        self.ping_timeout = int(ping_timeout)
        self.reconnect_delay = int(reconnect_delay)
        self.channel = self.TIMEFRAME_TO_CHANNEL.get(timeframe)
        if not self.channel:
            raise ValueError(f"Unsupported websocket timeframe: {timeframe}")

        host = "wss://demo-trading-openapi.blofin.com/ws/public" if self.demo_mode else "wss://openapi.blofin.com/ws/public"
        self.url = host

        self._lock = threading.Lock()
        self._ws_app = None
        self._thread = None
        self._stop_event = threading.Event()
        self._connected = False
        self._last_ticker: Optional[float] = None
        self._last_ticker_ts: Optional[float] = None
        self._candles: Dict[str, List[str]] = {}
        self._last_candle_ts: Optional[float] = None
        self._last_message_ts: Optional[float] = None
        self._last_error: Optional[str] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_forever, daemon=True, name="blofin-market-ws")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        ws_app = self._ws_app
        if ws_app is not None:
            try:
                ws_app.close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def seed_snapshot(self, current_price: Optional[float], candles: List) -> None:
        now = time.time()
        with self._lock:
            if current_price is not None:
                self._last_ticker = float(current_price)
                self._last_ticker_ts = now
            parsed = {}
            for candle in candles or []:
                normalized = self._normalize_candle(candle)
                if normalized is None:
                    continue
                parsed[normalized[0]] = normalized
            if parsed:
                self._candles.update(parsed)
                self._trim_candles_locked()
                self._last_candle_ts = now
                self._last_message_ts = now

    def is_healthy(self, max_staleness_seconds: float = 30.0) -> bool:
        max_staleness_seconds = float(max_staleness_seconds)
        with self._lock:
            if not self._connected:
                return False
            if self._last_message_ts is None:
                return False
            if time.time() - self._last_message_ts > max_staleness_seconds:
                return False
            if self._last_ticker is None:
                return False
            if len(self._candles) < 5:
                return False
            return True

    def get_snapshot(self) -> Optional[Tuple[float, List[List[str]]]]:
        with self._lock:
            if self._last_ticker is None or not self._candles:
                return None
            candles = [self._candles[k] for k in sorted(self._candles.keys())]
            return float(self._last_ticker), candles

    def status(self) -> Dict:
        with self._lock:
            return {
                "connected": self._connected,
                "last_ticker_ts": self._last_ticker_ts,
                "last_candle_ts": self._last_candle_ts,
                "last_message_ts": self._last_message_ts,
                "last_error": self._last_error,
                "cached_candles": len(self._candles),
            }

    def _run_forever(self) -> None:
        while not self._stop_event.is_set():
            self._ws_app = websocket.WebSocketApp(
                self.url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            try:
                self._ws_app.run_forever(ping_interval=self.ping_interval, ping_timeout=self.ping_timeout)
            except Exception as exc:
                self._set_error(str(exc))
                self.logger("error", f"Blofin market websocket crashed: {exc}")
            finally:
                with self._lock:
                    self._connected = False
                self._ws_app = None
            if not self._stop_event.is_set():
                time.sleep(self.reconnect_delay)

    def _on_open(self, ws) -> None:
        del ws
        with self._lock:
            self._connected = True
            self._last_error = None
        self.logger("info", f"Blofin market websocket connected for {self.inst_id} / {self.channel}")
        payload = {
            "op": "subscribe",
            "args": [
                {"channel": "tickers", "instId": self.inst_id},
                {"channel": self.channel, "instId": self.inst_id},
            ],
        }
        self._send(payload)

    def _on_message(self, ws, message: str) -> None:
        del ws
        now = time.time()
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return
        with self._lock:
            self._last_message_ts = now

        if payload.get("event") in {"subscribe", "unsubscribe"}:
            return
        if payload.get("event") == "error":
            self._set_error(payload.get("msg") or payload.get("code") or "unknown websocket error", fatal=False)
            return
        if payload.get("op") == "pong" or payload.get("event") == "pong":
            return

        arg = payload.get("arg") or {}
        channel = arg.get("channel")
        data = payload.get("data") or []
        if channel == "tickers":
            self._handle_tickers(data, now)
        elif channel == self.channel:
            self._handle_candles(data, now)

    def _on_error(self, ws, error) -> None:
        del ws
        self._set_error(str(error))
        self.logger("warning", f"Blofin market websocket error: {error}")

    def _on_close(self, ws, status_code, message) -> None:
        del ws
        with self._lock:
            self._connected = False
        self.logger("warning", f"Blofin market websocket closed: code={status_code}, message={message}")

    def _send(self, payload: Dict) -> None:
        if self._ws_app is None:
            return
        try:
            self._ws_app.send(json.dumps(payload))
        except Exception as exc:
            self._set_error(str(exc), fatal=False)

    def _set_error(self, message: str, *, fatal: bool = True) -> None:
        with self._lock:
            self._last_error = message
            if fatal:
                self._connected = False

    def _handle_tickers(self, data: List[Dict], now: float) -> None:
        if not data:
            return
        ticker = data[0] if isinstance(data, list) else data
        price = ticker.get("last") if isinstance(ticker, dict) else None
        if price is None:
            return
        with self._lock:
            self._last_ticker = float(price)
            self._last_ticker_ts = now

    def _handle_candles(self, data: List, now: float) -> None:
        updates = []
        if isinstance(data, list):
            for item in data:
                normalized = self._normalize_candle(item)
                if normalized is not None:
                    updates.append(normalized)
        elif isinstance(data, dict):
            normalized = self._normalize_candle(data)
            if normalized is not None:
                updates.append(normalized)
        if not updates:
            return
        with self._lock:
            for candle in updates:
                self._candles[candle[0]] = candle
            self._trim_candles_locked()
            self._last_candle_ts = now

    def _trim_candles_locked(self) -> None:
        keys = sorted(self._candles.keys())
        if len(keys) <= self.max_candles:
            return
        for key in keys[:-self.max_candles]:
            self._candles.pop(key, None)

    def _normalize_candle(self, item) -> Optional[List[str]]:
        if isinstance(item, dict):
            ts = str(item.get("ts")) if item.get("ts") is not None else None
            if not ts:
                return None
            return [
                ts,
                str(item.get("open", "0")),
                str(item.get("high", "0")),
                str(item.get("low", "0")),
                str(item.get("close", "0")),
                str(item.get("vol", item.get("volume", "0"))),
                str(item.get("volCurrency", "0")),
                str(item.get("volCurrencyQuote", "0")),
                str(item.get("confirm", item.get("isClosed", "0"))),
            ]
        if isinstance(item, (list, tuple)) and len(item) >= 5:
            values = [str(v) for v in item]
            while len(values) < 9:
                values.append("0")
            return values[:9]
        return None
