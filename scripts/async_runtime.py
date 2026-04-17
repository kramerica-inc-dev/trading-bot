#!/usr/bin/env python3
"""Async event-driven runtime that wraps the existing sync adapter.

This is a migration target, not a drop-in replacement for every existing feature.
It moves the bot from polling orchestration to event-driven tasks:
- market data producer
- signal engine
- risk gate
- execution engine
- state projector
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional

from exchange_adapter import create_exchange_adapter
from event_bus import EventBus
from institutional_risk import InstitutionalRiskManager
from robust_strategy import RobustTrendPullback


@dataclass
class RuntimeState:
    last_price: Optional[float] = None
    last_signal_ts: float = 0.0
    position: Optional[Dict] = None
    daily_pnl_pct: float = 0.0
    trailing_drawdown_pct: float = 0.0
    margin_usage_pct: float = 0.0
    portfolio_heat_pct: float = 0.0


class AsyncTradingRuntime:
    def __init__(self, config_path: str = "config.json"):
        self.base_dir = Path(config_path).resolve().parent if Path(config_path).is_absolute() else Path(__file__).parent.parent
        self.config = json.loads((self.base_dir / Path(config_path).name).read_text())
        self.exchange_name = self.config["exchange"]
        self.exchange = create_exchange_adapter(self.exchange_name, self.config[self.exchange_name])
        self.inst_id = self.config["trading_pair"]
        self.timeframe = self.config.get("timeframe", "5m")
        self.bus = EventBus()
        self.state = RuntimeState()
        self.strategy = RobustTrendPullback(self.config.get("strategy", {}))
        self.risk = InstitutionalRiskManager(self.config.get("institutional_risk", {}))
        self.running = False
        self.memory_dir = self.base_dir / "memory"
        self.memory_dir.mkdir(exist_ok=True)
        self.state_file = self.memory_dir / "async-runtime-state.json"

        self.bus.subscribe("market.tick", self.on_market_tick)
        self.bus.subscribe("signal.generated", self.on_signal)
        self.bus.subscribe("order.requested", self.on_order_requested)

    async def fetch_market_snapshot(self) -> Dict:
        ticker = await asyncio.to_thread(self.exchange.get_ticker, self.inst_id)
        candles = await asyncio.to_thread(self.exchange.get_candles, self.inst_id, self.timeframe, 300)
        price = float(ticker["data"][0]["last"])
        return {"price": price, "candles": candles, "ts": time.time()}

    async def market_data_loop(self, interval_seconds: int = 5) -> None:
        while self.running:
            snapshot = await self.fetch_market_snapshot()
            await self.bus.publish("market.tick", snapshot)
            await asyncio.sleep(interval_seconds)

    async def on_market_tick(self, event) -> None:
        payload = event.payload
        self.state.last_price = float(payload["price"])
        signal = self.strategy.analyze(payload["candles"], float(payload["price"]))
        await self.bus.publish("signal.generated", {"signal": signal, "market": payload})

    async def on_signal(self, event) -> None:
        signal = event.payload["signal"]
        market = event.payload["market"]
        if signal.action == "hold":
            return

        balance_resp = await asyncio.to_thread(self.exchange.get_balance, "futures", "USDT")
        balance = self._extract_balance(balance_resp)
        atr_pct = (float(signal.atr or 0.0) / float(market["price"])) if market["price"] else 0.0
        decision = self.risk.evaluate_entry(
            balance=balance,
            daily_pnl_pct=self.state.daily_pnl_pct,
            trailing_drawdown_pct=self.state.trailing_drawdown_pct,
            current_portfolio_heat_pct=self.state.portfolio_heat_pct,
            current_margin_usage_pct=self.state.margin_usage_pct,
            atr_pct=atr_pct,
            quality_score=float(getattr(signal, "quality_score", signal.confidence)),
        )
        if not decision.approved:
            return

        await self.bus.publish("order.requested", {
            "signal": signal,
            "market": market,
            "balance": balance,
            "risk_decision": asdict(decision),
        })

    async def on_order_requested(self, event) -> None:
        signal = event.payload["signal"]
        market = event.payload["market"]
        risk_decision = event.payload["risk_decision"]
        side = "buy" if signal.action == "buy" else "sell"

        # Minimal migration-safe execution stub. Position sizing should reuse your current risk_utils.
        order = await asyncio.to_thread(
            self.exchange.place_order,
            self.inst_id,
            side,
            "market",
            "0.1",
            None,
            self.config.get("risk", {}).get("margin_mode", "isolated"),
            client_order_id=f"async-{int(time.time() * 1000)}",
        )
        self.state.last_signal_ts = time.time()
        self._persist_state()
        _ = order
        _ = market
        _ = risk_decision

    def _extract_balance(self, response: Dict) -> float:
        data = response.get("data") or []
        if not data:
            return 0.0
        row = data[0]
        for key in ("available", "availBal", "balance", "eq"):
            if key in row:
                try:
                    return float(row[key])
                except Exception:
                    continue
        return 0.0

    def _persist_state(self) -> None:
        self.state_file.write_text(json.dumps(asdict(self.state), indent=2))

    async def run(self) -> None:
        self.running = True
        bus_task = asyncio.create_task(self.bus.run())
        market_task = asyncio.create_task(self.market_data_loop())
        await asyncio.gather(bus_task, market_task)


if __name__ == "__main__":
    runtime = AsyncTradingRuntime("config.json")
    asyncio.run(runtime.run())
