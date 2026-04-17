#!/usr/bin/env python3
"""Async execution adapter that wraps the existing synchronous exchange adapter."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional


class AsyncExecutionAdapter:
    def __init__(self, sync_adapter):
        self.sync_adapter = sync_adapter

    async def get_account_balance(self) -> Dict[str, Any]:
        return await asyncio.to_thread(self.sync_adapter.get_account_balance)

    async def get_ticker(self, inst_id: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self.sync_adapter.get_ticker, inst_id)

    async def get_candles(self, inst_id: str, bar: str, limit: int = 300):
        return await asyncio.to_thread(
            self.sync_adapter.get_candles,
            inst_id,
            bar,
            limit,
        )

    async def place_order(self, **kwargs) -> Dict[str, Any]:
        return await asyncio.to_thread(self.sync_adapter.place_order, **kwargs)

    async def cancel_order(self, inst_id: str, order_id: Optional[str] = None,
                           client_order_id: Optional[str] = None) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self.sync_adapter.cancel_order,
            inst_id,
            order_id,
            client_order_id,
        )

    async def get_positions(self, inst_id: Optional[str] = None) -> Dict[str, Any]:
        if inst_id is None:
            return await asyncio.to_thread(self.sync_adapter.get_positions)
        return await asyncio.to_thread(self.sync_adapter.get_positions, inst_id)
