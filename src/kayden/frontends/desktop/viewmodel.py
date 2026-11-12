"""Toolkit-free state of the desktop app: the transcript as items, permission prompts and
the status line, built from runtime events. Widgets render it; nothing here imports Qt.

`ConversationModel` is pure state: `apply` takes one event, `load` rebuilds from a
snapshot and a history page. `Controller` connects it to a RuntimeAPI: it subscribes,
applies envelopes in seq order, recovers on a gap, and runs commands as separate tasks so
a slow command never blocks event handling.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from kayden.api import (
    AgentState,
    Attachment,
    Budget,
    CompactionFinished,
    CompactionStarted,
    ConversationDeleted,
    ConversationInfo,
    ConversationUpdated,
    Decision,
    Document,
    Duty,
    DutyScheduled,
    EventEnvelope,
    Failure,
    FailureKind,
    File,
    HelpNeeded,
    Image,
    InstanceLockedError,
    MaintenanceFinished,
    MaintenanceStarted,
    MemoriesChanged,
    MemoryNote,
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
    PlanItem,
    Proposal,
    ProposalMade,
    ProposalResolved,
    Reasoning,
    ReasoningDelta,
    Role,
    RuntimeAPI,
    RuntimeEvent,
    RuntimeSnapshot,
    RuntimeStateError,
    ServerToolUse,
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
from kayden.frontends.common import (
    CARD_TOOLS,
    COMPACTED,
    COMPACTING,
    RESULT_LABELS,
    Card,
    call_parts,
    card_of,
    duty_status,
    group_title,
    help_text,
    money,
    notice_of,
    past_tense,
    plan_steps,
    provider_step,
)
from kayden.frontends.sessions import SubSessions

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
BUSY_STATES = (AgentState.THINKING, AgentState.RUNNING_TOOLS, AgentState.AWAITING_PERMISSION,
               AgentState.STOPPING)


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


LIMIT_WARNING = 0.8  # of the spending limit: one notice when it is passed


class Phase(StrEnum):
    PREPARING = "preparing"  # the model is still writing the call
    WAITING = "waiting"  # written, not started: a permission prompt or review, earlier calls
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
    attachments: tuple[Attachment, ...] = ()  # what the user attached to it


@dataclass(eq=False)
class AssistantItem:
    text: str = ""
    reasoning: str = ""
    final: bool = False  # committed; False while streaming
    created_at: float | None = None  # when its message was committed
    interrupted: bool = False  # the cycle ended before it was committed
    refused: bool = False
    # Thinking only, while what follows it streams: the stream's line stands in for it, and
    # the committed message is joined to it (ConversationModel._thinking).
    hidden: bool = False


@dataclass(eq=False)
class ToolItem:
    call_id: str
    name: str
    arguments: str = ""  # JSON text, or raw chunks while streaming
    phase: Phase = Phase.WAITING
    progress: str = ""
    status: ToolStatus | None = None
    content: str = ""
    duration_s: float | None = None

    @property
    def sentence(self) -> str | None:
        """The model's plain-language description of the call, if it gave one."""
        headline = self._parts()[0]
        return clip(headline, MAX_ARGS_CHARS) if headline else None

    @property
    def summary(self) -> str:
        """The call's description, in the past tense once it succeeded, else its arguments."""
        if self.sentence is None:
            return clip(self.detail, MAX_ARGS_CHARS)
        return past_tense(self.sentence) if self.status is ToolStatus.OK else self.sentence

    @property
    def detail(self) -> str:
        """What runs: a command as it is, other arguments as JSON."""
        return self._parts()[1]

    def _parts(self) -> tuple[str | None, str]:
        try:
            return call_parts(json.loads(self.arguments))
        except ValueError:  # still streaming
            return None, self.arguments

    @property
    def state_label(self) -> str:
        if self.status is not None:
            return RESULT_LABELS.get(self.status, self.status.value)
        return self.phase.value

    @property
    def problem(self) -> str:
        """The state worth pointing out once ended: '' when it went fine."""
        return "" if self.status is ToolStatus.OK else self.state_label

    @property
    def cancellable(self) -> bool:
        return self.status is None and self.phase in (Phase.RUNNING, Phase.BACKGROUND)


@dataclass(eq=False)
class NoticeItem:
    text: str
    severity: str = "info"  # info | warning | error
    link: str | None = None  # a link after the text: "memory" opens the Memory window


@dataclass(eq=False)
class ChecklistItem:
    """The plan as the agent last wrote it during one turn (its update_plan calls), shown
    under that turn's newest message and left there when the turn ends."""

    steps: tuple[PlanItem, ...]
    live: bool = False  # the agent is working on it now


type Item = UserItem | AssistantItem | ToolItem | NoticeItem | ChecklistItem
type Segment = Item | list[Item]


def _in_steps(item: Item) -> bool:
    return isinstance(item, ToolItem) or (
        isinstance(item, AssistantItem) and not item.text and not item.refused)


def step_segments(items: Sequence[Item]) -> list[Segment]:
    """Items as shown: each run of steps, with the thinking before and between them, as one
    group (a list); every other item on its own. Thinking after a run's last step stays out
    until another step follows it, so a reply's thinking is never folded away."""
    segments: list[Segment] = []
    run: list[Item] = []

    def flush() -> None:
        last = max((i for i, item in enumerate(run) if isinstance(item, ToolItem)), default=-1)
        if last >= 0:
            segments.append(run[: last + 1])
        segments.extend(run[last + 1:])
        run.clear()

    for item in items:
        if _in_steps(item):
            run.append(item)
        else:
            flush()
            segments.append(item)
    flush()
    return segments


def group_heading(steps: Sequence[ToolItem]) -> tuple[str, str, ToolItem | None]:
    """A group's title, its state, and the step still under way (None when all ended)."""
    title, state, active = group_title([
        (s.summary, s.problem,
         s.state_label if s.status is None and s.phase is not Phase.NOT_RUN else None)
        for s in steps])
    return title, state, None if active is None else steps[active]


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
    """A conversation was renamed, archived or deleted: re-read the
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
        self.proposals: dict[str, Proposal] = {}  # shown with the prompts
        self.card: Card | None = None  # the agent's wrap-up card under its answer
        self.last_seq = -1
        self.state = AgentState.NOT_STARTED
        self.pending_ops = 0
        self.conversation: ConversationInfo | None = None
        self.model: str | None = None
        self.usage: UsageSnapshot | None = None
        self.duty_wake_at: float | None = None
        self.help_needed: str | None = None
        self.permissions: PermissionPolicy | None = None  # what the agent may do
        self.notice = ""  # transient status text: retries, maintenance, stop stalled
        self.failure: Failure | None = None  # shown as a banner until the next cycle
        self.has_earlier = False
        self.closed = False
        self._tools: dict[str, ToolItem] = {}
        self._stream: list[AssistantItem | ToolItem] = []  # not yet committed
        self._model_next = True  # the cycle's next step is the model's (waiting)
        self._queued: dict[str, UserItem] = {}
        self._seen: set[str] = set()  # message ids rendered
        self._oldest_id: str | None = None
        self._insert_at: int | None = None  # set while prepending an earlier page
        # The checklist of the turn being shown, and of the turn being prepended.
        self._turn_plan: ChecklistItem | None = None
        self._page_plan: ChecklistItem | None = None
        self._compaction: NoticeItem | None = None  # the line of a running compaction
        self._closed_card: str | None = None  # the card the user put away

    # ── status ────────────────────────────────────────────────────────────

    @property
    def busy(self) -> bool:
        """A cycle runs: Send becomes Stop and messages are queued."""
        return self.state in BUSY_STATES

    @property
    def waiting(self) -> bool:
        """The model works on its next step and nothing of it shows yet: before the first
        token, which a thinking model can take long to send."""
        return self.state is AgentState.THINKING and self._model_next and not self._stream

    @property
    def status_text(self) -> str:
        parts = [STATE_LABELS[self.state]]
        if background := sum(1 for t in self._tools.values()
                             if t.phase is Phase.BACKGROUND and t.status is None):
            parts.append(f"{background} task{'s' if background != 1 else ''} running in the "
                         "background")
        if self.conversation is not None and (duty := duty_status(
                self.conversation.duty, self.duty_wake_at, self.state, self.help_needed)):
            parts.append(duty)
        if self.notice:
            parts.append(self.notice)
        return " · ".join(parts)

    @property
    def limit_share(self) -> float | None:
        """How much of the spending limit is spent, 0 to 1; None without a limit."""
        if self.usage is None:
            return None
        remaining = self.usage.budget_remaining.get("max_cost_usd")
        if remaining is None:
            return None
        spent = self.usage.usage.cost_usd or 0.0
        return spent / (spent + remaining) if spent + remaining > 0 else 1.0

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
        self._turn_plan = self._page_plan = None
        self.prompts.clear()
        self.proposals.clear()
        self.card = None
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
        self.duty_wake_at = snapshot.duty_wake_at
        self.help_needed = snapshot.help_needed
        self.permissions = snapshot.config.permissions if snapshot.config else None
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
        for proposal in snapshot.proposals:
            self.proposals[proposal.id] = proposal
        self.on_change(Reset())

    @property
    def oldest_id(self) -> str | None:
        return self._oldest_id

    def prepend(self, page: Sequence[Message], *, page_size: int = HISTORY_PAGE) -> None:
        """Add an earlier history page above what is shown."""
        self._insert_at = 0
        self._page_plan = None
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
                    self._model_next = True
                self.state, self.pending_ops = state, pending_ops
                self._set_live(self._turn_plan, self.busy)
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
                    if item in self.items:  # a wrap_up call is shown as the card instead
                        self._stream.append(item)
                if name:
                    item.name = name
                item.arguments += chunk
                self.on_change(Updated(item))
            case MessageQueued():
                self._queue(event)
            case MessageCommitted(message=message, stop=stop):
                # After an answer without calls the cycle only tidies up (Memory's upkeep).
                self._model_next = message.role is not Role.ASSISTANT or any(
                    isinstance(b, ToolCall) for b in message.blocks)
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
            case PermissionResolved(request_id=request_id):
                if self.prompts.pop(request_id, None) is not None:
                    self.on_change(PromptsChanged())
            case ProposalMade(proposal=proposal):
                self.proposals[proposal.id] = proposal
                self.on_change(PromptsChanged())
            case ProposalResolved(proposal_id=proposal_id):
                if self.proposals.pop(proposal_id, None) is not None:
                    self.on_change(PromptsChanged())
            case ModelRetry(attempt=attempt, max_attempts=max_attempts):
                self.notice = f"Retrying ({attempt + 1} of {max_attempts})…"
                self._discard_stream()
            case ModelSelected(model_id=model_id):
                self.model = model_id
            case SettingsChanged(settings=settings):
                self.model = settings.model or self.model
                self.permissions = settings.permissions
                self.on_change(StatusChanged())
            case MaintenanceStarted():
                self.notice = "Tidying up memory…"
            case MaintenanceFinished():
                self.notice = ""
            case CompactionStarted():
                self._compaction = self._add(NoticeItem(COMPACTING))
            case CompactionFinished(compacted=compacted):
                if (line := self._compaction) is not None and line in self.items:
                    if compacted:
                        line.text = COMPACTED
                        self.on_change(Updated(line))
                    else:
                        self._remove(line)
                self._compaction = None
            case StopStalled(call_ids=call_ids):
                self.notice = "Some tasks did not stop in time"
                self._add(NoticeItem(
                    f"{len(call_ids)} task{'s' if len(call_ids) != 1 else ''} did not confirm "
                    "that they stopped and may still be running.", "warning"))
            case UsageUpdated(snapshot=snapshot):
                before = self.limit_share
                self.usage = snapshot
                after = self.limit_share
                if before is not None and after is not None and before < LIMIT_WARNING <= after:
                    spent = snapshot.usage.cost_usd or 0.0
                    limit = spent + snapshot.budget_remaining["max_cost_usd"]
                    self._add(NoticeItem(f"You have spent {money(spent)} of your {money(limit)} "
                                         "limit. Kayden stops when it is reached; raise it in "
                                         "Settings.", "warning"))
            case ConversationUpdated(info=info):
                if self.conversation is not None and info.id == self.conversation.id:
                    self.conversation = info
                    self.on_change(StatusChanged())
                self.on_change(ConversationListChanged())
            case DutyScheduled(wake_at=wake_at):
                self.duty_wake_at = wake_at
                self.on_change(StatusChanged())
            case HelpNeeded(question=question):
                self.help_needed = question
                if question is not None:
                    self._add(NoticeItem(help_text(question), "warning"))
                self.on_change(StatusChanged())
            case ConversationDeleted():
                self.on_change(ConversationListChanged())
            case MemoriesChanged(saved=saved):
                if saved:  # Memory saved on its own after the answer: say so, as Claude does
                    self._add(NoticeItem(f"Memory updated: {', '.join(saved)}", link="memory"))
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
        plan = self._turn_plan if self._insert_at is None else self._page_plan
        if plan is not None and item is not plan and plan in self.items:
            # The checklist stays under its turn's newest message.
            self._remove(plan)
            last = len(self.items) if self._insert_at is None else self._insert_at - 1
            self.items.insert(last, plan)
            self.on_change(Inserted(last, plan))
        return item

    def _show_plan(self, steps: tuple[PlanItem, ...]) -> None:
        """The turn's checklist, replaced by the agent's newest update_plan call."""
        prepending = self._insert_at is not None
        old = self._page_plan if prepending else self._turn_plan
        if old is not None and old in self.items:
            if prepending and self._insert_at is not None:
                self._insert_at -= 1
            self._remove(old)
        new = None
        if steps:
            new = ChecklistItem(steps, live=not prepending and self.busy)
            if prepending:  # so that adding it does not move the one it replaces
                self._page_plan = None
            else:
                self._turn_plan = None
            self._add(new)
        if prepending:
            self._page_plan = new
        else:
            self._turn_plan = new

    def _end_turn_plan(self) -> None:
        """A new turn begins: the checklist stays where it is, as history."""
        if self._insert_at is not None:
            self._page_plan = None
            return
        self._set_live(self._turn_plan, False)
        self._turn_plan = None

    def _set_live(self, plan: ChecklistItem | None, live: bool) -> None:
        if plan is not None and plan.live != live:
            plan.live = live
            self.on_change(Updated(plan))

    def _remove(self, item: Item) -> None:
        self.items.remove(item)
        self.on_change(Removed(item))

    def _add_tool(self, call: ToolCall, *, streaming: bool = False) -> ToolItem:
        arguments = "" if streaming else json.dumps(call.arguments, ensure_ascii=False)
        item = ToolItem(call.id, call.name, arguments)
        if call.name not in CARD_TOOLS:  # shown as a card instead
            self._add(item)
        self._tools[call.id] = item
        return item

    def _set_card(self, card: Card | None) -> None:
        if card is not None and card.call_id == self._closed_card:
            card = None  # put away: reloading the history does not bring it back
        if card != self.card:
            self.card = card
            self.on_change(PromptsChanged())

    def close_card(self) -> None:
        """The user put the card away; typing an answer still works."""
        if self.card is not None:
            self._closed_card = self.card.call_id
        self._set_card(None)

    def _queue(self, event: MessageQueued) -> None:
        if notice_of(event.text) is not None:
            return  # shown once it is committed, as a notice
        self._set_card(None)
        if event.message_id not in self._queued and event.message_id not in self._seen:
            self._queued[event.message_id] = self._add(
                UserItem(event.text, event.message_id, queued=True,
                         attachments=tuple(event.attachments)))

    def _stream_text(self, text: str, *, reasoning: bool) -> None:
        tail = self._stream[-1] if self._stream else None
        if not isinstance(tail, AssistantItem):
            if not self._stream and (before := self._thinking()) is not None:
                before.hidden = True
                self.on_change(Updated(before))
            tail = self._add(AssistantItem())
            self._stream.append(tail)
        if reasoning:
            tail.reasoning += text
        else:
            tail.text += text
        self.on_change(Updated(tail))

    def _thinking(self) -> AssistantItem | None:
        """The committed thinking, without a reply, that the conversation ends on (a
        checklist aside): what the agent says next belongs with it. Only something not
        shown as a step (a plan update) can lie between the two, so on screen they would
        be two Thinking lines one under the other."""
        if self._insert_at is not None:
            return None
        for item in reversed(self.items):
            if isinstance(item, ChecklistItem) or item in self._stream:
                continue
            if (isinstance(item, AssistantItem) and item.final and item.reasoning
                    and not item.text and not item.refused and not item.interrupted):
                return item
            return None
        return None

    def _show_thinking(self) -> None:
        for item in self.items:
            if isinstance(item, AssistantItem) and item.hidden:
                item.hidden = False
                self.on_change(Updated(item))

    def _discard_stream(self) -> None:
        for item in self._stream:
            if isinstance(item, ToolItem):
                self._tools.pop(item.call_id, None)
            self._remove(item)
        self._stream.clear()
        self._show_thinking()

    def _end_stream(self) -> None:
        """The cycle ended without committing what streamed (Stop or a failure)."""
        self._show_thinking()
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
            self._end_turn_plan()
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
            attached = tuple(b for b in message.blocks
                             if isinstance(b, (Image, File, Document)))
            said = bool(text or attached)
            if said and notice_of(text) is None and self._insert_at is None:
                self._set_card(None)  # answered: results and notices leave it up
            if said:
                self._end_turn_plan()
            if (notice := notice_of(text)) is not None:
                self._add(NoticeItem(notice, "info"))
            elif said:
                self._add(UserItem(text, message.id, attachments=attached))
            for block in message.blocks:
                if isinstance(block, OperationResult):
                    self._finish(block, None)
            return None
        last = None
        current = self._thinking()
        joined = current is not None
        for block in message.blocks:
            match block:
                case TextBlock(text=text):
                    current = current or self._add(
                        AssistantItem(final=True, created_at=message.created_at))
                    current.text += text
                    current.created_at = message.created_at  # the reply's, when joined
                    last = current
                    self.on_change(Updated(current))
                case Reasoning(text=text):
                    current = current or self._add(
                        AssistantItem(final=True, created_at=message.created_at))
                    if joined and text:
                        current.reasoning += "\n\n"
                        joined = False
                    current.reasoning += text
                    self.on_change(Updated(current))
                case ToolCall() as call:
                    if call.id not in self._tools:
                        self._add_tool(call)
                    if (card := card_of(call)) and self._insert_at is None:
                        self._set_card(card)
                    if call.name == "update_plan":
                        self._show_plan(plan_steps(call.arguments))
                    current = None
                case ToolResult() as result:
                    self._finish(result, None)
                    current = None
                case ServerToolUse() as use:  # the provider ran it: a finished step
                    call, result = provider_step(use)
                    if call.id not in self._tools:
                        self._add_tool(call)
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


def conversation_label(info: ConversationInfo, *, full: bool = False) -> str:
    """Its title, else the start of its first message (all of it with `full`), else
    "New chat"."""
    if info.title:
        return info.title
    if info.preview and info.preview.strip() and notice_of(info.preview) is None:
        return info.preview.strip() if full else clip(info.preview, LABEL_CHARS)
    return "New chat"


def conversation_preview(info: ConversationInfo) -> str:
    """The sidebar's second line: the start of the first message, unless it is the label."""
    if (not info.title or not info.preview or not info.preview.strip()
            or notice_of(info.preview) is not None):
        return ""
    return clip(info.preview, LABEL_CHARS)


def relative_age(timestamp: float, now: float | None = None) -> str:
    """How long ago, as a person would say it: "just now", "5 minutes ago", "3 hours ago",
    then the date (relative_date)."""
    seconds = (time.time() if now is None else now) - timestamp
    if seconds < 60:
        return "just now"
    for unit, size in (("minute", 60), ("hour", 3600)):
        count = int(seconds // size)
        if seconds < size * (60 if unit == "minute" else 24):
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return relative_date(timestamp, now)


def relative_date(timestamp: float, now: float | None = None) -> str:
    """The time today, "Yesterday", the weekday within a week, else the date."""
    when = time.localtime(timestamp)
    today = time.localtime(time.time() if now is None else now)
    days = (datetime.date(*today[:3]) - datetime.date(*when[:3])).days
    if days <= 0:
        return time.strftime("%H:%M", when)
    if days == 1:
        return "Yesterday"
    if days < 7:
        return time.strftime("%a", when)
    date = f"{when.tm_mday} {time.strftime('%b', when)}"
    return date if when.tm_year == today.tm_year else f"{date} {when.tm_year}"


def memory_source(note: MemoryNote) -> str:
    when = time.strftime("%d %b %Y", time.localtime(note.updated_at))
    if note.source_conversation_id is None:
        return f"Last written by you on {when}"
    return f"Last written in a conversation on {when}"


def newest_first(conversations: Sequence[ConversationInfo]) -> list[ConversationInfo]:
    return sorted(conversations, key=lambda c: c.updated_at, reverse=True)


def needs_welcome(settings: Settings) -> bool:
    """First run: Kayden could not start and no AI provider has a key yet. Never for a
    Core from config.toml or one that needs no key."""
    cores = settings.modules["core"]
    current = next((m for m in cores.installed if m.id == cores.chosen), None)
    return (settings.startup_error is not None and current is not None
            and current.key_name is not None and not any(m.has_key for m in cores.installed))


# ── settings form ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SettingsForm:
    """What the settings window's fields hold, as plain values."""

    modules: Mapping[str, str | None]  # the chosen id per kind
    api_key: str
    model: str | None
    maintenance_model: str | None  # None = same as the main model
    spending_limit: str  # dollars as typed; empty = no limit
    permissions: PermissionPolicy
    instructions: str
    work_folder: str | None  # None when the folder is not settable


def form_from(settings: Settings) -> SettingsForm:
    limit = settings.budget.max_cost_usd
    modules = {kind: choice.chosen for kind, choice in settings.modules.items()}
    return SettingsForm(modules, "", settings.model, settings.maintenance_model,
                        "" if limit is None else f"{limit:g}", settings.permissions,
                        settings.system_prompt, settings.work_folder)


def settings_update(current: Settings, form: SettingsForm) -> SettingsUpdate:
    """Only the fields that changed. ValueError with a message for the user when a value
    is invalid. A provider switch leaves the models alone: the new provider picks its own."""
    changes: dict[str, Any] = {}
    modules = {kind: module_id for kind, module_id in form.modules.items()
               if module_id is not None and module_id != current.modules[kind].chosen}
    if modules:
        changes["modules"] = modules
    switching = "core" in modules
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
NO_DELETE_MESSAGE = "Deleting conversations is not available with this memory setup."
BUDGET_MESSAGE = "The spending limit is reached. Raise it in Settings to continue."
_BUSY_REFUSALS = ("cannot start while", "outstanding", "idle")


def user_message(error: Exception) -> str:
    """A refused command as the user should read it."""
    text = str(error) or type(error).__name__
    if isinstance(error, InstanceLockedError):
        return f"{text}."
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
        self._switching = asyncio.Lock()  # conversation switches, one after another
        self.can_delete = True  # False once Memory said it cannot delete conversations
        self.sessions: SubSessions | None = None  # deleted with their conversation

    async def run(self) -> None:
        """Until the event stream ends or ShuttingDown arrives."""
        pump = asyncio.create_task(self._pump())
        try:
            await self.recover()
            while (item := await self._inbox.get()) is not None:
                if callable(item):
                    try:
                        await item()
                    except Exception as e:  # noqa: BLE001 - shown; the view keeps following
                        self.model.notify(user_message(e))
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

    def send(self, text: str, files: Sequence[str] = ()) -> None:
        """The user's message with the files they attached to it."""
        if text.strip() or files:
            self.command(self.runtime.send(text.strip(), files=list(files)) if files
                         else self.runtime.send(text.strip()))

    def stop(self) -> None:
        self.model.notice = "Stop requested"
        self.model.on_change(StatusChanged())
        self.command(self.runtime.stop())

    def cancel(self, call_id: str) -> None:
        self.command(self.runtime.cancel_operation(call_id))

    def resolve(self, request_id: str, decision: Decision) -> None:
        self.command(self.runtime.resolve_permission(request_id, decision))

    def resolve_proposal(self, proposal_id: str, accept: bool) -> None:
        self.command(self.runtime.resolve_proposal(proposal_id, accept))

    def select_model(self, model_id: str) -> None:
        self.command(self.runtime.select_model(model_id))

    def open_conversation(self, conversation_id: str | None, *,
                          seed_from: str | None = None) -> asyncio.Task[None]:
        """Resume a conversation, or start a new one (None), continuing where `seed_from`
        left off when set."""
        return self._switch(StartOptions(conversation_id, seed_from=seed_from))

    def new_in_folder(self, folder: str) -> asyncio.Task[None]:
        """Start a new chat in `folder`."""
        return self._switch(StartOptions(folder=folder))

    def _switch(self, options: StartOptions) -> asyncio.Task[None]:
        """One after another, so the conversation asked for last is the one left open."""

        async def start() -> None:
            async with self._switching:
                await self.runtime.start(options)
                await self._shown()

        return self.command(start())

    async def _shown(self) -> None:
        """Load the conversation now open, in order with events, and return once it is in
        the model: what runs after a switch (the sidebar) then sees the new one."""
        if self.model.closed:  # nothing reads the inbox any more
            return
        loaded: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def recover() -> None:
            try:
                await self.recover()
            finally:
                if not loaded.done():
                    loaded.set_result(None)

        await self._inbox.put(recover)
        await loaded

    async def conversations(self, *, include_archived: bool = False) -> list[ConversationInfo]:
        return newest_first(await self.runtime.list_conversations(
            include_archived=include_archived))

    def rename(self, conversation_id: str, title: str) -> asyncio.Task[None]:
        """An empty title lets Kayden name the conversation again."""
        return self.command(self.runtime.rename_conversation(conversation_id, title.strip()))

    def archive(self, conversation_id: str, archived: bool) -> asyncio.Task[None]:
        return self.command(self.runtime.archive_conversation(conversation_id, archived))

    def is_open(self, conversation_id: str) -> bool:
        return self.model.conversation is not None and conversation_id == self.model.conversation.id

    def delete(self, conversation_id: str) -> asyncio.Task[None]:
        """Erase for good; the caller has asked the user to confirm. The open conversation
        is first replaced by a new chat."""

        def failed(error: Exception) -> None:
            if isinstance(error, NotImplementedError):
                self.can_delete = False
                self.model.notify(NO_DELETE_MESSAGE)
            else:
                self.model.notify(user_message(error))

        async def erase() -> None:
            if self.is_open(conversation_id):
                await self.runtime.start(StartOptions())
                await self._shown()
            await self.runtime.delete_conversation(conversation_id)
            if self.sessions is not None:
                await self.sessions.delete_of(conversation_id)

        return self.command(erase(), failed=failed)

    async def notes(self) -> list[MemoryNote] | None:
        """Long-term memory notes, the profile first; None when the Memory module cannot
        show them, so their controls are hidden."""
        try:
            return list(await self.runtime.notes())
        except NotImplementedError:
            return None

    def save_note(self, title: str, description: str, text: str,
                  note_id: str | None = None,
                  failed: Callable[[Exception], None] | None = None,
                  done: Callable[[Any], None] | None = None) -> asyncio.Task[None]:
        return self.command(self.runtime.save_note(title, description, text, note_id=note_id),
                            done=done, failed=failed)

    def delete_note(self, note_id: str) -> asyncio.Task[None]:
        return self.command(self.runtime.delete_note(note_id))

    def save_duty(self, duty: Duty | None, plan: Sequence[PlanItem],
                  failed: Callable[[Exception], None] | None = None,
                  done: Callable[[Any], None] | None = None) -> asyncio.Task[None]:
        """Save the plan, then the duty: None ends a duty, if there is one."""
        async def save() -> None:
            await self.runtime.set_plan(plan)
            conversation = self.model.conversation
            if duty is not None or (conversation is not None and conversation.duty):
                await self.runtime.set_duty(duty)

        return self.command(save(), done=done, failed=failed)

    def can_switch(self) -> str | None:
        """Why the conversation cannot change right now, or None."""
        snapshot = self.runtime.snapshot()
        if snapshot.operations or snapshot.state in BUSY_STATES:
            return BUSY_MESSAGE
        return None

    @property
    def failure_is_budget(self) -> bool:
        return self.model.failure is not None and self.model.failure.kind is FailureKind.BUDGET
