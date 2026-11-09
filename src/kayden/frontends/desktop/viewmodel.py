"""Toolkit-free state of the desktop app: the transcript as items, permission prompts and
the status line, built from runtime events. Widgets render it; nothing here imports Qt.

`ConversationModel` is pure state: `apply` takes one event, `load` rebuilds from a
snapshot and a history page. `Controller` connects it to a RuntimeAPI: it subscribes,
applies envelopes in seq order, recovers on a gap, and runs commands as separate tasks so
a slow command never blocks event handling.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from kayden.api import (
    AgentState,
    Budget,
    ConversationDeleted,
    ConversationInfo,
    ConversationUpdated,
    Decision,
    EventEnvelope,
    Failure,
    FailureKind,
    MaintenanceFinished,
    MaintenanceStarted,
    MemoriesChanged,
    MemoryEntry,
    Message,
    MessageCommitted,
    MessageQueued,
    ModelRetry,
    ModelSelected,
    OperationResult,
    PermissionPolicy,
    PermissionRequest,
    PermissionRequested,
    PermissionResolved,
    Reasoning,
    ReasoningDelta,
    Role,
    RuntimeAPI,
    RuntimeEvent,
    RuntimeSnapshot,
    RuntimeStateError,
    Settings,
    SettingsChanged,
    SettingsUpdate,
    ShuttingDown,
    StartOptions,
    StateChanged,
    StopReason,
    StopStalled,
    TextDelta,
    ToolBackgrounded,
    ToolCall,
    ToolCallDelta,
    ToolFinished,
    ToolProgress,
    ToolResult,
    ToolStarted,
    ToolStatus,
    UsageSnapshot,
    UsageUpdated,
)
from kayden.api import Text as TextBlock
from kayden.frontends.common import money
from kayden.runtime import InstanceLockedError

HISTORY_PAGE = 100
MAX_ARGS_CHARS = 120
LABEL_CHARS = 60  # a conversation's label in the sidebar

STATE_LABELS = {
    AgentState.NOT_STARTED: "Not started",
    AgentState.IDLE: "Ready",
    AgentState.THINKING: "Thinking",
    AgentState.RUNNING_TOOLS: "Working",
    AgentState.AWAITING_PERMISSION: "Waiting for you",
    AgentState.STOPPING: "Stopping",
    AgentState.STOPPED: "Stopped",
    AgentState.FAILED: "Failed",
}
RESULT_LABELS = {
    ToolStatus.OK: "done",
    ToolStatus.ERROR: "failed",
    ToolStatus.REFUSED: "declined",
    ToolStatus.CANCELLED: "cancelled",
    ToolStatus.TIMED_OUT: "timed out",
    ToolStatus.ABANDONED: "abandoned, may still be running",
    ToolStatus.INTERRUPTED: "interrupted",
}
DECISION_NOTES = {
    Decision.ALLOW_ONCE: "allowed",
    Decision.ALLOW_CONVERSATION: "allowed for this conversation",
    Decision.DENY: "denied",
}
BUSY_STATES = (AgentState.THINKING, AgentState.RUNNING_TOOLS, AgentState.AWAITING_PERMISSION,
               AgentState.STOPPING)


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def summarize_arguments(arguments: Any) -> str:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    return clip(raw, MAX_ARGS_CHARS)


class Phase(StrEnum):
    PREPARING = "preparing"  # the model is still writing the call
    RUNNING = "running"
    BACKGROUND = "in the background"
    DONE = "done"
    NOT_RUN = "not run"


# ── transcript items (compared by identity: a widget tracks its item) ─────


@dataclass(eq=False)
class UserItem:
    text: str
    message_id: str | None = None
    queued: bool = False


@dataclass(eq=False)
class AssistantItem:
    text: str = ""
    reasoning: str = ""
    final: bool = False  # committed; False while streaming
    interrupted: bool = False  # the cycle ended before it was committed
    refused: bool = False


@dataclass(eq=False)
class ToolItem:
    call_id: str
    name: str
    arguments: str = ""  # JSON text, or raw chunks while streaming
    phase: Phase = Phase.RUNNING
    progress: str = ""
    status: ToolStatus | None = None
    content: str = ""
    duration_s: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return summarize_arguments(self.arguments)

    @property
    def state_label(self) -> str:
        if self.status is not None:
            return RESULT_LABELS.get(self.status, self.status.value)
        return self.phase.value

    @property
    def cancellable(self) -> bool:
        return self.status is None and self.phase in (Phase.RUNNING, Phase.BACKGROUND)


@dataclass(eq=False)
class NoticeItem:
    text: str
    severity: str = "info"  # info | warning | error


type Item = UserItem | AssistantItem | ToolItem | NoticeItem


# ── change notifications for the widget layer ─────────────────────────────


@dataclass(frozen=True)
class Reset:
    """Everything changed: rebuild from model.items and model.prompts."""


@dataclass(frozen=True)
class Inserted:
    index: int
    item: Item


@dataclass(frozen=True)
class Updated:
    item: Item


@dataclass(frozen=True)
class Removed:
    item: Item


@dataclass(frozen=True)
class PromptsChanged:
    pass


@dataclass(frozen=True)
class StatusChanged:
    pass


@dataclass(frozen=True)
class Closed:
    reason: str


@dataclass(frozen=True)
class ConversationListChanged:
    """A conversation was renamed, archived, deleted or switched to autonomy: re-read the
    list."""


@dataclass(frozen=True)
class MemoryListChanged:
    """Long-term memory entries changed: re-read them."""


type Change = (Reset | Inserted | Updated | Removed | PromptsChanged | StatusChanged | Closed
               | ConversationListChanged | MemoryListChanged)


class ConversationModel:
    def __init__(self, on_change: Callable[[Change], None] = lambda change: None) -> None:
        self.on_change = on_change
        self.items: list[Item] = []
        self.prompts: dict[str, PermissionRequest] = {}
        self.last_seq = -1
        self.state = AgentState.NOT_STARTED
        self.pending_ops = 0
        self.conversation: ConversationInfo | None = None
        self.model: str | None = None
        self.usage: UsageSnapshot | None = None
        self.notice = ""  # transient status text: retries, maintenance, stop stalled
        self.failure: Failure | None = None  # shown as a banner until the next cycle
        self.has_earlier = False
        self.closed = False
        self._tools: dict[str, ToolItem] = {}
        self._stream: list[AssistantItem | ToolItem] = []  # not yet committed
        self._queued: dict[str, UserItem] = {}
        self._seen: set[str] = set()  # message ids rendered
        self._oldest_id: str | None = None
        self._insert_at: int | None = None  # set while prepending an earlier page

    # ── status ────────────────────────────────────────────────────────────

    @property
    def busy(self) -> bool:
        """A cycle runs: Send becomes Stop and messages are queued."""
        return self.state in BUSY_STATES

    @property
    def autonomous(self) -> bool:
        return self.conversation is not None and self.conversation.autonomous

    @property
    def private(self) -> bool:
        """The open conversation has Remember off."""
        return self.conversation is not None and not self.conversation.remember

    @property
    def status_text(self) -> str:
        parts = [STATE_LABELS[self.state]]
        if background := sum(1 for t in self._tools.values()
                             if t.phase is Phase.BACKGROUND and t.status is None):
            parts.append(f"{background} task{'s' if background != 1 else ''} running in the "
                         "background")
        if self.notice:
            parts.append(self.notice)
        return " · ".join(parts)

    @property
    def spending_text(self) -> str:
        if self.usage is None:
            return ""
        spent = self.usage.usage.cost_usd or 0.0
        remaining = self.usage.budget_remaining.get("max_cost_usd")
        if remaining is None:
            return f"{money(spent)} spent"
        return f"{money(spent)} of {money(spent + remaining)} spent"

    # ── recovery ──────────────────────────────────────────────────────────

    def load(self, snapshot: RuntimeSnapshot, history: Sequence[Message], *,
             page: int = HISTORY_PAGE) -> None:
        """Rebuild from a snapshot and the newest history page (RuntimeSnapshot order: the
        caller subscribed first and drops envelopes at or below snapshot.seq)."""
        self.items.clear()
        self.prompts.clear()
        self._tools.clear()
        self._stream.clear()
        self._queued.clear()
        self._seen.clear()
        self.last_seq = snapshot.seq
        self.state = snapshot.state
        self.pending_ops = len(snapshot.operations)
        self.conversation = snapshot.conversation
        self.model = snapshot.model
        self.usage = snapshot.usage
        self.notice = ""
        self.failure = None
        self.has_earlier = len(history) >= page
        self._oldest_id = history[0].id if history else None
        for message in history:
            self._render(message)
        for op in snapshot.operations:
            item = self._tools.get(op.call.id) or self._add_tool(op.call)
            item.phase = Phase.BACKGROUND if op.backgrounded else Phase.RUNNING
            item.progress = op.last_progress or item.progress
        for queued in snapshot.queued_messages:
            self._queue(queued)
        for request in snapshot.pending_permissions:
            self.prompts[request.id] = request
        self.on_change(Reset())

    @property
    def oldest_id(self) -> str | None:
        return self._oldest_id

    def prepend(self, page: Sequence[Message], *, page_size: int = HISTORY_PAGE) -> None:
        """Add an earlier history page above what is shown."""
        self._insert_at = 0
        try:
            for message in page:
                self._render(message)
        finally:
            self._insert_at = None
        if page:
            self._oldest_id = page[0].id
        self.has_earlier = len(page) >= page_size
        self.on_change(StatusChanged())

    # ── events ────────────────────────────────────────────────────────────

    def apply(self, event: RuntimeEvent) -> None:
        match event:
            case StateChanged(state=state, pending_ops=pending_ops):
                if state is AgentState.THINKING and self.state is not AgentState.THINKING:
                    self.failure = None
                self.state, self.pending_ops = state, pending_ops
                if state in (AgentState.IDLE, AgentState.STOPPED, AgentState.FAILED):
                    self._end_stream()
                if state is not AgentState.STOPPING:
                    self.notice = ""
            case TextDelta(text=text):
                self._stream_text(text, reasoning=False)
            case ReasoningDelta(text=text):
                self._stream_text(text, reasoning=True)
            case ToolCallDelta(call_id=call_id, name=name, arguments_chunk=chunk):
                item = self._tools.get(call_id)
                if item is None:
                    item = self._add_tool(ToolCall(call_id, name or "tool", {}), streaming=True)
                    item.phase = Phase.PREPARING
                    self._stream.append(item)
                if name:
                    item.name = name
                item.arguments += chunk
                self.on_change(Updated(item))
            case MessageQueued():
                self._queue(event)
            case MessageCommitted(message=message, stop=stop):
                self._commit(message, stop)
            case ToolStarted(call=call):
                item = self._tools.get(call.id) or self._add_tool(call)
                item.arguments = json.dumps(call.arguments, ensure_ascii=False)
                item.phase = Phase.RUNNING
                if item in self._stream:
                    self._stream.remove(item)
                self.on_change(Updated(item))
            case ToolProgress(call_id=call_id, text=text):
                if (item := self._tools.get(call_id)) and text:
                    item.progress = text
                    self.on_change(Updated(item))
            case ToolBackgrounded(call_id=call_id):
                if item := self._tools.get(call_id):
                    item.phase = Phase.BACKGROUND
                    self.on_change(Updated(item))
            case ToolFinished(result=result, duration_s=duration):
                self._finish(result, duration)
            case PermissionRequested(request=request):
                self.prompts[request.id] = request
                self.on_change(PromptsChanged())
            case PermissionResolved(request_id=request_id, decision=decision, call_id=call_id):
                if self.prompts.pop(request_id, None) is not None:
                    self.on_change(PromptsChanged())
                if item := self._tools.get(call_id):
                    item.notes.append(f"Permission: {DECISION_NOTES[decision]}")
                    self.on_change(Updated(item))
            case ModelRetry(attempt=attempt, max_attempts=max_attempts):
                self.notice = f"Retrying ({attempt + 1} of {max_attempts})…"
                self._discard_stream()
            case ModelSelected(model_id=model_id):
                self.model = model_id
            case SettingsChanged(settings=settings):
                self.model = settings.model or self.model
            case MaintenanceStarted():
                self.notice = "Tidying up memory…"
            case MaintenanceFinished():
                self.notice = ""
            case StopStalled(call_ids=call_ids):
                self.notice = "Some tasks did not stop in time"
                self._add(NoticeItem(
                    f"{len(call_ids)} task{'s' if len(call_ids) != 1 else ''} did not confirm "
                    "that they stopped and may still be running.", "warning"))
            case UsageUpdated(snapshot=snapshot):
                self.usage = snapshot
            case ConversationUpdated(info=info):
                if self.conversation is not None and info.id == self.conversation.id:
                    self.conversation = info
                self.on_change(ConversationListChanged())
            case ConversationDeleted():
                self.on_change(ConversationListChanged())
            case MemoriesChanged():
                self.on_change(MemoryListChanged())
            case Failure():
                self.failure = event
                self._add(NoticeItem(event.message, "error"))
            case ShuttingDown(reason=reason):
                self.close(reason)
        self.on_change(StatusChanged())

    def close(self, reason: str) -> None:
        if not self.closed:
            self.closed = True
            self._add(NoticeItem(f"Kayden is closing: {reason}", "warning"))
            self.on_change(Closed(reason))

    def notify(self, text: str, severity: str = "error") -> None:
        """An inline notice from the app itself, such as a refused command."""
        self._add(NoticeItem(text, severity))

    # ── internals ─────────────────────────────────────────────────────────

    def _add[I: Item](self, item: I) -> I:
        if self._insert_at is not None:
            index = self._insert_at
            self._insert_at += 1
        else:
            index = len(self.items)
        self.items.insert(index, item)
        self.on_change(Inserted(index, item))
        return item

    def _remove(self, item: Item) -> None:
        self.items.remove(item)
        self.on_change(Removed(item))

    def _add_tool(self, call: ToolCall, *, streaming: bool = False) -> ToolItem:
        arguments = "" if streaming else json.dumps(call.arguments, ensure_ascii=False)
        item = self._add(ToolItem(call.id, call.name, arguments))
        self._tools[call.id] = item
        return item

    def _queue(self, event: MessageQueued) -> None:
        if event.message_id not in self._queued and event.message_id not in self._seen:
            self._queued[event.message_id] = self._add(
                UserItem(event.text, event.message_id, queued=True))

    def _stream_text(self, text: str, *, reasoning: bool) -> None:
        tail = self._stream[-1] if self._stream else None
        if not isinstance(tail, AssistantItem):
            tail = self._add(AssistantItem())
            self._stream.append(tail)
        if reasoning:
            tail.reasoning += text
        else:
            tail.text += text
        self.on_change(Updated(tail))

    def _discard_stream(self) -> None:
        for item in self._stream:
            if isinstance(item, ToolItem):
                self._tools.pop(item.call_id, None)
            self._remove(item)
        self._stream.clear()

    def _end_stream(self) -> None:
        """The cycle ended without committing what streamed (Stop or a failure)."""
        for item in self._stream:
            if isinstance(item, AssistantItem):
                item.interrupted = True
            else:
                item.phase = Phase.NOT_RUN
            self.on_change(Updated(item))
        self._stream.clear()

    def _commit(self, message: Message, stop: StopReason | None) -> None:
        if message.id in self._seen:
            return
        if (queued := self._queued.pop(message.id, None)) is not None:
            self._seen.add(message.id)
            queued.queued = False
            self.on_change(Updated(queued))
            return
        if message.role is Role.ASSISTANT:
            self._discard_stream()  # replaced by the committed message
        last = self._render(message)
        if stop is StopReason.REFUSAL:
            last = last or self._add(AssistantItem(final=True))
            last.refused = True
            self.on_change(Updated(last))

    def _render(self, message: Message) -> AssistantItem | None:
        """Add a message's items; returns its last assistant text item, if any."""
        self._seen.add(message.id)
        if message.role is Role.USER:
            text = "".join(b.text for b in message.blocks if isinstance(b, TextBlock))
            if text:
                self._add(UserItem(text, message.id))
            for block in message.blocks:
                if isinstance(block, OperationResult):
                    self._finish(block, None)
            return None
        last = current = None
        for block in message.blocks:
            match block:
                case TextBlock(text=text):
                    current = current or self._add(AssistantItem(final=True))
                    current.text += text
                    last = current
                    self.on_change(Updated(current))
                case Reasoning(text=text):
                    current = current or self._add(AssistantItem(final=True))
                    current.reasoning += text
                    self.on_change(Updated(current))
                case ToolCall() as call:
                    if call.id not in self._tools:
                        self._add_tool(call)
                    current = None
                case ToolResult() as result:
                    self._finish(result, None)
                    current = None
        return last

    def _finish(self, result: ToolResult | OperationResult, duration: float | None) -> None:
        item = self._tools.get(result.call_id)
        if item is None:
            item = self._add_tool(ToolCall(result.call_id, "tool", {}))
        if result.status is ToolStatus.BACKGROUNDED:  # placeholder: still running
            if item.status is None:
                item.phase = Phase.BACKGROUND
        else:
            item.phase = Phase.DONE
            item.status = result.status
            item.content = result.content
            item.duration_s = duration if duration is not None else item.duration_s
        if item in self._stream:
            self._stream.remove(item)
        self.on_change(Updated(item))


# ── conversations ─────────────────────────────────────────────────────────


def conversation_label(info: ConversationInfo) -> str:
    """Its title, else the start of its first message, else when it began."""
    if info.title:
        return info.title
    if info.preview and info.preview.strip():
        return clip(info.preview, LABEL_CHARS)
    return time.strftime("Conversation of %d %b %Y, %H:%M", time.localtime(info.created_at))


def memory_source(entry: MemoryEntry) -> str:
    when = time.strftime("%d %b %Y", time.localtime(entry.created_at))
    if entry.source_conversation_id is None:
        return f"Added by you on {when}"
    return f"Learned in a conversation on {when}"


def newest_first(conversations: Sequence[ConversationInfo]) -> list[ConversationInfo]:
    return sorted(conversations, key=lambda c: c.updated_at, reverse=True)


def needs_welcome(settings: Settings) -> bool:
    """First run: no provider has a key yet. Never for a Core from config.toml or one
    that needs no key."""
    current = next((p for p in settings.providers if p.id == settings.provider), None)
    return (current is not None and current.key_name is not None
            and not any(p.has_key for p in settings.providers))


# ── settings form ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SettingsForm:
    """What the settings window's fields hold, as plain values."""

    provider: str | None
    api_key: str
    model: str | None
    maintenance_model: str | None  # None = same as the main model
    spending_limit: str  # dollars as typed; empty = no limit
    permissions: PermissionPolicy
    instructions: str
    work_folder: str | None  # None when the folder is not settable


def form_from(settings: Settings) -> SettingsForm:
    limit = settings.budget.max_cost_usd
    return SettingsForm(settings.provider, "", settings.model, settings.maintenance_model,
                        "" if limit is None else f"{limit:g}", settings.permissions,
                        settings.system_prompt, settings.work_folder)


def settings_update(current: Settings, form: SettingsForm) -> SettingsUpdate:
    """Only the fields that changed. ValueError with a message for the user when a value
    is invalid. A provider switch leaves the models alone: the new provider picks its own."""
    changes: dict[str, Any] = {}
    switching = form.provider is not None and form.provider != current.provider
    if switching:
        changes["provider"] = form.provider
    if key := form.api_key.strip():
        changes["api_key"] = key
    if not switching:
        if form.model is not None and form.model != current.model:
            changes["model"] = form.model
        if form.maintenance_model != current.maintenance_model:
            changes["maintenance_model"] = form.maintenance_model or ""
    text = form.spending_limit.strip().lstrip("$")
    try:
        limit = float(text) if text else None
    except ValueError:
        limit = -1.0
    if limit is not None and limit < 0:
        raise ValueError("The spending limit must be an amount in dollars, like 10 or 2.50.")
    if limit != current.budget.max_cost_usd:
        changes["budget"] = replace(current.budget, max_cost_usd=limit)
    if form.permissions != current.permissions:
        changes["permissions"] = form.permissions
    if form.instructions != current.system_prompt:
        if not form.instructions.strip():
            raise ValueError("The instructions can't be empty.")
        changes["system_prompt"] = form.instructions
    if form.work_folder is not None and current.work_folder is not None:
        folder = form.work_folder.strip()
        if folder != current.work_folder:
            if not folder:
                raise ValueError("Choose a work folder.")
            changes["work_folder"] = folder
    return SettingsUpdate(**changes)


def limit_update(current: Settings, text: str) -> Budget:
    """The budget for a spending limit typed on the welcome page."""
    form = replace(form_from(current), spending_limit=text)
    return settings_update(current, form).budget or current.budget


# ── controller ────────────────────────────────────────────────────────────

BUSY_MESSAGE = "Finish or stop the current task first."
OPEN_DELETE_MESSAGE = "This conversation is open. Open another one first, then delete it."
EMPTY_MEMORY_MESSAGE = "Write something to remember first."
NO_DELETE_MESSAGE = "Deleting conversations is not available with this memory setup."
BUDGET_MESSAGE = "The spending limit is reached. Raise it in Settings to continue."
_BUSY_REFUSALS = ("cannot start while", "outstanding", "idle")


def user_message(error: Exception) -> str:
    """A refused command as the user should read it."""
    text = str(error) or type(error).__name__
    if isinstance(error, InstanceLockedError):
        return f"Kayden is already open in another window ({text})."
    if isinstance(error, RuntimeStateError):
        if text.startswith("budget exhausted"):
            return BUDGET_MESSAGE
        if any(refusal in text for refusal in _BUSY_REFUSALS):
            return BUSY_MESSAGE
    return text


class Controller:
    """Runs the event pump for one ConversationModel and issues runtime commands."""

    def __init__(self, runtime: RuntimeAPI, model: ConversationModel) -> None:
        self.runtime = runtime
        self.model = model
        self._stream = runtime.events()  # subscribed now, before any snapshot
        self._inbox: asyncio.Queue[EventEnvelope | Callable[[], Awaitable[None]] | None] = (
            asyncio.Queue())
        self._tasks: set[asyncio.Task[Any]] = set()
        self.can_delete = True  # False once Memory said it cannot delete conversations

    async def run(self) -> None:
        """Until the event stream ends or ShuttingDown arrives."""
        pump = asyncio.create_task(self._pump())
        try:
            await self.recover()
            while (item := await self._inbox.get()) is not None:
                if callable(item):
                    await item()
                    continue
                if item.seq <= self.model.last_seq:
                    continue
                if item.seq != self.model.last_seq + 1:
                    await self.recover()  # lagged: deltas are lost, history is authoritative
                    if item.seq <= self.model.last_seq:
                        continue
                self.model.last_seq = item.seq
                self.model.apply(item.event)
                if self.model.closed:
                    break
        finally:
            pump.cancel()
            self.model.close("the event stream ended")

    def caught_up(self, seq: int) -> bool:
        """For tests: every event up to `seq` has been applied."""
        return self.model.last_seq >= seq and self._inbox.empty()

    async def _pump(self) -> None:
        try:
            async for envelope in self._stream:
                await self._inbox.put(envelope)
        finally:
            await self._inbox.put(None)

    async def recover(self) -> None:
        snapshot = self.runtime.snapshot()
        history = (await self.runtime.history(snapshot.conversation.id, limit=HISTORY_PAGE)
                   if snapshot.conversation else ())
        self.model.load(snapshot, history)

    def load_earlier(self) -> None:
        async def load() -> None:
            conversation, oldest = self.model.conversation, self.model.oldest_id
            if conversation is None or oldest is None:
                return
            page = await self.runtime.history(conversation.id, before=oldest,
                                              limit=HISTORY_PAGE)
            self.model.prepend(page)

        self._inbox.put_nowait(load)  # in order with events, so nothing is rendered twice

    def command(self, awaitable: Awaitable[Any],
                done: Callable[[Any], None] | None = None,
                failed: Callable[[Exception], None] | None = None) -> asyncio.Task[None]:
        """Run a runtime command as its own task; a refusal becomes an inline notice."""

        async def run() -> None:
            try:
                result = await awaitable
            except Exception as e:  # noqa: BLE001 - any refusal is shown, never fatal
                if failed is not None:
                    failed(e)
                else:
                    self.model.notify(user_message(e))
                return
            if done is not None:
                done(result)

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ── commands ──────────────────────────────────────────────────────────

    def send(self, text: str) -> None:
        if text.strip():
            self.command(self.runtime.send(text.strip()))

    def stop(self) -> None:
        self.model.notice = "Stop requested"
        self.model.on_change(StatusChanged())
        self.command(self.runtime.stop())

    def cancel(self, call_id: str) -> None:
        self.command(self.runtime.cancel_operation(call_id))

    def resolve(self, request_id: str, decision: Decision) -> None:
        self.command(self.runtime.resolve_permission(request_id, decision))

    def select_model(self, model_id: str) -> None:
        self.command(self.runtime.select_model(model_id))

    def open_conversation(self, conversation_id: str | None, *, remember: bool = True,
                          seed_from: str | None = None) -> asyncio.Task[None]:
        """Resume a conversation, or start a new one (None): private with remember=False,
        continuing where `seed_from` left off when set."""

        async def start() -> None:
            await self.runtime.start(StartOptions(conversation_id, remember=remember,
                                                  seed_from=seed_from))
            await self._inbox.put(self.recover)

        return self.command(start())

    async def conversations(self, *, include_archived: bool = False) -> list[ConversationInfo]:
        return newest_first(await self.runtime.list_conversations(
            include_archived=include_archived))

    def set_autonomous(self, autonomous: bool) -> asyncio.Task[None]:
        return self.command(self.runtime.set_autonomous(autonomous))

    def rename(self, conversation_id: str, title: str) -> asyncio.Task[None]:
        """An empty title lets Kayden name the conversation again."""
        return self.command(self.runtime.rename_conversation(conversation_id, title.strip()))

    def archive(self, conversation_id: str, archived: bool) -> asyncio.Task[None]:
        return self.command(self.runtime.archive_conversation(conversation_id, archived))

    def delete_refusal(self, conversation_id: str) -> str | None:
        """Why this conversation cannot be deleted right now, or None."""
        if self.model.conversation is not None and conversation_id == self.model.conversation.id:
            return OPEN_DELETE_MESSAGE
        return None

    def delete(self, conversation_id: str) -> asyncio.Task[None]:
        """Erase for good; the caller has asked the user to confirm."""

        def failed(error: Exception) -> None:
            if isinstance(error, NotImplementedError):
                self.can_delete = False
                self.model.notify(NO_DELETE_MESSAGE)
            else:
                self.model.notify(user_message(error))

        if reason := self.delete_refusal(conversation_id):
            self.model.notify(reason)
            return self.command(asyncio.sleep(0))
        return self.command(self.runtime.delete_conversation(conversation_id), failed=failed)

    async def memories(self) -> list[MemoryEntry] | None:
        """Long-term memory entries, oldest first; None when the Memory module cannot show
        them, so their controls are hidden."""
        try:
            return list(await self.runtime.memories())
        except NotImplementedError:
            return None

    def add_memory(self, text: str) -> asyncio.Task[None]:
        if not text.strip():
            self.model.notify(EMPTY_MEMORY_MESSAGE)
            return self.command(asyncio.sleep(0))
        return self.command(self.runtime.add_memory(text.strip()))

    def edit_memory(self, entry_id: str, text: str) -> asyncio.Task[None]:
        if not text.strip():
            self.model.notify(EMPTY_MEMORY_MESSAGE)
            return self.command(asyncio.sleep(0))
        return self.command(self.runtime.edit_memory(entry_id, text.strip()))

    def forget_memory(self, entry_id: str) -> asyncio.Task[None]:
        return self.command(self.runtime.forget_memory(entry_id))

    def can_switch(self) -> str | None:
        """Why the conversation cannot change right now, or None."""
        snapshot = self.runtime.snapshot()
        if snapshot.operations or snapshot.state in BUSY_STATES:
            return BUSY_MESSAGE
        return None

    @property
    def failure_is_budget(self) -> bool:
        return self.model.failure is not None and self.model.failure.kind is FailureKind.BUDGET
