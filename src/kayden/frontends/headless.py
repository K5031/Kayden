"""A frontend for another program instead of a person: `kayden --headless`.

The program drives Kayden with JSON-RPC 2.0, one message per line, on Kayden's stdin and
stdout. Each request is a RuntimeAPI command of the same name (REQUESTS); every runtime
event comes out as an `event` notification with its `seq`. API values are JSON objects
with their class name under "type"; enums are their values. Permission prompts and the
agent's questions (HelpNeeded) go out as events and are answered with resolve_permission
and send. Closing stdin quits. Nothing else is written to stdout: the process's own stdout
is pointed at stderr, and its stdin at /dev/null, so no library or subprocess can write to
or read from the channel.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Mapping
from enum import Enum
from pathlib import Path
from typing import IO, Any

from kayden.api import (
    Decision,
    Duty,
    DutyKind,
    Frontend,
    FrontendInfo,
    InstancePaths,
    PlanItem,
    PlanStatus,
    RuntimeAPI,
    RuntimeStateError,
    StartOptions,
)

logger = logging.getLogger(__name__)

PARSE_ERROR, INVALID_REQUEST, NO_METHOD, INVALID_PARAMS = -32700, -32600, -32601, -32602
REFUSED = -32000  # the runtime refused: RuntimeStateError, ValueError, KeyError, ...
_LINE_LIMIT = 16 * 1024 * 1024


def to_json(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {"type": type(value).__name__,
                **{f.name: to_json(getattr(value, f.name)) for f in dataclasses.fields(value)}}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(k): to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _duty(raw: Mapping[str, Any] | None) -> Duty | None:
    if raw is None:
        return None
    return Duty(_arg(dict(raw), "instruction"), DutyKind(raw.get("kind", "duty")),
                raw.get("min_interval_s", 60.0), raw.get("max_interval_s", 3600.0))


def _plan(raw: list[Mapping[str, Any]]) -> list[PlanItem]:
    return [PlanItem(_arg(dict(i), "text"), PlanStatus(i.get("status", "todo")), i.get("due"),
                     i.get("note", "")) for i in raw]


def _arg(params: dict[str, Any], name: str) -> Any:
    """A required parameter; TypeError (invalid params) when it is missing."""
    if name not in params:
        raise TypeError(f"missing parameter {name!r}")
    return params.pop(name)


type Handler = Callable[[RuntimeAPI, dict[str, Any]], Awaitable[Any]]

REQUESTS: dict[str, Handler] = {
    "start": lambda r, p: r.start(StartOptions(**p)),
    "send": lambda r, p: r.send(_arg(p, "text"), **p),
    "activate": lambda r, p: r.activate(**p),
    "stop": lambda r, p: r.stop(**p),
    "cancel_operation": lambda r, p: r.cancel_operation(_arg(p, "call_id"), **p),
    "select_model": lambda r, p: r.select_model(_arg(p, "model_id"), **p),
    "set_duty": lambda r, p: r.set_duty(_duty(_arg(p, "duty")), **p),
    "set_plan": lambda r, p: r.set_plan(_plan(_arg(p, "plan")), **p),
    "resolve_permission": lambda r, p: r.resolve_permission(
        _arg(p, "request_id"), Decision(_arg(p, "decision")), **p),
    "snapshot": lambda r, p: _now(r.snapshot(**p)),
    "history": lambda r, p: r.history(_arg(p, "conversation_id"), **p),
    "list_conversations": lambda r, p: r.list_conversations(**p),
    "list_models": lambda r, p: r.list_models(**p),
    "settings": lambda r, p: r.settings(**p),
}


async def _now(value: Any) -> Any:
    return value


def factory(options: dict[str, Any], paths: InstancePaths | None) -> HeadlessFrontend:
    """Options: start, conversation_id, model (as for every frontend); duty, with goal true
    for a goal: start a new conversation on that duty."""
    start = None
    if options.get("start"):
        start = StartOptions(conversation_id=options.get("conversation_id"),
                             model=options.get("model"))
    kind = DutyKind.GOAL if options.get("goal") else DutyKind.DUTY
    return HeadlessFrontend(start=start, duty=options.get("duty"), kind=kind)


factory.display_name = "Headless (for programs)"  # type: ignore[attr-defined]
factory.kind = "terminal"  # type: ignore[attr-defined]


class HeadlessFrontend(Frontend):
    def __init__(self, *, start: StartOptions | None = None, duty: str | None = None,
                 kind: DutyKind = DutyKind.DUTY, stdin: IO[bytes] | None = None,
                 stdout: IO[bytes] | None = None) -> None:
        self.start, self.duty, self.kind = start, duty, kind
        self._stdin, self._stdout = stdin, stdout
        self._write_lock = asyncio.Lock()
        self._writer: asyncio.StreamWriter | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def info(self) -> FrontendInfo:
        return FrontendInfo("headless", interactive_permissions=True)

    async def run(self, runtime: RuntimeAPI) -> None:
        stdin, stdout = self._stdin, self._stdout
        if stdin is None or stdout is None:
            stdin, stdout = _claim_stdio()
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=_LINE_LIMIT)
        stdin_transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), stdin)
        transport, protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, stdout)
        self._writer = asyncio.StreamWriter(transport, protocol, None, loop)
        events = asyncio.create_task(self._forward(runtime))
        try:
            if await self._open(runtime):
                await self._serve(runtime, reader)
        finally:
            events.cancel()
            for task in (events, *self._tasks):
                task.cancel()
            await asyncio.gather(events, *self._tasks, return_exceptions=True)
            stdin_transport.close()
            transport.close()

    async def _open(self, runtime: RuntimeAPI) -> bool:
        """Start as the launch options say; False when the unit cannot run (said on
        stderr), which quits."""
        if self.start is None and self.duty is None:
            return True
        try:
            await runtime.start(self.start or StartOptions())
            if self.duty is not None:
                await runtime.set_duty(Duty(self.duty, self.kind))
        except Exception as e:  # noqa: BLE001 - reported to the spawner, then quit
            print(f"kayden: {e}", file=sys.stderr)
            return False
        return True

    async def _forward(self, runtime: RuntimeAPI) -> None:
        async for envelope in runtime.events():
            await self._send({"jsonrpc": "2.0", "method": "event",
                              "params": {"seq": envelope.seq,
                                         "event": to_json(envelope.event)}})

    async def _serve(self, runtime: RuntimeAPI, reader: asyncio.StreamReader) -> None:
        while line := await reader.readline():
            if not line.strip():
                continue
            task = asyncio.create_task(self._handle(runtime, line))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        if self._tasks:  # answer what was asked before stdin closed
            await asyncio.wait(self._tasks)

    async def _handle(self, runtime: RuntimeAPI, line: bytes) -> None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError as e:
            await self._error(None, PARSE_ERROR, f"invalid JSON: {e}")
            return
        if not isinstance(message, dict) or not isinstance(message.get("method"), str):
            await self._error(None, INVALID_REQUEST, "expected a JSON-RPC request object")
            return
        request_id, method = message.get("id"), message["method"]
        params = message.get("params") or {}
        handler = REQUESTS.get(method)
        if handler is None:
            await self._error(request_id, NO_METHOD, f"unknown method {method!r}")
            return
        if not isinstance(params, dict):
            await self._error(request_id, INVALID_PARAMS, "params must be an object")
            return
        try:
            result = await handler(runtime, dict(params))
        except (TypeError, AttributeError) as e:
            await self._error(request_id, INVALID_PARAMS, str(e))
            return
        except (RuntimeStateError, ValueError, KeyError, NotImplementedError) as e:
            message_text = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
            await self._error(request_id, REFUSED, str(message_text) or type(e).__name__,
                              type(e).__name__)
            return
        except Exception as e:  # every request gets an answer
            logger.exception("headless request %s failed", method)
            await self._error(request_id, REFUSED, f"{type(e).__name__}: {e}",
                              type(e).__name__)
            return
        if request_id is not None:
            await self._send({"jsonrpc": "2.0", "id": request_id, "result": to_json(result)})

    async def _error(self, request_id: Any, code: int, message: str,
                     kind: str | None = None) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if kind is not None:
            error["data"] = {"type": kind}
        await self._send({"jsonrpc": "2.0", "id": request_id, "error": error})

    async def _send(self, message: dict[str, Any]) -> None:
        assert self._writer is not None
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode()
        async with self._write_lock:
            with contextlib.suppress(ConnectionError):
                self._writer.write(data)
                await self._writer.drain()


def _claim_stdio() -> tuple[IO[bytes], IO[bytes]]:
    """The process's stdin and stdout for the channel alone: fd 0 then reads /dev/null and
    fd 1 writes to stderr, so nothing else in the process can touch the channel."""
    channel_in, channel_out = os.dup(0), os.dup(1)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(channel_in, "rb", buffering=0), os.fdopen(channel_out, "wb", buffering=0)
