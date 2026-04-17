
#!/usr/bin/env python3
"""Blofin private websocket stream for order and algo-order updates."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from typing import Callable, Dict, Optional
from uuid import uuid4

import websocket


def _default_logger(level: str, message: str, data: Optional[Dict] = None) -> None:
    del data
    print(f"[{level.upper()}] {message}")


class BlofinPrivateOrderStream:
    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        passphrase: str,
        inst_id: str,
        demo_mode: bool = False,
        logger: Optional[Callable[[str, str, Optional[Dict]], None]] = None,
        on_order_update: Optional[Callable[[Dict], None]] = None,
        ping_interval: int = 20,
        ping_timeout: int = 10,
        reconnect_delay: int = 5,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.inst_id = inst_id
        self.demo_mode = bool(demo_mode)
        self.logger = logger or _default_logger
        self.on_order_update = on_order_update
        self.ping_interval = int(ping_interval)
        self.ping_timeout = int(ping_timeout)
        self.reconnect_delay = int(reconnect_delay)
        self.url = 'wss://demo-trading-openapi.blofin.com/ws/private' if self.demo_mode else 'wss://openapi.blofin.com/ws/private'
        self._lock = threading.Lock()
        self._ws_app = None
        self._thread = None
        self._stop_event = threading.Event()
        self._connected = False
        self._authenticated = False
        self._last_message_ts = None
        self._last_error = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_forever, daemon=True, name='blofin-private-orders-ws')
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

    def is_healthy(self, max_staleness_seconds: float = 45.0) -> bool:
        with self._lock:
            if not self._connected or not self._authenticated:
                return False
            if self._last_message_ts is None:
                return False
            return (time.time() - self._last_message_ts) <= float(max_staleness_seconds)

    def status(self) -> Dict:
        with self._lock:
            return {
                'connected': self._connected,
                'authenticated': self._authenticated,
                'last_message_ts': self._last_message_ts,
                'last_error': self._last_error,
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
                self.logger('error', f'Blofin private websocket crashed: {exc}')
            finally:
                with self._lock:
                    self._connected = False
                    self._authenticated = False
                self._ws_app = None
            if not self._stop_event.is_set():
                time.sleep(self.reconnect_delay)

    def _login_payload(self) -> Dict:
        timestamp = str(int(time.time() * 1000))
        nonce = str(uuid4())
        path = '/users/self/verify'
        method = 'GET'
        msg = f'{path}{method}{timestamp}{nonce}'
        hex_signature = hmac.new(self.api_secret.encode(), msg.encode(), hashlib.sha256).hexdigest().encode()
        sign = base64.b64encode(hex_signature).decode()
        return {
            'op': 'login',
            'args': [{
                'apiKey': self.api_key,
                'passphrase': self.passphrase,
                'timestamp': timestamp,
                'sign': sign,
                'nonce': nonce,
            }],
        }

    def _on_open(self, ws) -> None:
        del ws
        with self._lock:
            self._connected = True
            self._authenticated = False
            self._last_error = None
        self._send(self._login_payload())

    def _subscribe(self) -> None:
        self._send({'op': 'subscribe', 'args': [{'channel': 'orders', 'instId': self.inst_id}, {'channel': 'orders-algo', 'instId': self.inst_id}]})
        self.logger('info', f'Blofin private websocket subscribed for {self.inst_id}')

    def _on_message(self, ws, message: str) -> None:
        del ws
        with self._lock:
            self._last_message_ts = time.time()
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            return
        event = payload.get('event')
        if event == 'login' and str(payload.get('code')) == '0':
            with self._lock:
                self._authenticated = True
            self.logger('info', 'Blofin private websocket authenticated')
            self._subscribe()
            return
        if event == 'error':
            self._set_error(payload.get('msg') or payload.get('code') or 'private websocket error')
            return
        if payload.get('op') == 'pong' or event == 'pong':
            return
        arg = payload.get('arg') or {}
        channel = arg.get('channel')
        if channel not in {'orders', 'orders-algo'}:
            return
        data = payload.get('data') or []
        if isinstance(data, dict):
            data = [data]
        for item in data:
            if isinstance(item, dict) and self.on_order_update is not None:
                try:
                    self.on_order_update({'channel': channel, **item})
                except Exception as exc:
                    self.logger('warning', f'Order update callback failed: {exc}')

    def _on_error(self, ws, error) -> None:
        del ws
        self._set_error(str(error))
        self.logger('warning', f'Blofin private websocket error: {error}')

    def _on_close(self, ws, status_code, message) -> None:
        del ws
        with self._lock:
            self._connected = False
            self._authenticated = False
        self.logger('warning', f'Blofin private websocket closed: code={status_code}, message={message}')

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
                self._authenticated = False
