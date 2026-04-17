#!/usr/bin/env python3
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, DefaultDict, Dict, List


@dataclass
class Event:
    type: str
    payload: Dict[str, Any] = field(default_factory=dict)


Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._handlers: DefaultDict[str, List[Handler]] = defaultdict(list)
        self._running = False

    def subscribe(self, event_type: str, handler: Handler) -> None:
        self._handlers[event_type].append(handler)

    async def publish(self, event_type: str, payload: Dict[str, Any]) -> None:
        await self._queue.put(Event(type=event_type, payload=payload))

    async def run(self) -> None:
        self._running = True
        while self._running:
            event = await self._queue.get()
            handlers = list(self._handlers.get(event.type, []))
            for handler in handlers:
                await handler(event)
            self._queue.task_done()

    def stop(self) -> None:
        self._running = False
