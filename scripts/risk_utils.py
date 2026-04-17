#!/usr/bin/env python3
"""Risk-based position sizing helpers."""

from __future__ import annotations

from dataclasses import dataclass
from math import floor


@dataclass
class PositionSizingResult:
    contracts: float
    risk_amount: float
    stop_distance: float
    effective_stop_distance: float
    risk_per_contract: float
    estimated_loss: float
    notional_value: float
    contracts_raw: float = 0.0
    capped_by_notional: bool = False
    reason: str = ""



def calculate_risk_position_size(
    *,
    balance: float,
    entry_price: float,
    stop_loss: float,
    risk_percent: float,
    contract_size: float,
    contract_step: float = 0.1,
    min_contracts: float = 0.1,
    leverage: float = 1.0,
    max_position_notional_pct: float = 100.0,
    slippage_buffer_pct: float = 0.0,
) -> PositionSizingResult:
    if balance <= 0:
        return PositionSizingResult(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, reason="Balance is zero")

    if entry_price <= 0:
        return PositionSizingResult(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, reason="Entry price is zero or negative")

    stop_distance = abs(entry_price - stop_loss)
    if stop_distance <= 0:
        return PositionSizingResult(0.0, 0.0, stop_distance, stop_distance, 0.0, 0.0, 0.0, reason="Stop distance is zero")

    effective_stop_distance = stop_distance * (1 + max(slippage_buffer_pct, 0.0) / 100.0)
    risk_amount = balance * (risk_percent / 100.0)
    risk_per_contract = effective_stop_distance * contract_size

    if risk_per_contract <= 0:
        return PositionSizingResult(0.0, risk_amount, stop_distance, effective_stop_distance, 0.0, 0.0, 0.0, reason="Invalid risk per contract")

    raw_contracts = risk_amount / risk_per_contract
    contracts = _floor_to_step(raw_contracts, contract_step)

    if contracts < min_contracts:
        return PositionSizingResult(
            0.0,
            risk_amount,
            stop_distance,
            effective_stop_distance,
            risk_per_contract,
            0.0,
            0.0,
            contracts_raw=round(raw_contracts, 8),
            reason="Risk budget too small for minimum contract size",
        )

    notional_value = contracts * contract_size * entry_price
    max_notional_value = balance * max(leverage, 1.0) * (max_position_notional_pct / 100.0)
    capped_by_notional = False

    if max_notional_value > 0 and notional_value > max_notional_value:
        max_contracts_by_notional = _floor_to_step(
            max_notional_value / (contract_size * entry_price),
            contract_step,
        )
        capped_by_notional = True
        contracts = min(contracts, max_contracts_by_notional)
        notional_value = contracts * contract_size * entry_price

    if contracts < min_contracts:
        return PositionSizingResult(
            0.0,
            risk_amount,
            stop_distance,
            effective_stop_distance,
            risk_per_contract,
            0.0,
            0.0,
            contracts_raw=round(raw_contracts, 8),
            capped_by_notional=capped_by_notional,
            reason="Maximum notional cap leaves no valid order size",
        )

    estimated_loss = contracts * risk_per_contract

    return PositionSizingResult(
        contracts=round(contracts, 8),
        risk_amount=round(risk_amount, 8),
        stop_distance=round(stop_distance, 8),
        effective_stop_distance=round(effective_stop_distance, 8),
        risk_per_contract=round(risk_per_contract, 8),
        estimated_loss=round(estimated_loss, 8),
        notional_value=round(notional_value, 8),
        contracts_raw=round(raw_contracts, 8),
        capped_by_notional=capped_by_notional,
        reason="ok",
    )



def _floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        raise ValueError("step must be > 0")
    return floor(value / step) * step
