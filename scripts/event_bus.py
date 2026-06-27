#!/usr/bin/env python3
"""Event bus — the swappable fan-out seam for the event-driven runner (Phase 1).

This is the "multicast" boundary, made explicit. Today it ships ONE
implementation, `InProcAsyncBus`, backed by per-subscriber `asyncio.Queue`s —
microsecond in-process fan-out, which is four orders of magnitude under
Hyperliquid's ~200ms order floor, so for a single-process bot it is strictly
sufficient. The value of the seam is that the *contract* (one `publish` →
every subscriber receives the event) is exactly the contract a UDP-multicast or
Aeron transport provides, so a future `MulticastBus`/`AeronBus` (the day the bot
goes multi-process / multi-host) drops in behind the same `EventBus` Protocol
without touching producer or consumer code. See the plan's "what we are NOT
doing now, and when it flips" table.

Backpressure: market data is latest-wins. A slow subscriber must NEVER block the
WS callback that feeds the whole system (that coupling is a sibling of the
2026-06-20 hang-while-blocked failure), so `publish` is non-blocking and drops
the OLDEST event from a full subscriber queue, counting the drop. Control events
(fills, intents) use a roomier queue so they are not silently dropped under a
market-data burst.

Self-test (proves fan-out + drop-oldest backpressure):
    python -m scripts.event_bus --selftest
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, List, Optional, Protocol


# --------------------------------------------------------------- event types
@dataclass(slots=True)
class MarketEvent:
    """A top-of-book / mid update for one coin from the WS feed."""
    ts: float
    coin: str
    mid: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    source: str = "bbo"          # "bbo" | "allMids" | "rest-resync"


@dataclass(slots=True)
class FillEvent:
    """A realised fill (from userFills/orderUpdates) — drives event-time
    reconcile so the in-memory book updates without polling positions()."""
    ts: float
    coin: str
    sz: float                    # signed: +long add / -reduce
    px: float
    cloid: Optional[str] = None
    side: str = ""               # "B" | "A"


@dataclass(slots=True)
class OrderIntent:
    """A desired leg emitted by the strategy task and consumed by execution.
    `cloid` is the deterministic idempotency tag (HLAdapter.make_cloid)."""
    ts: float
    coin: str
    is_buy: bool
    usd: float
    cloid: Optional[str] = None
    reason: str = ""


@dataclass(slots=True)
class HealthTick:
    """Liveness snapshot written by the heartbeat task — DECOUPLED from any
    blocking venue I/O (the structural fix for the watchdog-flatten race)."""
    ts: float
    ws_connected: bool
    ws_last_msg_age_sec: float
    maintenance: bool = False
    executor_inflight: int = 0
    extra: dict = field(default_factory=dict)


# ----------------------------------------------------------------- protocol
class EventBus(Protocol):
    """One publish → every subscriber receives it. The contract a future
    UDP-multicast / Aeron transport must also satisfy."""

    def publish(self, event: object) -> None: ...

    def subscribe(self, *, control: bool = False) -> "Subscription": ...


class Subscription:
    """An async-iterable view onto one subscriber's queue. `async for ev in
    sub:` yields events until `close()`. Iteration is per-subscriber, so two
    subscribers each see every published event (true fan-out)."""

    __slots__ = ("_bus", "_q", "_closed")

    def __init__(self, bus: "InProcAsyncBus", q: "asyncio.Queue"):
        self._bus = bus
        self._q = q
        self._closed = False

    def __aiter__(self) -> AsyncIterator[object]:
        return self

    async def __anext__(self) -> object:
        if self._closed:
            raise StopAsyncIteration
        return await self._q.get()

    async def get(self) -> object:
        return await self._q.get()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._bus._remove(self._q)


# ------------------------------------------------------------- in-proc impl
class InProcAsyncBus:
    """In-process fan-out over per-subscriber asyncio.Queues. `publish` is
    synchronous and non-blocking (callable straight from a WS callback); a full
    market-data queue drops its oldest event so a slow consumer can never stall
    the producer."""

    def __init__(self, *, market_maxsize: int = 1024, control_maxsize: int = 4096):
        self._subs: List["asyncio.Queue"] = []
        self._control: set = set()        # ids of roomier, no-drop control queues
        self._market_maxsize = market_maxsize
        self._control_maxsize = control_maxsize
        self.dropped = 0                  # market events shed under backpressure

    def subscribe(self, *, control: bool = False) -> Subscription:
        q: asyncio.Queue = asyncio.Queue(
            maxsize=self._control_maxsize if control else self._market_maxsize)
        self._subs.append(q)
        if control:
            self._control.add(id(q))
        return Subscription(self, q)

    def _remove(self, q: "asyncio.Queue") -> None:
        try:
            self._subs.remove(q)
        except ValueError:
            pass
        self._control.discard(id(q))

    def publish(self, event: object) -> None:
        for q in self._subs:
            if q.full():
                if id(q) in self._control:
                    # control stream: never silently drop — let it back up.
                    pass
                else:
                    try:
                        q.get_nowait()        # drop OLDEST (latest-wins for ticks)
                        self.dropped += 1
                    except asyncio.QueueEmpty:
                        pass
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self.dropped += 1

    def stats(self) -> dict:
        return {"subscribers": len(self._subs), "dropped": self.dropped}


# ------------------------------------------------------------------ selftest
async def _selftest() -> int:
    bus = InProcAsyncBus(market_maxsize=4)
    a = bus.subscribe()
    b = bus.subscribe()

    # 1) Fan-out: both subscribers receive the same published event.
    bus.publish(MarketEvent(ts=time.time(), coin="BTC", mid=60000.0))
    ea = await asyncio.wait_for(a.get(), 1.0)
    eb = await asyncio.wait_for(b.get(), 1.0)
    assert isinstance(ea, MarketEvent) and ea.coin == "BTC", ea
    assert eb.mid == 60000.0 and ea is eb, "both subs must see the SAME event"
    print("  fan-out: both subscribers received the event  ✓")

    # 2) Drop-oldest backpressure: overflow a slow market queue (maxsize=4).
    for i in range(10):
        bus.publish(MarketEvent(ts=time.time(), coin="BTC", mid=float(i)))
    drained = []
    while not a._q.empty():
        drained.append((await a.get()).mid)
    assert len(drained) == 4, f"queue should cap at 4, got {len(drained)}"
    assert drained == [6.0, 7.0, 8.0, 9.0], drained          # newest 4 survive
    assert bus.dropped >= 6, bus.dropped
    print(f"  backpressure: kept newest {drained}, dropped {bus.dropped}  ✓")

    # 3) Control stream is not dropped (roomy, no shedding under the same load).
    c = bus.subscribe(control=True)
    for i in range(20):
        bus.publish(OrderIntent(ts=time.time(), coin="ETH", is_buy=True, usd=10.0))
    got = c._q.qsize()
    assert got == 20, f"control stream must keep all 20, got {got}"
    print(f"  control stream: kept all {got} intents (no drop)  ✓")

    a.close(); b.close(); c.close()
    assert bus.stats()["subscribers"] == 0
    print("  close: subscribers deregistered  ✓")
    print("\nevent_bus self-test passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return asyncio.run(_selftest())
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
