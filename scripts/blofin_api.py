#!/usr/bin/env python3
"""
Blofin API Client
Handles authentication and API calls to Blofin exchange
"""

import hmac
import hashlib
import base64
import json
import threading
import time
from collections import deque
from uuid import uuid4
from typing import Optional, Dict, Any, List
import requests


class BlofinAPI:
    """Blofin REST API Client with proper authentication"""
    
    def __init__(self, api_key: str, api_secret: str, passphrase: str, demo: bool = False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase

        # Use demo or production endpoint
        self.base_url = (
            "https://demo-trading-openapi.blofin.com" if demo
            else "https://openapi.blofin.com"
        )

        # Rate limiter: 20 requests per 10 seconds (conservative vs 30 limit)
        self._rate_window = 10.0
        self._rate_max = 20
        self._rate_timestamps: deque = deque()
        self._rate_lock = threading.Lock()

    def _wait_for_rate_limit(self) -> None:
        with self._rate_lock:
            now = time.monotonic()
            # Purge timestamps older than the window
            while self._rate_timestamps and self._rate_timestamps[0] < now - self._rate_window:
                self._rate_timestamps.popleft()
            if len(self._rate_timestamps) >= self._rate_max:
                sleep_until = self._rate_timestamps[0] + self._rate_window
                wait = sleep_until - now
                if wait > 0:
                    time.sleep(wait)
                # Purge again after sleeping
                now = time.monotonic()
                while self._rate_timestamps and self._rate_timestamps[0] < now - self._rate_window:
                    self._rate_timestamps.popleft()
            self._rate_timestamps.append(time.monotonic())
    
    def _sign_request(self, path: str, method: str, body: Optional[Dict] = None) -> tuple:
        """Generate Blofin API signature (hex->base64 encoding!)"""
        timestamp = str(int(time.time() * 1000))
        nonce = str(uuid4())
        
        # Create prehash string
        body_str = json.dumps(body) if body is not None else ""
        prehash = f"{path}{method}{timestamp}{nonce}{body_str}"
        
        # Generate HMAC-SHA256 hex signature
        hex_signature = hmac.new(
            self.api_secret.encode(),
            prehash.encode(),
            hashlib.sha256
        ).hexdigest()
        
        # Convert hex string to bytes, then base64 encode
        signature = base64.b64encode(hex_signature.encode()).decode()
        
        return signature, timestamp, nonce
    
    def _request(self, method: str, path: str, params: Optional[Dict] = None,
                 body: Optional[Dict] = None) -> Dict[str, Any]:
        """Make authenticated API request"""
        self._wait_for_rate_limit()

        # Build full URL with query params for GET
        url = f"{self.base_url}{path}"
        if params and method == "GET":
            query_string = "&".join([f"{k}={v}" for k, v in params.items()])
            path_with_params = f"{path}?{query_string}"
            url = f"{self.base_url}{path_with_params}"
        else:
            path_with_params = path
        
        # Generate signature
        signature, timestamp, nonce = self._sign_request(path_with_params, method, body)
        
        # Build headers
        headers = {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": signature,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-NONCE": nonce,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json"
        }
        
        # Make request
        try:
            if method == "GET":
                response = requests.get(url, headers=headers, timeout=10)
            elif method == "POST":
                response = requests.post(url, headers=headers, json=body, timeout=10)
            else:
                raise ValueError(f"Unsupported method: {method}")

            try:
                data = response.json()
                return data
            except requests.exceptions.JSONDecodeError:
                response.raise_for_status()
                return {"code": "error",
                        "msg": f"Non-JSON response: {response.status_code}",
                        "data": None}

        except requests.exceptions.RequestException as e:
            return {"code": "error", "msg": str(e), "data": None}
    
    # ==================== MARKET DATA ====================
    
    def get_ticker(self, inst_id: str = "BTC-USDT") -> Dict:
        """Get latest price and 24h stats"""
        return self._request("GET", "/api/v1/market/tickers", params={"instId": inst_id})
    
    def get_orderbook(self, inst_id: str = "BTC-USDT", size: int = 5) -> Dict:
        """Get order book"""
        return self._request("GET", "/api/v1/market/books", 
                           params={"instId": inst_id, "size": str(size)})
    
    def get_candles(self, inst_id: str = "BTC-USDT", bar: str = "5m", limit: int = 100,
                   before: Optional[str] = None, after: Optional[str] = None) -> Dict:
        """Get candlestick data

        Args:
            inst_id: Trading pair (e.g. BTC-USDT)
            bar: Timeframe (1m, 5m, 15m, 1H, 4H, 1D, etc.)
            limit: Number of candles (max 1440)
            before: Return candles before this timestamp (ms), for backward pagination
            after: Return candles after this timestamp (ms)
        """
        params = {"instId": inst_id, "bar": bar, "limit": str(limit)}
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        return self._request("GET", "/api/v1/market/candles", params=params)
    
    def get_mark_price(self, inst_id: str = "BTC-USDT") -> Dict:
        """Get index and mark price"""
        return self._request("GET", "/api/v1/market/mark-price", 
                           params={"instId": inst_id})
    
    # ==================== ACCOUNT ====================
    
    def get_balance(self, account_type: str = "futures", currency: Optional[str] = None) -> Dict:
        """Get account balance
        
        Args:
            account_type: funding, futures, spot, copy_trading, earn
            currency: Optional filter (e.g. USDT)
        """
        params = {"accountType": account_type}
        if currency:
            params["currency"] = currency
        return self._request("GET", "/api/v1/asset/balances", params=params)
    
    def transfer_funds(self, currency: str, amount: str, from_account: str, 
                      to_account: str, client_id: Optional[str] = None) -> Dict:
        """Transfer funds between accounts
        
        Args:
            currency: e.g. USDT
            amount: Amount to transfer
            from_account: funding, futures, spot, etc.
            to_account: funding, futures, spot, etc.
            client_id: Optional client ID
        """
        body = {
            "currency": currency,
            "amount": amount,
            "fromAccount": from_account,
            "toAccount": to_account
        }
        if client_id:
            body["clientId"] = client_id
        
        return self._request("POST", "/api/v1/asset/transfer", body=body)
    
    # ==================== TRADING ====================
    
    def place_order(self, inst_id: str, side: str, order_type: str,
                   size: str, price: Optional[str] = None,
                   margin_mode: str = "isolated",
                   position_side: Optional[str] = None,
                   reduce_only: Optional[bool] = None,
                   client_order_id: Optional[str] = None,
                   tp_trigger_price: Optional[str] = None,
                   tp_order_price: Optional[str] = None,
                   sl_trigger_price: Optional[str] = None,
                   sl_order_price: Optional[str] = None) -> Dict:
        """Place a trading order"""
        body = {
            "instId": inst_id,
            "marginMode": margin_mode,
            "side": side,
            "orderType": order_type,
            "size": size,
        }
        if order_type == "limit" and price:
            body["price"] = price
        if position_side:
            body["positionSide"] = position_side
        if reduce_only is not None:
            body["reduceOnly"] = "true" if reduce_only else "false"
        if client_order_id:
            body["clientOrderId"] = client_order_id
        if tp_trigger_price is not None and tp_order_price is not None:
            body["tpTriggerPrice"] = tp_trigger_price
            body["tpOrderPrice"] = tp_order_price
        if sl_trigger_price is not None and sl_order_price is not None:
            body["slTriggerPrice"] = sl_trigger_price
            body["slOrderPrice"] = sl_order_price
        return self._request("POST", "/api/v1/trade/order", body=body)
    
    def cancel_order(self, inst_id: str, order_id: str) -> Dict:
        """Cancel an order"""
        body = {
            "instId": inst_id,
            "orderId": order_id
        }
        return self._request("POST", "/api/v1/trade/cancel-order", body=body)
    
    def get_orders(self, inst_id: Optional[str] = None, state: Optional[str] = None) -> Dict:
        """Get order list
        
        Args:
            inst_id: Optional trading pair filter
            state: live, filled, canceled
        """
        params = {}
        if inst_id:
            params["instId"] = inst_id
        if state:
            params["state"] = state
        
        return self._request("GET", "/api/v1/trade/orders", params=params)

    def get_active_orders(self, inst_id: Optional[str] = None) -> Dict:
        """Get all incomplete orders"""
        params = {}
        if inst_id:
            params["instId"] = inst_id
        return self._request("GET", "/api/v1/trade/orders-pending", params=params)

    def get_order_detail(self, inst_id: str, order_id: Optional[str] = None,
                         client_order_id: Optional[str] = None) -> Dict:
        """Get single order detail"""
        params = {"instId": inst_id}
        if order_id:
            params["orderId"] = order_id
        elif client_order_id:
            params["clientOrderId"] = client_order_id
        else:
            return {"code": "error", "msg": "order_id or client_order_id is required", "data": None}
        return self._request("GET", "/api/v1/trade/order-detail", params=params)

    def place_tpsl_order(self, *, inst_id: str, margin_mode: str, position_side: str,
                         side: str, size: str,
                         tp_trigger_price: Optional[str] = None,
                         tp_order_price: Optional[str] = None,
                         sl_trigger_price: Optional[str] = None,
                         sl_order_price: Optional[str] = None,
                         client_order_id: Optional[str] = None,
                         reduce_only: bool = True,
                         order_type: str = "trigger") -> Dict:
        """Place a standalone TP/SL order on an existing position"""
        body = {
            "instId": inst_id,
            "marginMode": margin_mode,
            "positionSide": position_side,
            "side": side,
            "size": size,
            "orderType": order_type,
            "reduceOnly": "true" if reduce_only else "false",
        }
        if client_order_id:
            body["clientOrderId"] = client_order_id
        if tp_trigger_price is not None:
            body["tpTriggerPrice"] = tp_trigger_price
            body["tpOrderPrice"] = tp_order_price if tp_order_price is not None else "-1"
        if sl_trigger_price is not None:
            body["slTriggerPrice"] = sl_trigger_price
            body["slOrderPrice"] = sl_order_price if sl_order_price is not None else "-1"
        return self._request("POST", "/api/v1/trade/order-tpsl", body=body)

    def get_active_tpsl_orders(self, inst_id: Optional[str] = None) -> Dict:
        """Get all active TP/SL orders"""
        params = {}
        if inst_id:
            params["instId"] = inst_id
        return self._request("GET", "/api/v1/trade/orders-tpsl-pending", params=params)

    def cancel_tpsl_orders(self, orders: List[Dict[str, str]]) -> Dict:
        """Cancel TP/SL orders"""
        return self._request("POST", "/api/v1/trade/cancel-tpsl", body=orders)

    # ==================== POSITIONS ====================

    def get_positions(self, inst_id: Optional[str] = None) -> Dict:
        """Get open positions"""
        params = {}
        if inst_id:
            params["instId"] = inst_id
        return self._request("GET", "/api/v1/account/positions", params=params)

    def get_positions_history(self, inst_id: Optional[str] = None,
                              position_id: Optional[str] = None,
                              begin: Optional[str] = None,
                              end: Optional[str] = None,
                              limit: int = 100) -> Dict:
        params = {"limit": str(limit)}
        if inst_id:
            params["instId"] = inst_id
        if position_id:
            params["positionId"] = position_id
        if begin:
            params["begin"] = begin
        if end:
            params["end"] = end
        return self._request("GET", "/api/v1/account/positions-history",
                             params=params)

    def get_orders_history(self, inst_id: Optional[str] = None,
                           begin: Optional[str] = None,
                           end: Optional[str] = None,
                           limit: int = 100) -> Dict:
        params = {"limit": str(limit)}
        if inst_id:
            params["instId"] = inst_id
        if begin:
            params["begin"] = begin
        if end:
            params["end"] = end
        return self._request("GET", "/api/v1/trade/orders-history",
                             params=params)

    def get_fills_history(self, inst_id: Optional[str] = None,
                          order_id: Optional[str] = None,
                          begin: Optional[str] = None,
                          end: Optional[str] = None,
                          limit: int = 100) -> Dict:
        params = {"limit": str(limit)}
        if inst_id:
            params["instId"] = inst_id
        if order_id:
            params["orderId"] = order_id
        if begin:
            params["begin"] = begin
        if end:
            params["end"] = end
        return self._request("GET", "/api/v1/trade/fills-history",
                             params=params)

    def get_position_mode(self) -> Dict:
        """Get account position mode"""
        return self._request("GET", "/api/v1/account/position-mode")


if __name__ == "__main__":
    # Quick test (needs credentials)
    import os
    
    api_key = os.getenv("BLOFIN_API_KEY")
    api_secret = os.getenv("BLOFIN_API_SECRET")
    passphrase = os.getenv("BLOFIN_PASSPHRASE")
    
    if api_key and api_secret and passphrase:
        client = BlofinAPI(api_key, api_secret, passphrase, demo=False)
        
        # Test market data (no auth needed)
        ticker = client.get_ticker("BTC-USDT")
        print(f"BTC-USDT Ticker: {ticker}")
        
        # Test balance (needs auth)
        balance = client.get_balance("futures", "USDT")
        print(f"Futures Balance: {balance}")
    else:
        print("Set BLOFIN_API_KEY, BLOFIN_API_SECRET, and BLOFIN_PASSPHRASE to test")
