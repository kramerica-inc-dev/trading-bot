#!/usr/bin/env python3
"""
Blofin Exchange Adapter
Wraps the existing BlofinAPI to conform to ExchangeAdapter interface
"""

from typing import Any, Dict, Optional, List
from exchange_adapter import ExchangeAdapter
from blofin_api import BlofinAPI


class BlofinAdapter(ExchangeAdapter):
    """Adapter for Blofin exchange using existing BlofinAPI"""
    
    def __init__(self, config: Dict):
        super().__init__(config)
        
        # Extract Blofin credentials from config
        self.api = BlofinAPI(
            api_key=config.get("api_key", ""),
            api_secret=config.get("api_secret", ""),
            passphrase=config.get("passphrase", ""),
            demo=config.get("demo_mode", False)
        )
    
    def get_balance(self, account_type: str = "futures", 
                   currency: Optional[str] = None) -> Dict:
        """Get account balance - passthrough to BlofinAPI"""
        return self.api.get_balance(account_type, currency)
    
    def get_ticker(self, inst_id: str = "BTC-USDT") -> Dict:
        """Get current ticker - passthrough to BlofinAPI"""
        return self.api.get_ticker(inst_id)
    
    def get_candles(self, inst_id: str = "BTC-USDT", bar: str = "5m",
                   limit: int = 100, before: str = None, after: str = None) -> List:
        """Get historical candles - extract data from API response"""
        response = self.api.get_candles(inst_id, bar, limit, before=before, after=after)
        
        # Extract candle data from response
        # Blofin API returns: {'code': '0', 'msg': 'success', 'data': [[...]]}
        if isinstance(response, dict) and 'data' in response:
            return response['data']
        elif isinstance(response, list):
            return response  # Already in correct format
        else:
            raise ValueError(f"Unexpected candles response format: {type(response)}")
    
    def place_order(self, inst_id: str, side: str, order_type: str,
                   size: str, price: Optional[str] = None,
                   margin_mode: str = "isolated", **kwargs: Any) -> Dict:
        """Place order - passthrough to BlofinAPI"""
        return self.api.place_order(
            inst_id=inst_id,
            side=side,
            order_type=order_type,
            size=size,
            price=price,
            margin_mode=margin_mode,
            **kwargs
        )
    
    def cancel_order(self, inst_id: str, order_id: str) -> Dict:
        """Cancel order - passthrough to BlofinAPI"""
        return self.api.cancel_order(inst_id, order_id)
    
    def get_orders(self, inst_id: Optional[str] = None,
                  state: Optional[str] = None) -> Dict:
        """Get orders - passthrough to BlofinAPI"""
        return self.api.get_orders(inst_id, state)

    def get_positions(self, inst_id: Optional[str] = None) -> Dict:
        return self.api.get_positions(inst_id)

    def get_active_orders(self, inst_id: Optional[str] = None) -> Dict:
        return self.api.get_active_orders(inst_id)

    def get_position_mode(self) -> Dict:
        return self.api.get_position_mode()

    def get_order_detail(self, inst_id: str, order_id: Optional[str] = None,
                         client_order_id: Optional[str] = None) -> Dict:
        return self.api.get_order_detail(inst_id, order_id=order_id,
                                          client_order_id=client_order_id)

    def place_tpsl_order(self, *, inst_id: str, margin_mode: str, position_side: str,
                         side: str, size: str, **kwargs: Any) -> Dict:
        return self.api.place_tpsl_order(
            inst_id=inst_id, margin_mode=margin_mode,
            position_side=position_side, side=side, size=size, **kwargs)

    def get_active_tpsl_orders(self, inst_id: Optional[str] = None) -> Dict:
        return self.api.get_active_tpsl_orders(inst_id)

    def cancel_tpsl_orders(self, orders: List[Dict]) -> Dict:
        return self.api.cancel_tpsl_orders(orders)

    def get_orders_history(self, inst_id: Optional[str] = None, **kwargs) -> Dict:
        return self.api.get_orders_history(inst_id=inst_id, **kwargs)

    def get_fills_history(self, inst_id: Optional[str] = None, **kwargs) -> Dict:
        return self.api.get_fills_history(inst_id=inst_id, **kwargs)

    def get_positions_history(self, inst_id: Optional[str] = None, **kwargs) -> Dict:
        return self.api.get_positions_history(inst_id=inst_id, **kwargs)
