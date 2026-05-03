#!/usr/bin/env python3
"""OKX v5 REST API Client.

Faithful client for OKX's v5 endpoints. Symbols use OKX's native naming
(`BTC-USDT-SWAP` for perp futures) — the OkxAdapter layer above is what
normalizes BTC-USDT ↔ BTC-USDT-SWAP for the runner.

Auth scheme (OK-ACCESS-* headers):
    timestamp = ISO 8601 UTC with millisecond precision
    prehash   = timestamp + method + requestPath + body
    signature = base64(HMAC-SHA256(secret, prehash))   # binary -> base64

Demo trading: pass demo=True; adds `x-simulated-trading: 1` header on the
same base URL (no separate hostname like BloFin).
"""

import base64
import hashlib
import hmac
import json
import random
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests


class OkxAPI:
    """OKX v5 REST client. Public + authenticated endpoints used by Plan E.

    Method names + return shapes mirror BlofinAPI deliberately so OkxAdapter
    can be a near-passthrough. Where the OKX response shape differs from
    BloFin's, we normalize at the OkxAdapter layer (not here) — keep this
    client a faithful mirror of OKX's wire format.
    """

    BASE_URL = "https://www.okx.com"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        passphrase: str = "",
        demo: bool = False,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.demo = demo
        self.session = requests.Session()

        # Rate limits on OKX vary per endpoint (5-60 req/2s). Keep
        # conservative aggregate similar to the BloFin client.
        self._rate_window = 2.0
        self._rate_max = 8
        self._rate_timestamps: deque = deque()
        self._rate_lock = threading.Lock()
        self._max_retries = 3
        self._base_retry_delay = 0.5

    # =====================  Internals  =====================

    def _wait_for_rate_limit(self) -> None:
        with self._rate_lock:
            now = time.monotonic()
            while self._rate_timestamps and self._rate_timestamps[0] < now - self._rate_window:
                self._rate_timestamps.popleft()
            if len(self._rate_timestamps) >= self._rate_max:
                sleep_until = self._rate_timestamps[0] + self._rate_window
                wait = sleep_until - now
                if wait > 0:
                    time.sleep(wait)
                now = time.monotonic()
                while self._rate_timestamps and self._rate_timestamps[0] < now - self._rate_window:
                    self._rate_timestamps.popleft()
            self._rate_timestamps.append(time.monotonic())

    @staticmethod
    def _iso_timestamp() -> str:
        # OKX wants ISO 8601 UTC w/ millisecond precision and trailing Z.
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") \
            + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"

    def _sign(self, timestamp: str, method: str, request_path: str,
              body: str) -> str:
        prehash = f"{timestamp}{method}{request_path}{body}"
        mac = hmac.new(
            self.api_secret.encode("utf-8"),
            prehash.encode("utf-8"),
            hashlib.sha256,
        )
        return base64.b64encode(mac.digest()).decode("utf-8")

    def _request(
        self, method: str, path: str,
        params: Optional[Dict] = None, body: Optional[Dict] = None,
        auth: bool = False,
    ) -> Dict[str, Any]:
        # Build the request path (with query string for GET — required for
        # signature input on OKX).
        request_path = path
        if params:
            qs = urlencode({k: v for k, v in params.items() if v is not None})
            if qs:
                request_path = f"{path}?{qs}"
        url = f"{self.BASE_URL}{request_path}"
        body_str = json.dumps(body) if body is not None else ""

        last_error: Optional[str] = None
        for attempt in range(1, self._max_retries + 1):
            self._wait_for_rate_limit()
            headers = {"Content-Type": "application/json"}
            if self.demo:
                headers["x-simulated-trading"] = "1"
            if auth:
                ts = self._iso_timestamp()
                headers.update({
                    "OK-ACCESS-KEY": self.api_key,
                    "OK-ACCESS-SIGN": self._sign(ts, method, request_path, body_str),
                    "OK-ACCESS-TIMESTAMP": ts,
                    "OK-ACCESS-PASSPHRASE": self.passphrase,
                })

            try:
                if method == "GET":
                    resp = self.session.get(url, headers=headers, timeout=10)
                elif method == "POST":
                    resp = self.session.post(url, headers=headers,
                                              data=body_str if body is not None else None,
                                              timeout=10)
                else:
                    raise ValueError(f"unsupported method: {method}")

                if resp.status_code in (429, 500, 502, 503, 504) and attempt < self._max_retries:
                    last_error = f"HTTP {resp.status_code}"
                    time.sleep(self._base_retry_delay * (2 ** (attempt - 1))
                               + random.uniform(0, 0.25))
                    continue

                try:
                    data = resp.json()
                except ValueError:
                    last_error = f"non-JSON response: HTTP {resp.status_code}"
                    if attempt < self._max_retries:
                        time.sleep(self._base_retry_delay * (2 ** (attempt - 1)))
                        continue
                    return {"code": "error", "msg": last_error, "data": None}

                if resp.status_code >= 400 and resp.status_code not in (429,):
                    msg = data.get("msg") if isinstance(data, dict) else f"HTTP {resp.status_code}"
                    return {"code": "error", "msg": f"HTTP {resp.status_code}: {msg}",
                            "data": data.get("data") if isinstance(data, dict) else None}

                return data if isinstance(data, dict) else {
                    "code": "error", "msg": "unexpected response shape", "data": data,
                }
            except requests.exceptions.RequestException as e:
                last_error = str(e)
                if attempt < self._max_retries:
                    time.sleep(self._base_retry_delay * (2 ** (attempt - 1))
                               + random.uniform(0, 0.25))
                    continue

        return {"code": "error", "msg": last_error or "unknown request error",
                "data": None}

    # =====================  Public market data  =====================

    def get_ticker(self, inst_id: str = "BTC-USDT-SWAP") -> Dict:
        return self._request("GET", "/api/v5/market/ticker",
                             params={"instId": inst_id})

    def get_orderbook(self, inst_id: str = "BTC-USDT-SWAP", size: int = 5) -> Dict:
        return self._request("GET", "/api/v5/market/books",
                             params={"instId": inst_id, "sz": str(size)})

    def get_candles(
        self, inst_id: str = "BTC-USDT-SWAP", bar: str = "5m",
        limit: int = 100, before: Optional[str] = None,
        after: Optional[str] = None,
    ) -> Dict:
        """Historical candles. OKX returns newest-first.

        bar values OKX accepts: 1m,3m,5m,15m,30m,1H,2H,4H,6H,12H,1D,1W,1M.
        """
        params: Dict[str, Any] = {
            "instId": inst_id, "bar": bar, "limit": str(min(limit, 300)),
        }
        if before: params["before"] = before
        if after: params["after"] = after
        return self._request("GET", "/api/v5/market/candles", params=params)

    def get_mark_price(self, inst_id: str = "BTC-USDT-SWAP") -> Dict:
        return self._request("GET", "/api/v5/public/mark-price",
                             params={"instType": "SWAP", "instId": inst_id})

    def get_funding_rate(self, inst_id: str = "BTC-USDT-SWAP") -> Dict:
        return self._request("GET", "/api/v5/public/funding-rate",
                             params={"instId": inst_id})

    def get_funding_rate_history(
        self, inst_id: str = "BTC-USDT-SWAP",
        before: Optional[str] = None, after: Optional[str] = None,
        limit: int = 100,
    ) -> Dict:
        params: Dict[str, Any] = {"instId": inst_id, "limit": str(limit)}
        if before: params["before"] = before
        if after: params["after"] = after
        return self._request("GET", "/api/v5/public/funding-rate-history",
                             params=params)

    # =====================  Account  =====================

    def get_balance(self, account_type: str = "futures",
                    currency: Optional[str] = None) -> Dict:
        # `account_type` is unused on OKX (single unified balance endpoint).
        # Kept for adapter-interface parity.
        params: Dict[str, Any] = {}
        if currency:
            params["ccy"] = currency
        return self._request("GET", "/api/v5/account/balance",
                             params=params, auth=True)

    def get_position_mode(self) -> Dict:
        return self._request("GET", "/api/v5/account/config", auth=True)

    def get_positions(self, inst_id: Optional[str] = None) -> Dict:
        params: Dict[str, Any] = {"instType": "SWAP"}
        if inst_id:
            params["instId"] = inst_id
        return self._request("GET", "/api/v5/account/positions",
                             params=params, auth=True)

    def get_positions_history(
        self, inst_id: Optional[str] = None,
        position_id: Optional[str] = None,
        begin: Optional[str] = None, end: Optional[str] = None,
        limit: int = 100,
    ) -> Dict:
        params: Dict[str, Any] = {"instType": "SWAP", "limit": str(limit)}
        if inst_id: params["instId"] = inst_id
        if position_id: params["posId"] = position_id
        if begin: params["begin"] = begin
        if end: params["end"] = end
        return self._request("GET", "/api/v5/account/positions-history",
                             params=params, auth=True)

    # =====================  Trading  =====================

    def place_order(
        self, inst_id: str, side: str, order_type: str, size: str,
        price: Optional[str] = None, margin_mode: str = "isolated",
        position_side: Optional[str] = None,
        reduce_only: Optional[bool] = None,
        client_order_id: Optional[str] = None,
        tp_trigger_price: Optional[str] = None,
        tp_order_price: Optional[str] = None,
        sl_trigger_price: Optional[str] = None,
        sl_order_price: Optional[str] = None,
    ) -> Dict:
        body: Dict[str, Any] = {
            "instId": inst_id,
            "tdMode": margin_mode,        # OKX: cross / isolated / cash
            "side": side,
            "ordType": order_type,        # market / limit / post_only / fok / ioc
            "sz": size,
        }
        if order_type in ("limit", "post_only", "fok", "ioc") and price:
            body["px"] = price
        if position_side:
            body["posSide"] = position_side
        if reduce_only is not None:
            body["reduceOnly"] = "true" if reduce_only else "false"
        if client_order_id:
            body["clOrdId"] = client_order_id
        if tp_trigger_price is not None:
            body["tpTriggerPx"] = tp_trigger_price
            body["tpOrdPx"] = tp_order_price if tp_order_price is not None else "-1"
        if sl_trigger_price is not None:
            body["slTriggerPx"] = sl_trigger_price
            body["slOrdPx"] = sl_order_price if sl_order_price is not None else "-1"
        return self._request("POST", "/api/v5/trade/order", body=body, auth=True)

    def cancel_order(self, inst_id: str, order_id: str) -> Dict:
        body = {"instId": inst_id, "ordId": order_id}
        return self._request("POST", "/api/v5/trade/cancel-order",
                             body=body, auth=True)

    def get_orders(self, inst_id: Optional[str] = None,
                   state: Optional[str] = None) -> Dict:
        # `live` orders are pending; `filled`/`canceled` are history. Route to
        # the right endpoint to match BloFin's single get_orders(state=...).
        if state == "live" or state is None and inst_id is None:
            return self.get_active_orders(inst_id)
        params: Dict[str, Any] = {"instType": "SWAP"}
        if inst_id: params["instId"] = inst_id
        if state: params["state"] = state
        return self._request("GET", "/api/v5/trade/orders-history",
                             params=params, auth=True)

    def get_active_orders(self, inst_id: Optional[str] = None) -> Dict:
        params: Dict[str, Any] = {"instType": "SWAP"}
        if inst_id: params["instId"] = inst_id
        return self._request("GET", "/api/v5/trade/orders-pending",
                             params=params, auth=True)

    def get_order_detail(self, inst_id: str, order_id: Optional[str] = None,
                         client_order_id: Optional[str] = None) -> Dict:
        params: Dict[str, Any] = {"instId": inst_id}
        if order_id:
            params["ordId"] = order_id
        elif client_order_id:
            params["clOrdId"] = client_order_id
        else:
            return {"code": "error",
                    "msg": "order_id or client_order_id is required",
                    "data": None}
        return self._request("GET", "/api/v5/trade/order",
                             params=params, auth=True)

    def get_orders_history(
        self, inst_id: Optional[str] = None,
        begin: Optional[str] = None, end: Optional[str] = None,
        limit: int = 100,
    ) -> Dict:
        params: Dict[str, Any] = {"instType": "SWAP", "limit": str(limit)}
        if inst_id: params["instId"] = inst_id
        if begin: params["begin"] = begin
        if end: params["end"] = end
        return self._request("GET", "/api/v5/trade/orders-history",
                             params=params, auth=True)

    def get_fills_history(
        self, inst_id: Optional[str] = None, order_id: Optional[str] = None,
        begin: Optional[str] = None, end: Optional[str] = None,
        limit: int = 100,
    ) -> Dict:
        params: Dict[str, Any] = {"instType": "SWAP", "limit": str(limit)}
        if inst_id: params["instId"] = inst_id
        if order_id: params["ordId"] = order_id
        if begin: params["begin"] = begin
        if end: params["end"] = end
        return self._request("GET", "/api/v5/trade/fills-history",
                             params=params, auth=True)

    # =====================  TP/SL (algo orders)  =====================

    def place_tpsl_order(
        self, *, inst_id: str, margin_mode: str, position_side: str,
        side: str, size: str,
        tp_trigger_price: Optional[str] = None,
        tp_order_price: Optional[str] = None,
        sl_trigger_price: Optional[str] = None,
        sl_order_price: Optional[str] = None,
        client_order_id: Optional[str] = None,
        reduce_only: bool = True,
        order_type: str = "conditional",
    ) -> Dict:
        body: Dict[str, Any] = {
            "instId": inst_id,
            "tdMode": margin_mode,
            "posSide": position_side,
            "side": side,
            "sz": size,
            "ordType": order_type,
            "reduceOnly": "true" if reduce_only else "false",
        }
        if client_order_id:
            body["clOrdId"] = client_order_id
        if tp_trigger_price is not None:
            body["tpTriggerPx"] = tp_trigger_price
            body["tpOrdPx"] = tp_order_price if tp_order_price is not None else "-1"
        if sl_trigger_price is not None:
            body["slTriggerPx"] = sl_trigger_price
            body["slOrdPx"] = sl_order_price if sl_order_price is not None else "-1"
        return self._request("POST", "/api/v5/trade/order-algo",
                             body=body, auth=True)

    def get_active_tpsl_orders(self, inst_id: Optional[str] = None) -> Dict:
        params: Dict[str, Any] = {"ordType": "conditional"}
        if inst_id: params["instId"] = inst_id
        return self._request("GET", "/api/v5/trade/orders-algo-pending",
                             params=params, auth=True)

    def cancel_tpsl_orders(self, orders: List[Dict[str, str]]) -> Dict:
        # Each item must have algoId + instId per OKX's batch-cancel API.
        return self._request("POST", "/api/v5/trade/cancel-algos",
                             body=orders, auth=True)


if __name__ == "__main__":
    # Quick sanity probe (public endpoints only).
    api = OkxAPI()
    t = api.get_ticker("BTC-USDT-SWAP")
    print("BTC-USDT-SWAP ticker:", t.get("code"), t.get("msg"))
    c = api.get_candles("BTC-USDT-SWAP", "1H", limit=3)
    print(f"candles: code={c.get('code')} count={len(c.get('data') or [])}")
