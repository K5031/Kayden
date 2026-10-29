"""Event fan-out to subscribers and the JSONL event log."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import logging.handlers
import queue
import time
from collections import deque
from pathlib import Path
from typing import Any

from kayden.api import (
    EventEnvelope,
    ReasoningDelta,
    RuntimeEvent,
    ShuttingDown,
    TextDelta,
    ToolCallDelta,
    ToolProgress,
)

# High-volume events stay out of the decision log.
_UNLOGGED = (TextDelta, ReasoningDelta, ToolCallDelta, ToolProgress)


class EventLog:
    """JSONL log written off the event loop through a logging QueueListener. Records are
    buffered until open() names the file (the mode, and so the file, is known at start)."""

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
        self._logger = logging.getLogger(f"kayden.events.{id(self)}")
        self._logger.propagate = False
        self._logger.setLevel(logging.INFO)
        self._buffer: list[str] | None = []
        self._listener: logging.handlers.QueueListener | None = None
        self._handler: logging.FileHandler | None = None

    def open(self, path: Path) -> None:
        self._handler = logging.FileHandler(path, encoding="utf-8", delay=True)
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self._listener = logging.handlers.QueueListener(self._queue, self._handler)
        self._listener.start()
        self._logger.addHandler(logging.handlers.QueueHandler(self._queue))
        for line in self._buffer or ():
            self._logger.info(line)
        self._buffer = None

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps({"ts": time.time(), **record}, default=str)
        if self._buffer is not None:
            self._buffer.append(line)
        else:
            self._logger.info(line)

    def event(self, envelope: EventEnvelope) -> None:
        if not isinstance(envelope.event, _UNLOGGED):
            self.write({
                "seq": envelope.seq,
                "event": type(envelope.event).__name__,
                "data": dataclasses.asdict(envelope.event),
            })

    def decision(self, kind: str, /, **fields: Any) -> None:
        self.write({"decision": kind, **fields})

    def close(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        if self._handler is not None:
            self._handler.close()
        self._logger.handlers.clear()


class Subscription:
    """Bounded queue that drops the oldest envelope on overflow."""

    def __init__(self, maxsize: int):
        self._queue: deque[EventEnvelope] = deque(maxlen=maxsize)
        self._ready = asyncio.Event()
        self.closed = False

    def push(self, envelope: EventEnvelope) -> None:
        self._queue.append(envelope)
        self._ready.set()

    def close(self) -> None:
        self.closed = True
        self._ready.set()

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> EventEnvelope:
        while not self._queue:
            if self.closed:
                raise StopAsyncIteration
            self._ready.clear()
            await self._ready.wait()
        return self._queue.popleft()


class EventBus:
    def __init__(self, queue_size: int, log: EventLog):
        self.seq = 0
        self._queue_size = queue_size
        self._subscribers: list[Subscription] = []
        self._log = log
        self._ended = False

    def subscribe(self) -> Subscription:
        sub = Subscription(self._queue_size)
        if self._ended:
            sub.close()
        else:
            self._subscribers.append(sub)
        return sub

    def emit(self, event: RuntimeEvent) -> EventEnvelope:
        self.seq += 1
        envelope = EventEnvelope(self.seq, event)
        for sub in self._subscribers:
            sub.push(envelope)
        self._log.event(envelope)
        if isinstance(event, ShuttingDown):
            self._ended = True
            for sub in self._subscribers:
                sub.close()
            self._subscribers.clear()
        return envelope
