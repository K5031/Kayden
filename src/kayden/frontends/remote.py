"""The other end of the headless channel: a JSON-RPC client for a Kayden child process
(Channel), and RemoteRuntime, a RuntimeAPI over it, so a frontend shows and drives a child
exactly as it does its own runtime."""

from __future__ import annotations

import asyncio
import itertools
import json
from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace
from typing import Any

from kayden.api import (
    ArchiveHit,
    ConversationId,
    ConversationInfo,
    ConversationUpdated,
    Decision,
    Duty,
    DutyScheduled,
    EventEnvelope,
    HelpNeeded,
    MemoryNote,
    MemoryView,
    Message,
    MessageCommitted,
    MessageQueued,
    ModelInfo,
    ModelSelected,
    OperationInfo,
    PermissionRequested,
    PermissionResolved,
    PlanItem,
    ProposalMade,
    ProposalResolved,
    RuntimeSnapshot,
    RuntimeStateError,
    Settings,
    SettingsUpdate,
    StartOptions,
    StateChanged,
    ToolBackgrounded,
    ToolFinished,
    ToolProgress,
    ToolSpec,
    ToolStarted,
    UsageUpdated,
)
from kayden.frontends.wire import from_json, to_json

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
    the parent's window restarts the parent."""

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

    async def _call(self, method: str, hint: Any = Any, **params: Any) -> Any:
        return from_json(await self.channel.request(method, **params), hint)

    async def list_models(self) -> Sequence[ModelInfo]:
        return await self._call("list_models", Sequence[ModelInfo])

    async def list_tools(self) -> Sequence[ToolSpec]:
        return await self._call("list_tools", Sequence[ToolSpec])

    async def list_conversations(self, *, include_archived: bool = False
                                 ) -> Sequence[ConversationInfo]:
        return await self._call("list_conversations", Sequence[ConversationInfo],
                                include_archived=include_archived)

    async def history(self, conversation_id: ConversationId, *, before: str | None = None,
                      limit: int | None = None) -> Sequence[Message]:
        return await self._call("history", Sequence[Message], conversation_id=conversation_id,
                                before=before, limit=limit)

    async def search_archive(self, query: str, *, conversation_id: ConversationId | None = None,
                             limit: int = 10) -> Sequence[ArchiveHit]:
        return await self._call("search_archive", Sequence[ArchiveHit], query=query,
                                conversation_id=conversation_id, limit=limit)

    async def inspect_memory(self, conversation_id: ConversationId) -> MemoryView | None:
        return await self._call("inspect_memory", MemoryView | None,
                                conversation_id=conversation_id)

    async def notes(self) -> Sequence[MemoryNote]:
        return await self._call("notes", Sequence[MemoryNote])

    # ── Control ───────────────────────────────────────────────────────────

    async def start(self, options: StartOptions) -> ConversationInfo:
        return await self._call("start", ConversationInfo,
                                **{k: v for k, v in to_json(options).items() if k != "type"})

    async def select_model(self, model_id: str) -> None:
        await self._call("select_model", model_id=model_id)

    async def settings(self) -> Settings:
        return await self._call("settings", Settings)

    async def update_settings(self, update: SettingsUpdate) -> Settings:
        return await self._call("update_settings", Settings, update=update)

    async def restart(self) -> None:
        raise RuntimeStateError("a sub-session restarts with its parent")

    async def send(self, text: str) -> None:
        await self._call("send", text=text)

    async def activate(self) -> None:
        await self._call("activate")

    async def stop(self) -> None:
        await self._call("stop")

    async def cancel_operation(self, call_id: str) -> None:
        await self._call("cancel_operation", call_id=call_id)

    async def rename_conversation(self, conversation_id: ConversationId, title: str) -> None:
        await self._call("rename_conversation", conversation_id=conversation_id, title=title)

    async def archive_conversation(self, conversation_id: ConversationId, archived: bool
                                   ) -> None:
        await self._call("archive_conversation", conversation_id=conversation_id,
                         archived=archived)

    async def delete_conversation(self, conversation_id: ConversationId) -> None:
        await self._call("delete_conversation", conversation_id=conversation_id)

    async def save_note(self, title: str, description: str, text: str, *,
                        note_id: str | None = None) -> MemoryNote:
        return await self._call("save_note", MemoryNote, title=title, description=description,
                                text=text, note_id=note_id)

    async def delete_note(self, note_id: str) -> None:
        await self._call("delete_note", note_id=note_id)

    async def set_duty(self, duty: Duty | None) -> None:
        plain = None if duty is None else {k: v for k, v in to_json(duty).items()
                                           if k != "type"}
        await self._call("set_duty", duty=plain)

    async def set_plan(self, plan: Sequence[PlanItem]) -> None:
        await self._call("set_plan", plan=[{k: v for k, v in to_json(i).items() if k != "type"}
                                           for i in plan])

    async def resolve_permission(self, request_id: str, decision: Decision) -> None:
        await self._call("resolve_permission", request_id=request_id, decision=decision)

    async def resolve_proposal(self, proposal_id: str, accept: bool) -> None:
        await self._call("resolve_proposal", proposal_id=proposal_id, accept=accept)


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
