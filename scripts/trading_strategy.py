#!/usr/bin/env python3
"""
Trading Strategies for Blofin Bot
Implements various trading strategies with risk management
"""

import pandas as pd
import numpy as np
from typing import Dict, Optional, List, Tuple
from dataclasses import dataclass


@dataclass
class Signal:
    """Trading signal"""
    action: str  # "buy", "sell", "hold"
    confidence: float  # 0.0 to 1.0
    reason: str
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


class TradingStrategy:
    """Base trading strategy class"""
    
    def __init__(self, config: Dict):
        self.config = config
        self.name = "Base Strategy"
    
    def analyze(self, candles: List, current_price: float) -> Signal:
        """Analyze market and return signal"""
        raise NotImplementedError("Implement in subclass")
    
    def calculate_position_size(self, balance: float, price: float, 
                               risk_percent: float) -> float:
        """Calculate position size based on risk management
        
        Args:
            balance: Available balance in USDT
            price: Current asset price
            risk_percent: Risk per trade as percentage (e.g. 10 = 10%)
        
        Returns:
            Position size in contracts (min 0.1 for BTC-USDT)
        """
        # Risk amount in USDT
        risk_amount = balance * (risk_percent / 100)
        
        # Calculate contracts (1 contract = 0.001 BTC for BTC-USDT)
        contract_value = 0.001  # BTC per contract
        contracts = (risk_amount / price) / contract_value
        
        # Round to 0.1 precision (minimum order size)
        contracts = max(0.1, round(contracts, 1))
        
        return contracts


class RSIMeanReversion(TradingStrategy):
    """RSI-based mean reversion strategy
    
    Buy when RSI is oversold, sell when overbought
    Good for ranging/sideways markets
    """
    
    def __init__(self, config: Dict):
        super().__init__(config)
        self.name = "RSI Mean Reversion"
        
        # Strategy parameters
        self.rsi_period = config.get("rsi_period", 14)
        self.rsi_oversold = config.get("rsi_oversold", 30)
        self.rsi_overbought = config.get("rsi_overbought", 70)
        self.stop_loss_pct = config.get("stop_loss_pct", 3.0)
        self.take_profit_pct = config.get("take_profit_pct", 2.0)
    
    def calculate_rsi(self, prices: List[float]) -> float:
        """Calculate RSI using Wilder's smoothing"""
        if len(prices) < self.rsi_period + 1:
            return 50.0  # Neutral if not enough data

        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        # Seed with SMA of first period values
        avg_gain = float(np.mean(gains[:self.rsi_period]))
        avg_loss = float(np.mean(losses[:self.rsi_period]))

        # Apply Wilder's exponential smoothing for remaining values
        for i in range(self.rsi_period, len(gains)):
            avg_gain = (avg_gain * (self.rsi_period - 1) + gains[i]) / self.rsi_period
            avg_loss = (avg_loss * (self.rsi_period - 1) + losses[i]) / self.rsi_period

        if avg_gain == 0 and avg_loss == 0:
            return 50.0  # Flat price = neutral
        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
    def analyze(self, candles: List, current_price: float) -> Signal:
        """Analyze candles and generate signal
        
        Args:
            candles: List of [timestamp, open, high, low, close, volume, ...]
            current_price: Current market price
        """
        # Extract close prices
        close_prices = [float(candle[4]) for candle in candles]
        
        if len(close_prices) < self.rsi_period + 1:
            return Signal("hold", 0.0, "Not enough data for RSI calculation")
        
        # Calculate RSI
        rsi = self.calculate_rsi(close_prices)
        
        # Generate signal
        if rsi < self.rsi_oversold:
            # Oversold - potential buy
            confidence = (self.rsi_oversold - rsi) / self.rsi_oversold
            stop_loss = current_price * (1 - self.stop_loss_pct / 100)
            take_profit = current_price * (1 + self.take_profit_pct / 100)
            
            return Signal(
                action="buy",
                confidence=min(confidence, 1.0),
                reason=f"RSI oversold: {rsi:.2f} < {self.rsi_oversold}",
                stop_loss=stop_loss,
                take_profit=take_profit
            )
        
        elif rsi > self.rsi_overbought:
            # Overbought - potential sell
            confidence = (rsi - self.rsi_overbought) / (100 - self.rsi_overbought)
            stop_loss = current_price * (1 + self.stop_loss_pct / 100)
            take_profit = current_price * (1 - self.take_profit_pct / 100)
            
            return Signal(
                action="sell",
                confidence=min(confidence, 1.0),
                reason=f"RSI overbought: {rsi:.2f} > {self.rsi_overbought}",
                stop_loss=stop_loss,
                take_profit=take_profit
            )
        
        else:
            # Neutral zone
            return Signal(
                action="hold",
                confidence=0.0,
                reason=f"RSI neutral: {rsi:.2f} (waiting for {self.rsi_oversold} or {self.rsi_overbought})"
            )


class TrendFollowing(TradingStrategy):
    """EMA crossover trend following strategy
    
    Buy when fast EMA crosses above slow EMA
    Sell when fast EMA crosses below slow EMA
    Good for trending markets
    """
    
    def __init__(self, config: Dict):
        super().__init__(config)
        self.name = "EMA Trend Following"
        
        self.fast_period = config.get("fast_ema", 9)
        self.slow_period = config.get("slow_ema", 21)
        self.stop_loss_pct = config.get("stop_loss_pct", 4.0)
        self.take_profit_pct = config.get("take_profit_pct", 8.0)
    
    def calculate_ema(self, prices: List[float], period: int) -> float:
        """Calculate Exponential Moving Average using full history"""
        if len(prices) < period:
            return float(np.mean(prices))

        prices_array = np.array(prices, dtype=float)
        alpha = 2 / (period + 1)

        # Seed with SMA of first period prices
        ema = float(np.mean(prices_array[:period]))
        # Run forward through all remaining prices
        for price in prices_array[period:]:
            ema = alpha * float(price) + (1 - alpha) * ema

        return ema
    
    def analyze(self, candles: List, current_price: float) -> Signal:
        """Analyze trend and generate signal"""
        close_prices = [float(candle[4]) for candle in candles]
        
        if len(close_prices) < max(self.fast_period, self.slow_period):
            return Signal("hold", 0.0, "Not enough data for EMA calculation")
        
        # Calculate EMAs
        fast_ema = self.calculate_ema(close_prices, self.fast_period)
        slow_ema = self.calculate_ema(close_prices, self.slow_period)
        
        # Previous values for crossover detection
        prev_fast = self.calculate_ema(close_prices[:-1], self.fast_period)
        prev_slow = self.calculate_ema(close_prices[:-1], self.slow_period)
        
        # Detect crossovers
        bullish_cross = prev_fast <= prev_slow and fast_ema > slow_ema
        bearish_cross = prev_fast >= prev_slow and fast_ema < slow_ema
        
        if bullish_cross:
            confidence = abs(fast_ema - slow_ema) / slow_ema
            return Signal(
                action="buy",
                confidence=min(confidence, 1.0),
                reason=f"Bullish EMA crossover: Fast={fast_ema:.2f} > Slow={slow_ema:.2f}",
                stop_loss=current_price * (1 - self.stop_loss_pct / 100),
                take_profit=current_price * (1 + self.take_profit_pct / 100)
            )
        
        elif bearish_cross:
            confidence = abs(fast_ema - slow_ema) / slow_ema
            return Signal(
                action="sell",
                confidence=min(confidence, 1.0),
                reason=f"Bearish EMA crossover: Fast={fast_ema:.2f} < Slow={slow_ema:.2f}",
                stop_loss=current_price * (1 + self.stop_loss_pct / 100),
                take_profit=current_price * (1 - self.take_profit_pct / 100)
            )
        
        else:
            trend = "bullish" if fast_ema > slow_ema else "bearish"
            return Signal(
                action="hold",
                confidence=0.0,
                reason=f"No crossover. Trend: {trend} (Fast={fast_ema:.2f}, Slow={slow_ema:.2f})"
            )


class GridTrading(TradingStrategy):
    """Grid trading strategy
    
    Places buy/sell orders at regular price intervals
    Profits from market volatility without predicting direction
    """
    
    def __init__(self, config: Dict):
        super().__init__(config)
        self.name = "Grid Trading"
        
        self.grid_levels = config.get("grid_levels", 5)
        self.grid_spacing_pct = config.get("grid_spacing_pct", 2.0)
        self.base_price = None
    
    def analyze(self, candles: List, current_price: float) -> Signal:
        """Generate grid trading signals"""
        # Set base price on first run
        if self.base_price is None:
            self.base_price = current_price
        
        # Calculate grid levels
        price_diff_pct = ((current_price - self.base_price) / self.base_price) * 100
        
        # Buy at lower grid levels
        if price_diff_pct <= -self.grid_spacing_pct:
            return Signal(
                action="buy",
                confidence=0.8,
                reason=f"Grid buy level: {price_diff_pct:.2f}% below base ({self.base_price:.2f})",
                stop_loss=None,  # Grid trading doesn't use stop losses
                take_profit=current_price * (1 + self.grid_spacing_pct / 100)
            )
        
        # Sell at upper grid levels
        elif price_diff_pct >= self.grid_spacing_pct:
            return Signal(
                action="sell",
                confidence=0.8,
                reason=f"Grid sell level: {price_diff_pct:.2f}% above base ({self.base_price:.2f})",
                stop_loss=None,
                take_profit=current_price * (1 - self.grid_spacing_pct / 100)
            )
        
        else:
            return Signal(
                action="hold",
                confidence=0.0,
                reason=f"Within grid range: {price_diff_pct:.2f}% from base"
            )


# Strategy factory
def create_strategy(strategy_name: str, config: Dict) -> TradingStrategy:
    """Create strategy instance by name"""
    # Import advanced strategies if needed
    if strategy_name.lower() == "advanced":
        try:
            from advanced_strategy import MultiIndicatorConfluence
            return MultiIndicatorConfluence(config)
        except ImportError:
            raise ValueError("Advanced strategy module not found")
    
    strategies = {
        "rsi": RSIMeanReversion,
        "trend": TrendFollowing,
        "grid": GridTrading
    }
    
    strategy_class = strategies.get(strategy_name.lower())
    if not strategy_class:
        raise ValueError(f"Unknown strategy: {strategy_name}. Available: {list(strategies.keys()) + ['advanced']}")
    
    return strategy_class(config)


if __name__ == "__main__":
    # Test RSI strategy with sample data
    config = {
        "rsi_period": 14,
        "rsi_oversold": 30,
        "rsi_overbought": 70,
        "stop_loss_pct": 3.0,
        "take_profit_pct": 2.0
    }
    
    strategy = RSIMeanReversion(config)
    
    # Fake candle data: [timestamp, open, high, low, close, volume, ...]
    fake_candles = [
        [1, 100, 105, 95, 98, 1000, 10, 1000, 1],
        [2, 98, 100, 90, 92, 1100, 11, 1100, 1],
        [3, 92, 95, 85, 87, 1200, 12, 1200, 1],
        # ... continuing downtrend for oversold RSI
    ] * 5
    
    signal = strategy.analyze(fake_candles, 87.0)
    print(f"Signal: {signal.action} ({signal.confidence:.2f}) - {signal.reason}")
    if signal.stop_loss:
        print(f"Stop Loss: {signal.stop_loss:.2f}, Take Profit: {signal.take_profit:.2f}")
