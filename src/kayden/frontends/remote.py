"""The other end of the headless channel: a JSON-RPC client for a Kayden child process
(Channel), and RemoteRuntime, a RuntimeAPI over it, so a frontend shows and drives a child
exactly as it does its own runtime."""

from __future__ import annotations

import asyncio
import inspect
import itertools
import json
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from typing import Any

from kayden.api import (
    ConversationUpdated,
    DutyScheduled,
    EventEnvelope,
    HelpNeeded,
    MessageCommitted,
    MessageQueued,
    ModelSelected,
    OperationInfo,
    PermissionRequested,
    PermissionResolved,
    ProposalMade,
    ProposalResolved,
    RuntimeAPI,
    RuntimeSnapshot,
    RuntimeStateError,
    StateChanged,
    ToolBackgrounded,
    ToolFinished,
    ToolProgress,
    ToolStarted,
    UsageUpdated,
)
from kayden.frontends.wire import COMMANDS, from_json, result_hint, to_json

STDERR_LINES = 20
_ERRORS: dict[str, type[Exception]] = {
    "RuntimeStateError": RuntimeStateError, "ValueError": ValueError, "KeyError": KeyError,
    "NotImplementedError": NotImplementedError}


class ChannelClosed(RuntimeStateError):
    """The child process ended."""


class Channel:
    """JSON-RPC over a child's stdin and stdout. Events go to every listener, already
    decoded; requests return the decoded result or raise the runtime's exception."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.listeners: list[Callable[[EventEnvelope], None]] = []
        self.stderr: deque[str] = deque(maxlen=STDERR_LINES)
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self.ended = asyncio.Event()
        self._reader = asyncio.create_task(self._read())
        self._errors = asyncio.create_task(self._read_stderr())

    async def request(self, method: str, **params: Any) -> Any:
        if self.ended.is_set():
            raise ChannelClosed("the sub-session has ended")
        request_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        assert self.process.stdin is not None
        line = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method,
                           "params": to_json(params)})
        self.process.stdin.write(line.encode() + b"\n")
        await self.process.stdin.drain()
        return await future

    async def _read(self) -> None:
        assert self.process.stdout is not None
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if message.get("method") == "event":
                    params = message["params"]
                    envelope = EventEnvelope(params["seq"], from_json(params["event"]))
                    for listener in list(self.listeners):
                        listener(envelope)
                elif ((future := self._pending.pop(message.get("id"), None)) is not None
                      and not future.done()):  # done: its caller was cancelled
                    if "error" in message:
                        error = message["error"]
                        kind = _ERRORS.get((error.get("data") or {}).get("type"), RuntimeError)
                        future.set_exception(kind(error["message"]))
                    else:
                        future.set_result(message.get("result"))
        finally:
            self.ended.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ChannelClosed("the sub-session has ended"))
            for listener in list(self.listeners):
                listener(None)  # type: ignore[arg-type]

    async def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        while line := await self.process.stderr.readline():
            self.stderr.append(line.decode(errors="replace").rstrip())

    async def close(self, grace_s: float) -> int:
        """Close its stdin (it quits as on closing the window); kill it after the grace."""
        if self.process.stdin is not None and not self.process.stdin.is_closing():
            self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), grace_s)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()
        await asyncio.gather(self._reader, self._errors, return_exceptions=True)
        assert self.process.returncode is not None
        return self.process.returncode


class RemoteRuntime:
    """RuntimeAPI for a child. attach() first: it subscribes, then reads the snapshot,
    which events keep current, since snapshot() must answer at once. restart() is refused:
    the parent's window restarts the parent. Every other command is made from RuntimeAPI
    itself (_command): a request of the same name."""

    def __init__(self, channel: Channel) -> None:
        self.channel = channel
        self._snapshot: RuntimeSnapshot | None = None
        self._queues: list[asyncio.Queue[EventEnvelope | None]] = []
        channel.listeners.append(self._event)

    async def attach(self) -> RemoteRuntime:
        self._snapshot = from_json(await self.channel.request("snapshot"), RuntimeSnapshot)
        return self

    def detach(self) -> None:
        if self._event in self.channel.listeners:
            self.channel.listeners.remove(self._event)
        for queue in self._queues:
            queue.put_nowait(None)

    def _event(self, envelope: EventEnvelope | None) -> None:
        if envelope is not None and self._snapshot is not None:
            self._snapshot = _applied(self._snapshot, envelope)
        for queue in self._queues:
            queue.put_nowait(envelope)

    # ── Observation ───────────────────────────────────────────────────────

    def events(self) -> AsyncIterator[EventEnvelope]:
        queue: asyncio.Queue[EventEnvelope | None] = asyncio.Queue()
        self._queues.append(queue)
        if self.channel.ended.is_set():
            queue.put_nowait(None)

        async def stream() -> AsyncIterator[EventEnvelope]:
            try:
                while (envelope := await queue.get()) is not None:
                    yield envelope
            finally:
                if queue in self._queues:
                    self._queues.remove(queue)

        return stream()

    def snapshot(self) -> RuntimeSnapshot:
        assert self._snapshot is not None, "attach() first"
        return self._snapshot

    async def restart(self) -> None:
        raise RuntimeStateError("a sub-session restarts with its parent")


def _command(name: str) -> Callable[..., Any]:
    """RemoteRuntime's method for a RuntimeAPI command: the same signature, sent as the
    request of that name, its reply decoded by the command's return type."""
    command = getattr(RuntimeAPI, name)
    signature = inspect.signature(command)
    hint = result_hint(name)

    async def send(self: RemoteRuntime, *args: Any, **kwargs: Any) -> Any:
        bound = signature.bind(self, *args, **kwargs)
        params = {k: v for k, v in bound.arguments.items() if k != "self"}
        return from_json(await self.channel.request(name, **params), hint)

    send.__name__ = send.__qualname__ = name
    send.__signature__ = signature  # type: ignore[attr-defined]
    send.__doc__ = command.__doc__
    return send


for _name in sorted(COMMANDS):
    if _name not in vars(RemoteRuntime):  # snapshot and restart are its own
        setattr(RemoteRuntime, _name, _command(_name))


def _applied(snap: RuntimeSnapshot, envelope: EventEnvelope) -> RuntimeSnapshot:
    """The snapshot after `envelope`: what the events say about the fields a frontend reads
    from snapshot() between full reloads."""
    snap = replace(snap, seq=max(snap.seq, envelope.seq))
    match envelope.event:
        case StateChanged(state=state):
            return replace(snap, state=state)
        case ConversationUpdated(info=info) if snap.conversation and info.id == \
                snap.conversation.id:
            return replace(snap, conversation=info)
        case ModelSelected(model_id=model_id):
            return replace(snap, model=model_id)
        case UsageUpdated(snapshot=usage):
            return replace(snap, usage=usage)
        case DutyScheduled(wake_at=wake_at):
            return replace(snap, duty_wake_at=wake_at)
        case HelpNeeded(question=question):
            return replace(snap, help_needed=question)
        case ToolStarted(call=call):
            return replace(snap, operations=(*snap.operations, OperationInfo(call, 0.0)))
        case ToolProgress(call_id=call_id, text=text):
            return replace(snap, operations=tuple(
                replace(o, last_progress=text) if o.call.id == call_id else o
                for o in snap.operations))
        case ToolBackgrounded(call_id=call_id):
            return replace(snap, operations=tuple(
                replace(o, backgrounded=True) if o.call.id == call_id else o
                for o in snap.operations))
        case ToolFinished(result=result):
            return replace(snap, operations=tuple(
                o for o in snap.operations if o.call.id != result.call_id))
        case PermissionRequested(request=request):
            return replace(snap, pending_permissions=(*snap.pending_permissions, request))
        case PermissionResolved(request_id=request_id):
            return replace(snap, pending_permissions=tuple(
                r for r in snap.pending_permissions if r.id != request_id))
        case ProposalMade(proposal=proposal):
            return replace(snap, proposals=(*snap.proposals, proposal))
        case ProposalResolved(proposal_id=proposal_id):
            return replace(snap, proposals=tuple(
                p for p in snap.proposals if p.id != proposal_id))
        case MessageQueued():
            return replace(snap, queued_messages=(*snap.queued_messages, envelope.event))
        case MessageCommitted(message=message):
            return replace(snap, queued_messages=tuple(
                q for q in snap.queued_messages if q.message_id != message.id))
    return snap
