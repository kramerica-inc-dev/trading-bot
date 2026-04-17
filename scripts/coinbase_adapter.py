#!/usr/bin/env python3
"""
Coinbase Exchange Adapter
Uses official Coinbase Advanced Trade library (coinbase-advanced-py)

FIXES (2026-02-15):
- Pagination: get_accounts() returns max 49 per page, now fetches ALL pages
- USDC support: recognizes USDC as trading currency (not just USD)
- Trading pairs: supports BTC-USDC, ETH-USDC, SOL-USDC
"""

import time
from typing import Dict, Optional, List
from coinbase.rest import RESTClient
from exchange_adapter import ExchangeAdapter


class CoinbaseAdapter(ExchangeAdapter):
    """Adapter for Coinbase Advanced Trade API using official library"""
    
    def __init__(self, config: Dict):
        super().__init__(config)
        
        # Initialize official Coinbase client
        self.client = RESTClient(
            api_key=config.get("api_key", ""),
            api_secret=config.get("api_secret", "")
        )
        
        # Map Blofin-style instrument IDs to Coinbase product IDs
        # Support both USD and USDC pairs
        self.instrument_map = {
            "BTC-USDT": "BTC-USDC",
            "ETH-USDT": "ETH-USDC",
            "SOL-USDT": "SOL-USDC",
            "BTC-USDC": "BTC-USDC",
            "ETH-USDC": "ETH-USDC",
            "SOL-USDC": "SOL-USDC",
            "BTC-USD": "BTC-USD",
            "ETH-USD": "ETH-USD",
            "SOL-USD": "SOL-USD",
        }
        
        # Reverse map for responses
        self.reverse_map = {v: k for k, v in self.instrument_map.items()}
    
    def _map_instrument(self, inst_id: str) -> str:
        """Map Blofin-style instrument to Coinbase product ID"""
        return self.instrument_map.get(inst_id, inst_id)
    
    def _normalize_response(self, data, success: bool = True, msg: str = "success") -> Dict:
        """Normalize response to Blofin format"""
        return {
            "code": "0" if success else "error",
            "msg": msg,
            "data": data
        }
    
    def _get_all_accounts(self):
        """Fetch ALL accounts with pagination (Coinbase returns max 49 per page)"""
        all_accounts = []
        cursor = None
        
        for _ in range(10):  # Safety limit: max 10 pages (~490 accounts)
            if cursor:
                result = self.client.get_accounts(cursor=cursor)
            else:
                result = self.client.get_accounts()
            
            all_accounts.extend(result.accounts)
            
            has_next = getattr(result, "has_next", False)
            next_cursor = getattr(result, "cursor", None)
            
            if not has_next or not next_cursor:
                break
            cursor = next_cursor
        
        return all_accounts
    
    def get_balance(self, account_type: str = "futures", 
                   currency: Optional[str] = None) -> Dict:
        """Get account balance
        
        Note: Coinbase doesn't have separate futures/spot accounts
        Returns USDC balance (normalized to USDT in response for compatibility)
        """
        try:
            accounts = self._get_all_accounts()
            
            # Map target currency: USDT -> USDC for Coinbase
            if currency in ["USDT", "USD"]:
                target_currency = "USDC"
            elif currency:
                target_currency = currency
            else:
                target_currency = None
            
            balances = []
            for account in accounts:
                acc_currency = getattr(account, 'currency', '')
                
                # If target specified, filter
                if target_currency and acc_currency != target_currency:
                    continue
                
                # Get available balance
                available_balance = getattr(account, 'available_balance', {})
                if isinstance(available_balance, dict):
                    available_value = available_balance.get('value', '0')
                else:
                    available_value = getattr(available_balance, 'value', '0')
                
                # Skip zero balances when no specific currency requested
                if target_currency is None and float(available_value) == 0:
                    continue
                
                # Normalize currency name (USDC → USDT for bot compatibility)
                if acc_currency in ("USD", "USDC"):
                    currency_name = "USDT"
                else:
                    currency_name = acc_currency
                
                balances.append({
                    "currency": currency_name,
                    "available": str(available_value),
                    "frozen": "0"
                })
            
            return self._normalize_response(balances)
        
        except Exception as e:
            return self._normalize_response(None, False, str(e))
    
    def get_ticker(self, inst_id: str = "BTC-USDT") -> Dict:
        """Get current ticker/price"""
        try:
            product_id = self._map_instrument(inst_id)
            result = self.client.get_product(product_id)
            
            ticker_data = [{
                "instId": inst_id,
                "last": str(getattr(result, 'price', '0')),
                "vol24h": str(getattr(result, 'volume_24h', '0')),
                "bid": str(getattr(result, 'bid', '0')),
                "ask": str(getattr(result, 'ask', '0'))
            }]
            
            return self._normalize_response(ticker_data)
        
        except Exception as e:
            return self._normalize_response(None, False, str(e))
    
    def get_candles(self, inst_id: str = "BTC-USDT", bar: str = "5m",
                   limit: int = 100) -> Dict:
        """Get historical candlestick data"""
        try:
            product_id = self._map_instrument(inst_id)
            
            granularity_map = {
                "1m": "ONE_MINUTE",
                "5m": "FIVE_MINUTE",
                "15m": "FIFTEEN_MINUTE",
                "1h": "ONE_HOUR",
                "4h": "FOUR_HOUR",
                "1d": "ONE_DAY"
            }
            granularity = granularity_map.get(bar, "FIVE_MINUTE")
            
            end_time = int(time.time())
            seconds_map = {
                "ONE_MINUTE": 60, "FIVE_MINUTE": 300,
                "FIFTEEN_MINUTE": 900, "ONE_HOUR": 3600,
                "FOUR_HOUR": 14400, "ONE_DAY": 86400
            }
            seconds_per_candle = seconds_map.get(granularity, 300)
            start_time = end_time - (limit * seconds_per_candle)
            
            result = self.client.get_candles(
                product_id=product_id,
                start=str(start_time),
                end=str(end_time),
                granularity=granularity
            )
            
            candles_data = result.candles
            
            formatted_candles = []
            for candle in candles_data:
                formatted_candles.append([
                    str(int(getattr(candle, 'start', 0)) * 1000),
                    str(getattr(candle, 'open', '0')),
                    str(getattr(candle, 'high', '0')),
                    str(getattr(candle, 'low', '0')),
                    str(getattr(candle, 'close', '0')),
                    str(getattr(candle, 'volume', '0'))
                ])
            
            formatted_candles.reverse()
            return self._normalize_response(formatted_candles)
        
        except Exception as e:
            return self._normalize_response(None, False, str(e))
    
    def place_order(self, inst_id: str, side: str, order_type: str,
                   size: str, price: Optional[str] = None,
                   margin_mode: str = "isolated") -> Dict:
        """Place an order (spot only, margin_mode ignored)"""
        try:
            product_id = self._map_instrument(inst_id)
            client_order_id = f"bot-{int(time.time() * 1000)}"
            
            if order_type.lower() == "market":
                if side.lower() == "buy":
                    result = self.client.market_order_buy(
                        client_order_id=client_order_id,
                        product_id=product_id,
                        quote_size=size
                    )
                else:
                    result = self.client.market_order_sell(
                        client_order_id=client_order_id,
                        product_id=product_id,
                        base_size=size
                    )
            else:
                if not price:
                    return self._normalize_response(None, False, "Price required for limit orders")
                
                if side.lower() == "buy":
                    result = self.client.limit_order_gtc_buy(
                        client_order_id=client_order_id,
                        product_id=product_id,
                        base_size=size,
                        limit_price=price,
                        post_only=True
                    )
                else:
                    result = self.client.limit_order_gtc_sell(
                        client_order_id=client_order_id,
                        product_id=product_id,
                        base_size=size,
                        limit_price=price,
                        post_only=True
                    )
            
            order_data = [{
                "orderId": str(getattr(result, 'order_id', '')),
                "instId": inst_id,
                "side": side,
                "orderType": order_type,
                "size": size,
                "price": price
            }]
            
            return self._normalize_response(order_data)
        
        except Exception as e:
            return self._normalize_response(None, False, str(e))
    
    def cancel_order(self, inst_id: str, order_id: str) -> Dict:
        """Cancel an order"""
        try:
            result = self.client.cancel_orders(order_ids=[order_id])
            return self._normalize_response(result)
        except Exception as e:
            return self._normalize_response(None, False, str(e))
    
    def get_orders(self, inst_id: Optional[str] = None,
                  state: Optional[str] = None) -> Dict:
        """Get order history"""
        try:
            state_map = {
                "filled": "FILLED",
                "live": "OPEN",
                "canceled": "CANCELLED"
            }
            
            product_id = self._map_instrument(inst_id) if inst_id else None
            
            result = self.client.get_orders(
                product_id=product_id,
                order_status=[state_map.get(state.lower(), "OPEN")] if state else None
            )
            
            orders_data = result.orders
            
            formatted_orders = []
            for order in orders_data:
                order_product = getattr(order, 'product_id', '')
                formatted_orders.append({
                    "orderId": str(getattr(order, 'order_id', '')),
                    "instId": self.reverse_map.get(order_product, order_product),
                    "state": str(getattr(order, 'status', '')).lower(),
                    "side": str(getattr(order, 'side', '')).lower(),
                    "size": str(getattr(order, 'size', '0')),
                    "price": str(getattr(order, 'average_filled_price', getattr(order, 'limit_price', '0')))
                })
            
            return self._normalize_response(formatted_orders)
        
        except Exception as e:
            return self._normalize_response(None, False, str(e))

    def get_positions(self, inst_id: Optional[str] = None) -> Dict:
        """Coinbase spot mode: no derivatives positions to reconcile."""
        return self._normalize_response([])

    def get_active_orders(self, inst_id: Optional[str] = None) -> Dict:
        return self.get_orders(inst_id=inst_id, state="live")


if __name__ == "__main__":
    config = {
        "api_key": "YOUR_COINBASE_API_KEY",
        "api_secret": "YOUR_COINBASE_API_SECRET"
    }
    
    adapter = CoinbaseAdapter(config)
    print(f"✅ {adapter.name} adapter initialized")
    print(f"Instrument mapping: BTC-USDT → {adapter._map_instrument('BTC-USDT')}")
    
    print("\nTest 1: Get balance (with pagination)...")
    balance = adapter.get_balance()
    print(f"Result: {balance}")
    
    print("\nTest 2: Get USDT/USDC balance...")
    balance_usdt = adapter.get_balance(currency="USDT")
    print(f"Result: {balance_usdt}")
    
    print("\nTest 3: Get ticker BTC-USDC...")
    ticker = adapter.get_ticker("BTC-USDT")
    print(f"Result: {ticker}")
