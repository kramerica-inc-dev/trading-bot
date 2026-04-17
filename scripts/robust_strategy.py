#!/usr/bin/env python3
"""Lower-variance strategy designed to reduce overfitting.

Principles:
- keep parameter count low
- only trade in aligned higher-timeframe trend
- use pullback entries instead of breakout chasing
- avoid trades in noisy/high-volatility chop
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np

from trading_strategy import Signal, TradingStrategy


@dataclass
class RobustSignal(Signal):
    atr: Optional[float] = None
    regime: Optional[str] = None
    risk_multiplier: float = 1.0
    quality_score: float = 0.0
    max_hold_bars: Optional[int] = None


class RobustTrendPullback(TradingStrategy):
    """Trend-pullback model with low parameter count and explicit no-trade zones."""

    def __init__(self, config: Dict):
        super().__init__(config)
        self.name = "Robust Trend Pullback"
        self.fast_ema = int(config.get("fast_ema", 20))
        self.slow_ema = int(config.get("slow_ema", 50))
        self.anchor_ema = int(config.get("anchor_ema", 200))
        self.rsi_period = int(config.get("rsi_period", 14))
        self.atr_period = int(config.get("atr_period", 14))
        self.pullback_atr_multiple = float(config.get("pullback_atr_multiple", 0.6))
        self.stop_atr_multiple = float(config.get("stop_atr_multiple", 1.8))
        self.take_profit_atr_multiple = float(config.get("take_profit_atr_multiple", 2.8))
        self.min_trend_strength = float(config.get("min_trend_strength", 0.0025))
        self.max_atr_pct = float(config.get("max_atr_pct", 0.018))
        self.min_atr_pct = float(config.get("min_atr_pct", 0.002))
        self.long_rsi_floor = float(config.get("long_rsi_floor", 45.0))
        self.short_rsi_ceiling = float(config.get("short_rsi_ceiling", 55.0))
        self.max_hold_bars_default = int(config.get("max_hold_bars", 36))

    def analyze(self, candles: List, current_price: float) -> RobustSignal:
        if len(candles) < max(self.anchor_ema + 5, self.atr_period + 5, self.rsi_period + 5):
            return RobustSignal("hold", 0.0, "not enough data")

        closes = np.array([float(c[4]) for c in candles], dtype=float)
        highs = np.array([float(c[2]) for c in candles], dtype=float)
        lows = np.array([float(c[3]) for c in candles], dtype=float)

        ema_fast = self._ema(closes, self.fast_ema)
        ema_slow = self._ema(closes, self.slow_ema)
        ema_anchor = self._ema(closes, self.anchor_ema)
        atr = self._atr(highs, lows, closes, self.atr_period)
        rsi = self._rsi(closes, self.rsi_period)
        price = float(current_price)
        atr_pct = atr / price if price > 0 else 0.0
        trend_strength = abs(ema_fast - ema_slow) / price if price > 0 else 0.0

        # Explicit no-trade volatility regimes
        if atr_pct > self.max_atr_pct:
            return RobustSignal("hold", 0.0, f"atr too high ({atr_pct:.4f})", atr=atr, regime="high_vol")
        if atr_pct < self.min_atr_pct:
            return RobustSignal("hold", 0.0, f"atr too low ({atr_pct:.4f})", atr=atr, regime="low_vol")
        if trend_strength < self.min_trend_strength:
            return RobustSignal("hold", 0.0, f"trend too weak ({trend_strength:.4f})", atr=atr, regime="range")

        bull = price > ema_anchor and ema_fast > ema_slow > ema_anchor
        bear = price < ema_anchor and ema_fast < ema_slow < ema_anchor
        pullback_distance = abs(price - ema_fast)
        good_pullback = pullback_distance <= (atr * self.pullback_atr_multiple)

        if bull and good_pullback and rsi >= self.long_rsi_floor:
            confidence = min(0.85, 0.45 + (trend_strength * 100) + max(0.0, (rsi - 50.0) / 100.0))
            return RobustSignal(
                action="buy",
                confidence=confidence,
                reason=f"bull trend pullback (rsi={rsi:.1f}, atr_pct={atr_pct:.4f})",
                stop_loss=price - (atr * self.stop_atr_multiple),
                take_profit=price + (atr * self.take_profit_atr_multiple),
                atr=atr,
                regime="bull_trend",
                risk_multiplier=1.0,
                quality_score=confidence,
                max_hold_bars=self.max_hold_bars_default,
            )

        if bear and good_pullback and rsi <= self.short_rsi_ceiling:
            confidence = min(0.85, 0.45 + (trend_strength * 100) + max(0.0, (50.0 - rsi) / 100.0))
            return RobustSignal(
                action="sell",
                confidence=confidence,
                reason=f"bear trend pullback (rsi={rsi:.1f}, atr_pct={atr_pct:.4f})",
                stop_loss=price + (atr * self.stop_atr_multiple),
                take_profit=price - (atr * self.take_profit_atr_multiple),
                atr=atr,
                regime="bear_trend",
                risk_multiplier=1.0,
                quality_score=confidence,
                max_hold_bars=self.max_hold_bars_default,
            )

        regime = "bull_trend" if bull else "bear_trend" if bear else "range"
        return RobustSignal("hold", 0.0, "no qualified pullback setup", atr=atr, regime=regime)

    def _ema(self, prices: np.ndarray, period: int) -> float:
        if len(prices) < period:
            return float(np.mean(prices))
        alpha = 2.0 / (period + 1)
        ema = float(np.mean(prices[:period]))
        for price in prices[period:]:
            ema = alpha * float(price) + (1.0 - alpha) * ema
        return ema

    def _rsi(self, prices: np.ndarray, period: int) -> float:
        if len(prices) < period + 1:
            return 50.0
        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        avg_gain = float(np.mean(gains[:period]))
        avg_loss = float(np.mean(losses[:period]))
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def _atr(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> float:
        if len(closes) < period + 1:
            return 0.0
        trs = []
        for i in range(1, len(closes)):
            tr = max(
                float(highs[i] - lows[i]),
                abs(float(highs[i] - closes[i - 1])),
                abs(float(lows[i] - closes[i - 1])),
            )
            trs.append(tr)
        return float(np.mean(trs[-period:]))
