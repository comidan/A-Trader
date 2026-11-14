"""In-process event bus. Slow subscribers lose events (counted and logged), never block publishers."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class Event:
    topic: str
    payload: dict[str, Any]


@dataclass
class Subscription:
    prefix: str
    queue: asyncio.Queue[Event]
    dropped: int = 0


@dataclass
class EventBus:
    _subs: list[Subscription] = field(default_factory=list)

    def subscribe(self, prefix: str = "", maxsize: int = 1000) -> Subscription:
        sub = Subscription(prefix, asyncio.Queue(maxsize))
        self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        event = Event(topic, payload)
        for sub in self._subs:
            if not topic.startswith(sub.prefix):
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                sub.dropped += 1
                log.warning("bus_event_dropped", extra={"topic": topic, "dropped": sub.dropped})
