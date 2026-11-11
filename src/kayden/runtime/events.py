"""Event fan-out to subscribers and the JSONL event log.

The log records what the runtime did, never what was said or run: messages, tool calls and
results appear by id, kind and size only, so deleting a conversation leaves nothing of it
here."""

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
    BLOCK_TYPES,
    ConversationUpdated,
    EventEnvelope,
    MemoriesChanged,
    MessageCommitted,
    MessageQueued,
    OpaqueBlock,
    PermissionRequested,
    ReasoningDelta,
    RuntimeEvent,
    SettingsChanged,
    ShuttingDown,
    TextDelta,
    ToolCallDelta,
    ToolFinished,
    ToolProgress,
    ToolStarted,
)

# High-volume events stay out of the decision log.
_UNLOGGED = (TextDelta, ReasoningDelta, ToolCallDelta, ToolProgress)
_BLOCK_TAGS = {kind: tag for tag, kind in BLOCK_TYPES.items()}


def _logged(event: RuntimeEvent) -> dict[str, Any]:
    """An event's record: what happened, without what was said or run."""
    match event:
        case MessageCommitted(message=message, stop=stop):
            blocks = [b.type if isinstance(b, OpaqueBlock) else _BLOCK_TAGS[type(b)]
                      for b in message.blocks]
            return {"message_id": message.id, "role": message.role, "blocks": blocks,
                    "stop": stop}
        case MessageQueued(message_id=message_id, text=text):
            return {"message_id": message_id, "chars": len(text)}
        case ToolStarted(call=call):
            return {"call_id": call.id, "name": call.name}
        case ToolFinished(result=result, duration_s=duration_s):
            return {"call_id": result.call_id, "status": result.status,
                    "duration_s": duration_s}
        case PermissionRequested(request=request):
            return {"request_id": request.id, "call_id": request.call.id,
                    "name": request.call.name, "read_only": request.read_only,
                    "confined": request.confined}
    data = dataclasses.asdict(event)
    match event:
        case SettingsChanged():
            data["settings"]["system_prompt_chars"] = len(data["settings"].pop("system_prompt"))
        case MemoriesChanged():
            data["saved"] = len(data["saved"])  # how many notes, not their titles
        case ConversationUpdated():
            del data["info"]["title"], data["info"]["preview"]
    return data


class EventLog:
    """JSONL log written off the event loop through a logging QueueListener. Records are
    buffered until open() names the file, once the first start() has locked the data
    directory."""

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
            self.write({"seq": envelope.seq, "event": type(envelope.event).__name__,
                        "data": _logged(envelope.event)})

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
