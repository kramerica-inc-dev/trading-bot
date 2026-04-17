#!/usr/bin/env python3
"""Institutional-style risk manager for live trading."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Optional


@dataclass
class RiskDecision:
    allowed: bool
    reason: str
    risk_percent: float


class InstitutionalRiskManager:
    def __init__(self, config: Dict, logger: Optional[Callable] = None):
        self.config = config or {}
        self.logger = logger
        self.base_risk_pct = float(self.config.get("base_risk_pct", 0.5))
        self.max_positions = int(self.config.get("max_positions", 2))
        self.max_pending_orders = int(self.config.get("max_pending_orders", 4))
        self.min_signal_quality = float(self.config.get("min_signal_quality", 0.55))
        self.max_portfolio_heat_pct = float(self.config.get("max_portfolio_heat_pct", 1.5))
        self.max_drawdown_pct = float(self.config.get("max_drawdown_pct", 5.0))
        self.trailing_drawdown_pct = float(self.config.get("trailing_drawdown_pct", 3.0))
        self.min_risk_multiplier = float(self.config.get("min_risk_multiplier", 0.5))
        self.max_risk_multiplier = float(self.config.get("max_risk_multiplier", 1.25))

    def _log(self, level: str, msg: str):
        if self.logger:
            self.logger(level, msg)

    def evaluate_trade(self, ctx: Dict) -> Dict:
        quality = float(ctx.get("signal_quality", 0.0) or 0.0)
        risk_multiplier = float(ctx.get("risk_multiplier", 1.0) or 1.0)
        open_positions = int(ctx.get("open_positions", 0) or 0)
        pending_orders = int(ctx.get("pending_orders", 0) or 0)

        if open_positions >= self.max_positions:
            return {"allowed": False, "reason": "max_positions_reached", "risk_percent": 0.0}

        if pending_orders >= self.max_pending_orders:
            return {"allowed": False, "reason": "max_pending_orders_reached", "risk_percent": 0.0}

        if quality < self.min_signal_quality:
            return {"allowed": False, "reason": "signal_quality_below_threshold", "risk_percent": 0.0}

        scaled_multiplier = max(self.min_risk_multiplier, min(self.max_risk_multiplier, risk_multiplier))
        risk_percent = self.base_risk_pct * scaled_multiplier

        return {"allowed": True, "reason": "ok", "risk_percent": round(risk_percent, 4)}
