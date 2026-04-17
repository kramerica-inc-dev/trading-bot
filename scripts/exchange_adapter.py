#!/usr/bin/env python3
"""
Exchange Adapter - Abstract Interface
Allows switching between exchanges via config
"""

from abc import ABC, abstractmethod
from typing import Dict, Optional, Any, List


class ExchangeAdapter(ABC):
    """Abstract base class for exchange adapters"""
    
    def __init__(self, config: Dict):
        self.config = config
        self.name = self.__class__.__name__.replace('Adapter', '')
    
    @abstractmethod
    def get_balance(self, account_type: str = "futures", currency: Optional[str] = None) -> Dict:
        """Get account balance
        
        Returns:
            {
                "code": "0" for success, "error" for failure,
                "msg": error message if any,
                "data": [
                    {
                        "currency": "USDT",
                        "available": "115.56",
                        "frozen": "0.00"
                    }
                ]
            }
        """
        pass
    
    @abstractmethod
    def get_ticker(self, inst_id: str = "BTC-USDT") -> Dict:
        """Get current ticker/price
        
        Returns:
            {
                "code": "0",
                "data": [
                    {
                        "instId": "BTC-USDT",
                        "last": "70000.00",
                        "vol24h": "1234.56",
                        ...
                    }
                ]
            }
        """
        pass
    
    @abstractmethod
    def get_candles(self, inst_id: str = "BTC-USDT", bar: str = "5m",
                   limit: int = 100, before: str = None, after: str = None) -> Dict:
        """Get historical candlestick data
        
        Args:
            inst_id: Trading pair (e.g., "BTC-USDT")
            bar: Timeframe (e.g., "5m", "1h", "1d")
            limit: Number of candles to return
        
        Returns:
            {
                "code": "0",
                "data": [
                    ["timestamp", "open", "high", "low", "close", "volume"],
                    ...
                ]
            }
        """
        pass
    
    @abstractmethod
    def place_order(self, inst_id: str, side: str, order_type: str,
                   size: str, price: Optional[str] = None,
                   margin_mode: str = "isolated", **kwargs: Any) -> Dict:
        """Place an order"""
        pass
    
    @abstractmethod
    def cancel_order(self, inst_id: str, order_id: str) -> Dict:
        """Cancel an order
        
        Returns:
            {
                "code": "0",
                "msg": "success"
            }
        """
        pass
    
    @abstractmethod
    def get_orders(self, inst_id: Optional[str] = None, 
                  state: Optional[str] = None) -> Dict:
        """Get order history
        
        Args:
            inst_id: Filter by trading pair
            state: Filter by state (e.g., "filled", "live", "canceled")
        
        Returns:
            {
                "code": "0",
                "data": [
                    {
                        "orderId": "123",
                        "instId": "BTC-USDT",
                        "state": "filled",
                        ...
                    }
                ]
            }
        """
        pass

    def get_positions(self, inst_id: Optional[str] = None) -> Dict:
        """Get open positions. Override in subclass if supported."""
        return {"code": "unsupported", "msg": "get_positions not implemented", "data": []}

    def get_active_orders(self, inst_id: Optional[str] = None) -> Dict:
        """Get incomplete orders. Override in subclass if supported."""
        return self.get_orders(inst_id=inst_id, state="live")

    def get_position_mode(self) -> Dict:
        """Get position mode. Override in subclass if supported."""
        return {"code": "unsupported", "msg": "get_position_mode not implemented", "data": {}}

    def get_order_detail(self, inst_id: str, order_id: Optional[str] = None,
                         client_order_id: Optional[str] = None) -> Dict:
        return {"code": "unsupported", "msg": "get_order_detail not implemented", "data": None}

    def place_tpsl_order(self, *, inst_id: str, margin_mode: str, position_side: str,
                         side: str, size: str, **kwargs: Any) -> Dict:
        return {"code": "unsupported", "msg": "place_tpsl_order not implemented", "data": None}

    def get_active_tpsl_orders(self, inst_id: Optional[str] = None) -> Dict:
        return {"code": "unsupported", "msg": "get_active_tpsl_orders not implemented", "data": []}

    def cancel_tpsl_orders(self, orders: List[Dict]) -> Dict:
        return {"code": "unsupported", "msg": "cancel_tpsl_orders not implemented", "data": []}

    def get_orders_history(self, inst_id: str, *, begin: str = "",
                           end: str = "", limit: int = 50) -> Dict:
        return {"code": "unsupported", "msg": "get_orders_history not implemented", "data": []}

    def get_fills_history(self, inst_id: str, *, order_id: Optional[str] = None,
                          begin: str = "", end: str = "", limit: int = 50) -> Dict:
        return {"code": "unsupported", "msg": "get_fills_history not implemented", "data": []}

    def get_positions_history(self, inst_id: str, *, position_id: Optional[str] = None,
                              begin: str = "", end: str = "", limit: int = 50) -> Dict:
        return {"code": "unsupported", "msg": "get_positions_history not implemented", "data": []}

    def get_capabilities(self) -> Dict[str, bool]:
        """Report which features this exchange adapter supports.

        Subclasses can override for accuracy. The default checks whether
        each optional method is overridden from the base class.
        """
        base = ExchangeAdapter
        cls = type(self)
        return {
            "server_side_tpsl": cls.place_tpsl_order is not base.place_tpsl_order,
            "active_tpsl_query": cls.get_active_tpsl_orders is not base.get_active_tpsl_orders,
            "position_query": cls.get_positions is not base.get_positions,
            "order_detail": cls.get_order_detail is not base.get_order_detail,
            "orders_history": cls.get_orders_history is not base.get_orders_history,
            "fills_history": cls.get_fills_history is not base.get_fills_history,
            "positions_history": cls.get_positions_history is not base.get_positions_history,
        }


def create_exchange_adapter(exchange_name: str, config: Dict) -> ExchangeAdapter:
    """Factory function to create the appropriate exchange adapter
    
    Args:
        exchange_name: "blofin" or "coinbase"
        config: Exchange-specific configuration
    
    Returns:
        ExchangeAdapter instance
    """
    exchange_name = exchange_name.lower()
    
    if exchange_name == "blofin":
        from blofin_adapter import BlofinAdapter
        return BlofinAdapter(config)
    
    elif exchange_name == "coinbase":
        from coinbase_adapter import CoinbaseAdapter
        return CoinbaseAdapter(config)
    
    else:
        raise ValueError(f"Unsupported exchange: {exchange_name}. "
                        f"Supported: blofin, coinbase")
