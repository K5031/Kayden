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
import os
import queue
import time
from collections import deque
from pathlib import Path
from typing import Any

from kayden.api import (
    BLOCK_TYPES,
    EventEnvelope,
    MessageCommitted,
    OpaqueBlock,
    ReasoningDelta,
    RuntimeEvent,
    ShuttingDown,
    TextDelta,
    ToolCallDelta,
    ToolProgress,
)

# High-volume events stay out of the decision log.
_UNLOGGED = (TextDelta, ReasoningDelta, ToolCallDelta, ToolProgress)
# First line of a log. 2: records carry no message text, tool arguments, tool output,
# system prompt or conversation titles.
LOG_FORMAT = 2
_HEADER = json.dumps({"log_format": LOG_FORMAT})
_BLOCK_TAGS = {kind: tag for tag, kind in BLOCK_TYPES.items()}


def _content_free(event: str, data: dict[str, Any]) -> dict[str, Any]:
    """An event's logged data without content. Also applied to the records of a log
    written before LOG_FORMAT 2, so it leaves data it does not recognize as it is."""
    match event, data:
        case "MessageCommitted", {"message": {"id": message_id, "role": role, **message}}:
            blocks = message.get("blocks")
            tagged = bool(blocks) and all(isinstance(b, str) for b in blocks)
            return {"message_id": message_id, "role": role,
                    **({"blocks": blocks} if tagged else {}), "stop": data.get("stop")}
        case "MessageQueued", {"message_id": message_id, "text": str(text)}:
            return {"message_id": message_id, "chars": len(text)}
        case "ToolStarted", {"call": {"id": call_id, "name": name}}:
            return {"call_id": call_id, "name": name}
        case "ToolFinished", {"result": {"call_id": call_id, "status": status}}:
            return {"call_id": call_id, "status": status, "duration_s": data.get("duration_s")}
        case "PermissionRequested", {"request": {"id": request_id, "call": {
                "id": call_id, "name": name}, **request}}:
            return {"request_id": request_id, "call_id": call_id, "name": name,
                    "read_only": request.get("read_only")}
        case "SettingsChanged", {"settings": {"system_prompt": str(prompt), **settings}}:
            return {"settings": {**settings, "system_prompt_chars": len(prompt)}}
        case "ConversationUpdated", {"info": dict(info)}:
            return {"info": {k: v for k, v in info.items() if k not in ("title", "preview")}}
    return data


def strip_content(path: Path, *, create: bool = False) -> None:
    """Bring a log to LOG_FORMAT: rewrite one written before it without content, once.
    `create` starts a missing log. Blocking; call it off the event loop."""
    try:
        with path.open(encoding="utf-8") as f:
            if f.readline().strip() == _HEADER:
                return
            f.seek(0)
            lines = f.readlines()
    except FileNotFoundError:
        if not create:
            return
        lines = []
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as out:
        out.write(_HEADER + "\n")
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue  # unreadable, so not known to be free of content
            if not isinstance(record, dict):
                continue
            if isinstance(record.get("event"), str) and isinstance(record.get("data"), dict):
                record["data"] = _content_free(record["event"], record["data"])
            out.write(json.dumps(record) + "\n")
    os.replace(tmp, path)


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
        """`path` is at LOG_FORMAT (strip_content)."""
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
        event = envelope.event
        if isinstance(event, _UNLOGGED):
            return
        data = dataclasses.asdict(event)
        if isinstance(event, MessageCommitted):
            data["message"]["blocks"] = [
                b.type if isinstance(b, OpaqueBlock) else _BLOCK_TAGS[type(b)]
                for b in event.message.blocks]
        name = type(event).__name__
        # Through JSON first, so enums are plain values for _content_free.
        data = json.loads(json.dumps(data, default=str))
        self.write({"seq": envelope.seq, "event": name, "data": _content_free(name, data)})

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
