#!/usr/bin/env python3
"""OKX v5 REST API Client.

Faithful client for OKX's v5 endpoints. Symbols use OKX's native naming
(`BTC-USDT-SWAP` for perp futures; `BTC-USDT` for spot) — the OkxAdapter
layer above is what normalizes BTC-USDT ↔ BTC-USDT-SWAP for the perp side.

Auth scheme (OK-ACCESS-* headers):
    timestamp = ISO 8601 UTC with millisecond precision
    prehash   = timestamp + method + requestPath + body
    signature = base64(HMAC-SHA256(secret, prehash))   # binary -> base64

Demo trading: pass demo=True; adds `x-simulated-trading: 1` header on the
same base URL (no separate hostname like BloFin).

EU region note (P1 carry build, 2026-05-13): the BASE_URL `https://www.okx.com`
is the global host. OKX EU traffic uses the same v5 endpoints but EU-region
accounts may be served from a region-specific edge; the host below works
for public market data globally. For EU-private endpoints we set
`OKX_API_BASE` (env override) so the carry-runner can point at the EU
host without touching code. Spot trading on OKX EU for retail is
documented as available; perp/swap on OKX EU has a documented leverage
cap of 2× for retail (MiCA). These are flagged as assumptions in
`docs/CARRY-BUILD-PLAN.md` until confirmed live.
"""

import base64
import hashlib
import hmac
import json
import os
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
        base_url: Optional[str] = None,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.demo = demo
        # Allow EU/region override via env or constructor without forking code.
        # If both unset, default to global host.
        self.base_url = base_url or os.environ.get("OKX_API_BASE") or self.BASE_URL
        self.session = requests.Session()
        # OKX sits behind Cloudflare; the default `python-requests/x.y` UA is
        # blocked on many datacenter egress IPs (CF error 1010 -> HTTP 403).
        # Use a browser-shaped UA so requests reach the upstream.
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        })

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
        url = f"{self.base_url}{request_path}"
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

    # =====================  SPOT (added for carry P1)  =====================
    #
    # OKX uses the *same* /api/v5/trade/order endpoint for spot and perp;
    # the difference is `instId` (BTC-USDT for spot vs BTC-USDT-SWAP for
    # perp) and `tdMode` ("cash" for spot un-margined; "cross"/"isolated"
    # for spot-on-margin under unified-margin). `posSide` is not used for
    # spot. Order types and price field are the same.

    def get_spot_ticker(self, inst_id: str = "BTC-USDT") -> Dict:
        """Spot ticker. Identical endpoint to perp ticker but inst_id is the
        spot symbol (no -SWAP suffix)."""
        return self._request("GET", "/api/v5/market/ticker",
                             params={"instId": inst_id})

    def get_spot_instruments(self, inst_id: Optional[str] = None) -> Dict:
        """Spot instrument metadata: minSz, tickSz, lotSz, state, baseCcy,
        quoteCcy, settleCcy, etc. Used to validate min order size."""
        params: Dict[str, Any] = {"instType": "SPOT"}
        if inst_id:
            params["instId"] = inst_id
        return self._request("GET", "/api/v5/public/instruments", params=params)

    def place_spot_order(
        self, inst_id: str, side: str, order_type: str, size: str,
        price: Optional[str] = None,
        td_mode: str = "cash",
        client_order_id: Optional[str] = None,
        target_currency: Optional[str] = None,
    ) -> Dict:
        """Place a SPOT order.

        Args:
            inst_id: spot symbol, e.g. "BTC-USDT" (no -SWAP).
            side: "buy" or "sell".
            order_type: "market" | "limit" | "post_only" | "fok" | "ioc".
            size: order size. For market BUY on OKX spot, size is the
                  *quote* amount (USDT) unless `target_currency=base_ccy`.
                  For limit/post_only, size is in base currency (BTC).
            price: required for limit/post_only/fok/ioc.
            td_mode: "cash" (no margin) or "cross"/"isolated" (margin/unified).
            client_order_id: optional clOrdId.
            target_currency: only used for spot market orders to disambiguate
                  whether `size` is base or quote currency. OKX accepts
                  "base_ccy" or "quote_ccy".

        Returns the raw OKX response. Caller is responsible for symbol
        munging (no -SWAP suffix here — this is the spot endpoint, callers
        pass the spot inst_id directly).
        """
        body: Dict[str, Any] = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": order_type,
            "sz": size,
        }
        if order_type in ("limit", "post_only", "fok", "ioc") and price:
            body["px"] = price
        if client_order_id:
            body["clOrdId"] = client_order_id
        if target_currency:
            body["tgtCcy"] = target_currency
        return self._request("POST", "/api/v5/trade/order", body=body, auth=True)

    def cancel_spot_order(self, inst_id: str, order_id: Optional[str] = None,
                          client_order_id: Optional[str] = None) -> Dict:
        """Cancel a spot order by orderId or clientOrderId."""
        body: Dict[str, Any] = {"instId": inst_id}
        if order_id:
            body["ordId"] = order_id
        elif client_order_id:
            body["clOrdId"] = client_order_id
        else:
            return {"code": "error",
                    "msg": "order_id or client_order_id is required",
                    "data": None}
        return self._request("POST", "/api/v5/trade/cancel-order",
                             body=body, auth=True)

    def get_spot_order_detail(self, inst_id: str,
                              order_id: Optional[str] = None,
                              client_order_id: Optional[str] = None) -> Dict:
        """Get spot order detail. Same endpoint as perp; OKX disambiguates
        by instId."""
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

    def get_spot_active_orders(self, inst_id: Optional[str] = None) -> Dict:
        """List pending spot orders."""
        params: Dict[str, Any] = {"instType": "SPOT"}
        if inst_id:
            params["instId"] = inst_id
        return self._request("GET", "/api/v5/trade/orders-pending",
                             params=params, auth=True)

    def get_spot_fills(self, inst_id: Optional[str] = None,
                       order_id: Optional[str] = None,
                       begin: Optional[str] = None,
                       end: Optional[str] = None,
                       limit: int = 100) -> Dict:
        """Spot fills history."""
        params: Dict[str, Any] = {"instType": "SPOT", "limit": str(limit)}
        if inst_id:
            params["instId"] = inst_id
        if order_id:
            params["ordId"] = order_id
        if begin:
            params["begin"] = begin
        if end:
            params["end"] = end
        return self._request("GET", "/api/v5/trade/fills-history",
                             params=params, auth=True)

    # =====================  Unified-margin awareness  =====================

    def get_account_config(self) -> Dict:
        """Account configuration (alias for get_position_mode — OKX returns
        acctLv + posMode + autoLoan etc. on the same endpoint).

        Key field for the carry strategy:
            acctLv: '1' = Simple, '2' = Single-currency margin,
                    '3' = Multi-currency margin, '4' = Portfolio margin.
        For the carry we want '3' or '4' (spot BTC collateralizes the
        perp short → ~50% effective-yield improvement vs siloed margin).
        """
        return self._request("GET", "/api/v5/account/config", auth=True)


if __name__ == "__main__":
    # Quick sanity probe (public endpoints only).
    api = OkxAPI()
    t = api.get_ticker("BTC-USDT-SWAP")
    print("BTC-USDT-SWAP ticker:", t.get("code"), t.get("msg"))
    c = api.get_candles("BTC-USDT-SWAP", "1H", limit=3)
    print(f"candles: code={c.get('code')} count={len(c.get('data') or [])}")
