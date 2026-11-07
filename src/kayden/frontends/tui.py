"""Terminal Frontend built on Textual.

All rendering happens in one consumer task that applies runtime events in sequence
order; runtime commands run as separate workers, so a slow or hanging runtime call
(Stop included) never freezes the interface.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Sequence
from typing import Any, ClassVar

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Footer, Input, Label, OptionList, Select, Static
from textual.widgets.option_list import Option

from kayden.api import (
    Activation,
    AgentConfig,
    AgentState,
    ConversationInfo,
    Decision,
    EventEnvelope,
    Failure,
    Frontend,
    FrontendInfo,
    InstancePaths,
    MaintenanceFinished,
    MaintenanceStarted,
    Message,
    MessageCommitted,
    MessageQueued,
    Mode,
    ModelInfo,
    ModelRetry,
    ModelSelected,
    OperationInfo,
    OperationResult,
    PermissionRequest,
    PermissionRequested,
    PermissionResolved,
    Reasoning,
    ReasoningDelta,
    Role,
    RuntimeAPI,
    RuntimeEvent,
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

STATE_LABELS = {
    AgentState.NOT_STARTED: "Not started",
    AgentState.IDLE: "Idle",
    AgentState.THINKING: "Thinking…",
    AgentState.RUNNING_TOOLS: "Running tools…",
    AgentState.AWAITING_PERMISSION: "Awaiting permission",
    AgentState.STOPPING: "Stopping…",
    AgentState.STOPPED: "Stopped",
    AgentState.FAILED: "Failed",
}
STATUS_ICONS = {
    ToolStatus.OK: ("✓", "green"),
    ToolStatus.ERROR: ("✗", "red"),
    ToolStatus.BACKGROUNDED: ("⋯", "yellow"),
    ToolStatus.CANCELLED: ("⊘", "yellow"),
    ToolStatus.ABANDONED: ("⊘", "red"),
    ToolStatus.TIMED_OUT: ("⏱", "yellow"),
    ToolStatus.INTERRUPTED: ("⚠", "yellow"),
    ToolStatus.REFUSED: ("⛔", "red"),
}
DECISION_LABELS = {
    Decision.ALLOW_ONCE: "Allow once",
    Decision.ALLOW_CONVERSATION: "Allow for conversation",
    Decision.DENY: "Deny",
}
DECISION_ALIASES = {
    "once": Decision.ALLOW_ONCE,
    "conversation": Decision.ALLOW_CONVERSATION,
    "conv": Decision.ALLOW_CONVERSATION,
}
HELP = (
    "Commands: /stop (Esc), /activate (Ctrl+R), /model [id] (Ctrl+O), "
    "/cancel [call id] (Ctrl+K), /allow [once|conversation], /deny, /quit (Ctrl+Q). "
    "Messages can be sent while the agent runs; they are queued."
)
HISTORY_PAGE = 100  # messages loaded per history page
MAX_ARGS_CHARS = 300
MAX_RESULT_LINES = 6
MAX_RESULT_CHARS = 600


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _format_args(arguments: Any) -> str:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    return _clip(raw, MAX_ARGS_CHARS)


# ── transcript entries ───────────────────────────────────────────────────


class UserEntry(Static):
    def __init__(self, text: str, *, queued: bool = False, message_id: str | None = None
                 ) -> None:
        super().__init__()
        self.message_text = text
        self.message_id = message_id
        self.queued = queued
        self.refresh_content()

    def mark_committed(self) -> None:
        self.queued = False
        self.refresh_content()

    def refresh_content(self) -> None:
        line = Text.assemble(("› ", "bold"), self.message_text)
        if self.queued:
            line.append("  (queued)", style="dim")
        self.update(line)


class AssistantEntry(Static):
    def __init__(self, text: str = "", reasoning: str = "", *, final: bool = False) -> None:
        super().__init__()
        self.body = text
        self.reasoning = reasoning
        self.final = final
        self.interrupted = False
        self.refused = False
        self.refresh_content()

    def append(self, text: str, *, reasoning: bool) -> None:
        if reasoning:
            self.reasoning += text
        else:
            self.body += text
        self.refresh_content()

    def mark_interrupted(self) -> None:
        self.interrupted = True
        self.refresh_content()

    def mark_refused(self) -> None:
        self.refused = True
        self.refresh_content()

    def refresh_content(self) -> None:
        parts: list[RenderableType] = []
        if self.reasoning:
            parts.append(Text(self.reasoning.strip(), style="dim italic"))
        if self.body:
            parts.append(Markdown(self.body) if self.final else Text(self.body))
        if self.interrupted:
            parts.append(Text("(interrupted)", style="dim"))
        if self.refused:
            parts.append(Text("⛔ The model declined to respond.", style="bold red"))
        self.update(Group(*parts))


class ToolEntry(Static):
    def __init__(self, call_id: str, name: str | None, arguments: Any = "") -> None:
        super().__init__()
        self.call_id = call_id
        self.tool_name = name or "…"
        self.arguments = arguments
        self.phase = "requested"  # requested | running | background | done | not run
        self.progress: str | None = None
        self.result: ToolResult | OperationResult | None = None
        self.duration: float | None = None
        self.notes: list[str] = []
        self.refresh_content()

    def stream_arguments(self, name: str | None, chunk: str) -> None:
        if name:
            self.tool_name = name
        self.arguments = (self.arguments if isinstance(self.arguments, str) else "") + chunk
        self.refresh_content()

    def set_phase(self, phase: str) -> None:
        self.phase = phase
        self.refresh_content()

    def set_progress(self, text: str) -> None:
        self.progress = text
        self.refresh_content()

    def finish(self, result: ToolResult | OperationResult, duration: float | None = None
               ) -> None:
        if result.status is ToolStatus.BACKGROUNDED:  # placeholder: still running
            if self.phase != "done":
                self.set_phase("background")
            return
        self.phase = "done"
        self.result = result
        self.duration = duration if duration is not None else self.duration
        self.refresh_content()

    def note(self, text: str) -> None:
        self.notes.append(text)
        self.refresh_content()

    def refresh_content(self) -> None:
        out = Text.assemble(("⚙ ", "cyan"), (self.tool_name, "bold cyan"), " ")
        out.append(_format_args(self.arguments), style="dim")
        if self.phase in ("running", "background"):
            label = self.phase
            out.append(f"\n  ⋯ {label}", style="yellow")
            if self.progress:
                out.append(f": {_clip(self.progress, 200)}", style="dim")
        elif self.phase == "not run":
            out.append("\n  (not run)", style="dim")
        for note in self.notes:
            out.append(f"\n  {note}", style="magenta")
        if self.result is not None:
            icon, style = STATUS_ICONS[self.result.status]
            head = f"\n  {icon} {self.result.status.value}"
            if self.duration is not None:
                head += f" ({self.duration:.1f}s)"
            out.append(head, style=style)
            lines = self.result.content.splitlines()
            body = "\n".join(lines[:MAX_RESULT_LINES])
            if len(lines) > MAX_RESULT_LINES:
                body += f"\n… {len(lines) - MAX_RESULT_LINES} more lines"
            if body:
                out.append("\n" + _clip(body, MAX_RESULT_CHARS), style="dim")
        self.update(out)


class SystemEntry(Static):
    def __init__(self, text: str, *, severity: str = "info") -> None:
        super().__init__(Text(text), classes=f"system {severity}")


# ── permission prompts ───────────────────────────────────────────────────


class PermissionPrompt(Vertical):
    def __init__(self, request: PermissionRequest) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        call = self.request.call
        kind = "read-only" if self.request.read_only else "may modify state"
        yield Label(
            Text.assemble(
                ("Permission requested: ", "bold"),
                (call.name, "bold cyan"),
                f" ({kind})\n",
                (_format_args(call.arguments), "dim"),
            )
        )
        with Horizontal(classes="buttons"):
            for decision, label in DECISION_LABELS.items():
                variant = "error" if decision is Decision.DENY else "primary"
                yield Button(label, id=decision.value, variant=variant)

    def set_busy(self, busy: bool) -> None:
        for button in self.query(Button):
            button.disabled = busy

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        event.stop()
        assert event.button.id is not None
        self.app.resolve_permission(self.request.id, Decision(event.button.id))  # type: ignore[attr-defined]


# ── modal screens ────────────────────────────────────────────────────────


class OperationsScreen(ModalScreen[str | None]):
    """Picks a running call to cancel."""

    BINDINGS: ClassVar = [Binding("escape", "dismiss(None)", "Close")]

    def __init__(self, operations: Sequence[OperationInfo]) -> None:
        super().__init__()
        self.operations = operations

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Cancel a running call" if self.operations else "Nothing is running")
            yield OptionList(
                *(Option(f"{op.call.name} {_clip(_format_args(op.call.arguments), 60)}"
                         f"  [{op.call.id}]"
                         + (f"  {_clip(op.last_progress, 40)}" if op.last_progress else ""),
                         id=op.call.id)
                  for op in self.operations),
                id="operations",
            )

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)


class ModelScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, models: Sequence[ModelInfo], current: str | None) -> None:
        super().__init__()
        self.models = models
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Select model (applies from the next inference)")
            options = [
                Option(f"{'● ' if m.id == self.current else '  '}{m.name}  [{m.id}]", id=m.id)
                for m in self.models
            ]
            yield OptionList(*options, id="models")

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)


NEW_CONVERSATION = "__new__"
DEFAULT_MODEL = "__default__"


class StartScreen(ModalScreen[StartOptions]):
    """Chooses mode, activation, conversation and model before the agent starts."""

    def __init__(self, runtime: RuntimeAPI) -> None:
        super().__init__()
        self.runtime = runtime

    def compose(self) -> ComposeResult:
        # Scrolls so Start stays reachable when the terminal is shorter than the dialog.
        with VerticalScroll(classes="dialog"):
            yield Label("Start Kayden", classes="title")
            yield Label("Mode")
            yield Select(
                [("Session", Mode.SESSION), ("Continuous", Mode.CONTINUOUS)],
                value=Mode.SESSION, allow_blank=False, id="mode",
            )
            yield Label("Activation (Continuous Mode)")
            yield Select(
                [
                    ("User-controlled", Activation.USER_CONTROLLED),
                    ("Agent-controlled", Activation.AGENT_CONTROLLED),
                ],
                value=Activation.USER_CONTROLLED, allow_blank=False, id="activation",
            )
            yield Label("Conversation")
            yield Select(
                [("New conversation", NEW_CONVERSATION)],
                value=NEW_CONVERSATION, allow_blank=False, id="conversation",
            )
            yield Label("Model")
            yield Select(
                [("Runtime default", DEFAULT_MODEL)],
                value=DEFAULT_MODEL, allow_blank=False, id="model",
            )
            yield Button("Start", variant="primary", id="start")

    def on_mount(self) -> None:
        self.query_one("#start", Button).focus()  # Enter starts with the defaults
        self.run_worker(self._load_models(), exit_on_error=False)
        self.run_worker(self._load_conversations(Mode.SESSION), group="conv", exclusive=True)

    async def _load_models(self) -> None:
        models = await self.runtime.list_models()
        self.query_one("#model", Select).set_options(
            [("Runtime default", DEFAULT_MODEL), *((f"{m.name} [{m.id}]", m.id) for m in models)]
        )
        self.query_one("#model", Select).value = DEFAULT_MODEL

    async def _load_conversations(self, mode: Mode) -> None:
        conversations = await self.runtime.list_conversations(mode)
        select = self.query_one("#conversation", Select)
        select.set_options(
            [
                ("New conversation", NEW_CONVERSATION),
                *((c.title or f"Untitled {c.id}", c.id) for c in conversations),
            ]
        )
        select.value = NEW_CONVERSATION

    @on(Select.Changed, "#mode")
    def _mode_changed(self, event: Select.Changed) -> None:
        mode = Mode(event.value)
        self.query_one("#activation", Select).disabled = mode is not Mode.CONTINUOUS
        self.run_worker(self._load_conversations(mode), group="conv", exclusive=True)

    @on(Button.Pressed, "#start")
    def _start(self) -> None:
        conversation = self.query_one("#conversation", Select).value
        model = self.query_one("#model", Select).value
        self.dismiss(
            StartOptions(
                mode=Mode(self.query_one("#mode", Select).value),
                activation=Activation(self.query_one("#activation", Select).value),
                conversation_id=None if conversation == NEW_CONVERSATION else str(conversation),
                model=None if model == DEFAULT_MODEL else str(model),
            )
        )


# ── app ──────────────────────────────────────────────────────────────────


class _Resync:
    pass


class _LoadEarlier:
    pass


RESYNC = _Resync()


def _next(container: Widget, child: Widget) -> Widget | None:
    children = list(container.children)
    index = children.index(child) + 1
    return children[index] if index < len(children) else None


class LoadEarlier(Button):
    def __init__(self) -> None:
        super().__init__("Load earlier messages", id="load-earlier", variant="default")


class KaydenApp(App[None]):
    TITLE = "Kayden"
    CSS = """
    #status { height: 1; padding: 0 1; background: $panel; }
    #transcript { height: 1fr; padding: 0 1; }
    #transcript > * { margin-bottom: 1; }
    UserEntry { color: $accent; }
    SystemEntry.warning { color: $warning; }
    SystemEntry.error { color: $error; }
    #permissions { height: auto; max-height: 50%; }
    PermissionPrompt { height: auto; border: round $warning; padding: 0 1; }
    PermissionPrompt .buttons { height: auto; }
    PermissionPrompt Button { margin-right: 1; }
    ModalScreen { align: center middle; }
    .dialog { width: 70; height: auto; max-height: 90%; border: thick $primary; padding: 1 2;
              background: $surface; }
    .dialog Select, .dialog Button { margin-bottom: 1; }
    """
    BINDINGS: ClassVar = [
        Binding("escape", "stop", "Stop"),
        Binding("ctrl+r", "activate", "Activate", priority=True),
        Binding("ctrl+o", "models", "Model", priority=True),
        Binding("ctrl+k", "operations", "Cancel op", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, runtime: RuntimeAPI, start_options: StartOptions | None = None) -> None:
        super().__init__()
        self.runtime = runtime
        self.start_options = start_options
        self.last_seq = -1
        self.state = AgentState.NOT_STARTED
        self.pending_ops = 0
        self.config: AgentConfig | None = None
        self.conversation: ConversationInfo | None = None
        self.model: str | None = None
        self.usage: UsageSnapshot | None = None
        self.notice = ""
        self._inbox: asyncio.Queue[EventEnvelope | _Resync | None] = asyncio.Queue()
        self._tool_entries: dict[str, ToolEntry] = {}
        self._stream: list[AssistantEntry | ToolEntry] = []
        self._queued: dict[str, UserEntry] = {}
        self._rendered_ids: set[str] = set()
        self._prompts: dict[str, PermissionPrompt] = {}
        self._oldest_id: str | None = None  # oldest history message loaded
        self._insert_before: Widget | None = None  # mount point while loading older pages

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        yield VerticalScroll(id="transcript")
        yield Vertical(id="permissions")
        yield Input(placeholder="Message, or /help", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        self._inbox.put_nowait(RESYNC)
        self.run_worker(self._consume(), name="consume", exit_on_error=True)
        self.run_worker(self._pump(), name="pump", exit_on_error=False)
        self.query_one("#prompt", Input).focus()
        if self.runtime.snapshot().conversation is None:
            if self.start_options is not None:
                self._call(self._start(self.start_options))
            else:
                self.push_screen(StartScreen(self.runtime), self._on_start_chosen)
        self._render_status()

    # ── event plumbing ───────────────────────────────────────────────────

    async def _pump(self) -> None:
        try:
            async for envelope in self.runtime.events():
                await self._inbox.put(envelope)
        finally:
            await self._inbox.put(None)

    async def _consume(self) -> None:
        while (item := await self._inbox.get()) is not None:
            if isinstance(item, _Resync):
                await self._resync()
                continue
            if isinstance(item, _LoadEarlier):
                await self._load_earlier()
                continue
            if item.seq <= self.last_seq:
                continue
            if item.seq != self.last_seq + 1:
                await self._resync()  # lagged: deltas are lost, history is authoritative
                if item.seq <= self.last_seq:
                    continue
            self.last_seq = item.seq
            await self._apply(item.event)
            self._render_status()
        self.exit()

    def idle_and_caught_up(self, seq: int) -> bool:
        """For tests: every event up to `seq` has been rendered."""
        return self.last_seq >= seq and self._inbox.empty()

    async def _resync(self) -> None:
        snap = self.runtime.snapshot()
        history = (await self.runtime.history(snap.conversation.id, limit=HISTORY_PAGE)
                   if snap.conversation else ())
        transcript = self.query_one("#transcript", VerticalScroll)
        await transcript.remove_children()
        await self.query_one("#permissions", Vertical).remove_children()
        self._tool_entries.clear()
        self._stream.clear()
        self._queued.clear()
        self._rendered_ids.clear()
        self._prompts.clear()
        self.last_seq = snap.seq
        self.state = snap.state
        self.pending_ops = len(snap.operations)
        self.config = snap.config
        self.conversation = snap.conversation
        self.model = snap.model
        self.usage = snap.usage
        self._oldest_id = history[0].id if history else None
        if len(history) == HISTORY_PAGE:
            await self._mount(LoadEarlier())
        for message in history:
            await self._render_message(message)
        for op in snap.operations:
            entry = self._tool_entries.get(op.call.id) or await self._mount_tool(op.call)
            if entry.phase != "background":
                entry.set_phase("running")
            if op.last_progress:
                entry.set_progress(op.last_progress)
        for queued in snap.queued_messages:
            await self._apply(queued)
        for request in snap.pending_permissions:
            await self._add_prompt(request)
        self._render_status()

    @on(Button.Pressed, "#load-earlier")
    def _load_earlier_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        event.button.disabled = True
        self._inbox.put_nowait(_LoadEarlier())

    async def _load_earlier(self) -> None:
        """Prepend the page of history before the oldest message shown."""
        button = self.query_one(LoadEarlier)
        if self.conversation is None or self._oldest_id is None:
            await button.remove()
            return
        page = await self.runtime.history(self.conversation.id, before=self._oldest_id,
                                          limit=HISTORY_PAGE)
        transcript = self.query_one("#transcript", VerticalScroll)
        self._insert_before = _next(transcript, button)
        try:
            for message in page:
                await self._render_message(message)
        finally:
            self._insert_before = None
        if page:
            self._oldest_id = page[0].id
        if len(page) < HISTORY_PAGE:
            await button.remove()
        else:
            button.disabled = False

    async def _apply(self, event: RuntimeEvent) -> None:
        match event:
            case StateChanged(state=state, pending_ops=pending_ops):
                self.state, self.pending_ops = state, pending_ops
                if state in (AgentState.IDLE, AgentState.STOPPED, AgentState.FAILED):
                    self._end_stream()
                if state is not AgentState.STOPPING:
                    self.notice = ""
            case TextDelta(text=text):
                await self._stream_text(text, reasoning=False)
            case ReasoningDelta(text=text):
                await self._stream_text(text, reasoning=True)
            case ToolCallDelta(call_id=call_id, name=name, arguments_chunk=chunk):
                entry = self._tool_entries.get(call_id)
                if entry is None:
                    entry = await self._mount(ToolEntry(call_id, name))
                    self._tool_entries[call_id] = entry
                    self._stream.append(entry)
                entry.stream_arguments(name, chunk)
            case MessageQueued(message_id=message_id, text=text):
                if message_id not in self._queued:
                    entry = UserEntry(text, queued=True, message_id=message_id)
                    self._queued[message_id] = await self._mount(entry)
            case MessageCommitted(message=message, stop=stop):
                await self._commit(message, stop)
            case ToolStarted(call=call):
                entry = self._tool_entries.get(call.id) or await self._mount_tool(call)
                entry.set_phase("running")
            case ToolProgress(call_id=call_id, text=text):
                if entry := self._tool_entries.get(call_id):
                    entry.set_progress(text)
            case ToolBackgrounded(call_id=call_id):
                if entry := self._tool_entries.get(call_id):
                    entry.set_phase("background")
            case ToolFinished(result=result, duration_s=duration):
                await self._finish_tool(result, duration)
            case PermissionRequested(request=request):
                await self._add_prompt(request)
            case PermissionResolved(request_id=request_id, decision=decision, call_id=call_id):
                if (prompt := self._prompts.pop(request_id, None)) is not None:
                    await prompt.remove()
                if entry := self._tool_entries.get(call_id):
                    entry.note(f"permission: {DECISION_LABELS[decision].lower()}")
            case ModelRetry(attempt=attempt, max_attempts=max_attempts, delay_s=delay, error=err):
                self.notice = f"model retry {attempt}/{max_attempts} in {delay:.0f}s: {err}"
                await self._discard_stream()  # the retried request streams from scratch
            case ModelSelected(model_id=model_id):
                self.model = model_id
            case MaintenanceStarted():
                self.notice = "maintaining memory…"
            case MaintenanceFinished():
                self.notice = ""
            case StopStalled(call_ids=call_ids):
                self.notice = f"stop stalled: abandoned {', '.join(call_ids)}"
                await self._mount(
                    SystemEntry(
                        f"These calls did not confirm cancellation and were abandoned (they "
                        f"may still have effects): {', '.join(call_ids)}",
                        severity="warning",
                    )
                )
            case UsageUpdated(snapshot=snapshot):
                self.usage = snapshot
            case Failure(kind=kind, message=message):
                await self._mount(SystemEntry(f"{kind.value} failure: {message}", severity="error"))
            case ShuttingDown(reason=reason):
                await self._mount(SystemEntry(f"Shutting down: {reason}", severity="warning"))
                self.exit()

    # ── transcript rendering ─────────────────────────────────────────────

    async def _mount[W: Widget](self, widget: W) -> W:
        transcript = self.query_one("#transcript", VerticalScroll)
        if self._insert_before is not None:
            await transcript.mount(widget, before=self._insert_before)
            return widget
        at_end = transcript.scroll_y >= transcript.max_scroll_y - 1
        await transcript.mount(widget)
        if at_end:
            transcript.scroll_end(animate=False)
        return widget

    async def _mount_tool(self, call: ToolCall) -> ToolEntry:
        entry = await self._mount(ToolEntry(call.id, call.name, call.arguments))
        self._tool_entries[call.id] = entry
        return entry

    async def _stream_text(self, text: str, *, reasoning: bool) -> None:
        tail = self._stream[-1] if self._stream else None
        if not isinstance(tail, AssistantEntry):
            tail = await self._mount(AssistantEntry())
            self._stream.append(tail)
        tail.append(text, reasoning=reasoning)

    async def _discard_stream(self) -> None:
        for widget in self._stream:
            if isinstance(widget, ToolEntry):
                self._tool_entries.pop(widget.call_id, None)
            await widget.remove()
        self._stream.clear()

    def _end_stream(self) -> None:
        """The cycle ended without committing what was streamed (Stop or failure)."""
        for widget in self._stream:
            if isinstance(widget, AssistantEntry):
                widget.mark_interrupted()
            elif widget.phase == "requested":
                widget.set_phase("not run")
        self._stream.clear()

    async def _commit(self, message: Message, stop: StopReason | None = None) -> None:
        if message.id in self._rendered_ids:
            return
        if (queued := self._queued.pop(message.id, None)) is not None:
            queued.mark_committed()
            self._rendered_ids.add(message.id)
            return
        if message.role is Role.ASSISTANT:
            await self._discard_stream()  # replaced by the authoritative message
        last = await self._render_message(message)
        if stop is StopReason.REFUSAL:
            if last is None:
                last = await self._mount(AssistantEntry(final=True))
            last.mark_refused()

    async def _render_message(self, message: Message) -> AssistantEntry | None:
        """Mount a message's entries; returns its last assistant text entry, if any."""
        self._rendered_ids.add(message.id)
        if message.role is Role.USER:
            text = "".join(b.text for b in message.blocks if isinstance(b, TextBlock))
            if text:
                await self._mount(UserEntry(text, message_id=message.id))
            for block in message.blocks:
                if isinstance(block, OperationResult):
                    await self._finish_tool(block, None)
            return None
        last: AssistantEntry | None = None
        current: AssistantEntry | None = None
        for block in message.blocks:
            match block:
                case TextBlock(text=text):
                    current = current or await self._mount(AssistantEntry(final=True))
                    current.body += text
                    current.refresh_content()
                    last = current
                case Reasoning(text=text):
                    current = current or await self._mount(AssistantEntry(final=True))
                    current.reasoning += text
                    current.refresh_content()
                case ToolCall() as call:
                    await self._mount_tool(call)
                    current = None
                case ToolResult() as result:
                    await self._finish_tool(result, None)
                    current = None
        return last

    async def _finish_tool(self, result: ToolResult | OperationResult, duration: float | None
                           ) -> None:
        entry = self._tool_entries.get(result.call_id)
        if entry is None:
            entry = await self._mount(ToolEntry(result.call_id, None))
            self._tool_entries[result.call_id] = entry
        entry.finish(result, duration)

    async def _add_prompt(self, request: PermissionRequest) -> None:
        if request.id in self._prompts:
            return
        prompt = PermissionPrompt(request)
        self._prompts[request.id] = prompt
        await self.query_one("#permissions", Vertical).mount(prompt)

    def _render_status(self) -> None:
        state = STATE_LABELS[self.state]
        parts = [state + (f" · {self.pending_ops} ops" if self.pending_ops else "")]
        if self.config is not None:
            mode = self.config.mode.value
            if self.config.mode is Mode.CONTINUOUS:
                mode += f"/{self.config.activation.value.replace('_controlled', '')}"
            parts.append(mode)
        if self.model:
            parts.append(self.model)
        if self.usage is not None:
            u = self.usage.usage
            usage = f"{u.input_tokens}↑ {u.output_tokens}↓ tok"
            if u.cost_usd is not None:
                usage += f" ${u.cost_usd:.4f}"
            parts.append(usage)
            if self.usage.budget_remaining:
                parts.append(
                    "left: "
                    + ", ".join(f"{k} {v:g}" for k, v in self.usage.budget_remaining.items())
                )
        if self.notice:
            parts.append(self.notice)
        style = {
            AgentState.STOPPING: "bold yellow",
            AgentState.AWAITING_PERMISSION: "bold magenta",
            AgentState.FAILED: "bold red",
        }.get(self.state, "bold")
        line = Text.assemble((f"● {parts[0]}", style), *(f" │ {p}" for p in parts[1:]))
        self.query_one("#status", Static).update(line)

    # ── commands ─────────────────────────────────────────────────────────

    def _call(self, awaitable: Awaitable[Any]) -> None:
        """Run a runtime command without blocking the UI; report errors inline."""

        async def run() -> None:
            try:
                await awaitable
            except Exception as e:  # noqa: BLE001 - any runtime error is shown, never fatal
                self.notify(str(e) or type(e).__name__, severity="error", markup=False)

        self.run_worker(run(), group="commands")

    def _on_start_chosen(self, options: StartOptions | None) -> None:
        if options is not None:
            self._call(self._start(options))

    async def _start(self, options: StartOptions) -> None:
        self.conversation = await self.runtime.start(options)
        await self._inbox.put(RESYNC)

    @on(Input.Submitted, "#prompt")
    def _submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.clear()
        if not text:
            return
        if text.startswith("/"):
            self._command(text)
        else:
            self._call(self.runtime.send(text))

    def _command(self, text: str) -> None:
        name, _, arg = text[1:].partition(" ")
        arg = arg.strip()
        match name:
            case "stop":
                self.action_stop()
            case "activate":
                self.action_activate()
            case "model" if arg:
                self._call(self.runtime.select_model(arg))
            case "model":
                self.action_models()
            case "cancel" if arg:
                self._call(self.runtime.cancel_operation(arg))
            case "cancel":
                self.action_operations()
            case "allow" | "deny":
                decision = Decision.DENY if name == "deny" else DECISION_ALIASES.get(arg or "once")
                if decision is None:
                    self.notify(f"Unknown grant '{arg}'", severity="error", markup=False)
                elif not self._prompts:
                    self.notify("No pending permission request", severity="warning")
                else:
                    self.resolve_permission(next(iter(self._prompts)), decision)
            case "quit" | "exit":
                self.exit()
            case "help":
                self.notify(HELP, markup=False, timeout=10)
            case _:
                self.notify(f"Unknown command /{name}. {HELP}", severity="error", markup=False)

    def action_stop(self) -> None:
        self.notice = "stop requested"
        self._render_status()
        self._call(self.runtime.stop())

    def action_activate(self) -> None:
        self._call(self.runtime.activate())

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "activate":
            config = self.config
            agent_controlled = (config is not None and config.mode is Mode.CONTINUOUS
                                and config.activation is Activation.AGENT_CONTROLLED)
            return config is not None and not (agent_controlled
                                               and self.state is AgentState.IDLE)
        return True

    def action_operations(self) -> None:
        self.push_screen(OperationsScreen(self.runtime.snapshot().operations),
                         self._on_operation_chosen)

    def _on_operation_chosen(self, call_id: str | None) -> None:
        if call_id is not None:
            self._call(self.runtime.cancel_operation(call_id))

    def action_models(self) -> None:
        async def pick() -> None:
            models = await self.runtime.list_models()
            self.push_screen(ModelScreen(models, self.model), self._on_model_chosen)

        self._call(pick())

    def _on_model_chosen(self, model_id: str | None) -> None:
        if model_id is not None:
            self._call(self.runtime.select_model(model_id))

    def resolve_permission(self, request_id: str, decision: Decision) -> None:
        prompt = self._prompts.get(request_id)
        if prompt is not None:
            prompt.set_busy(True)

        async def resolve() -> None:
            try:
                await self.runtime.resolve_permission(request_id, decision)
            except Exception:
                if prompt is not None:
                    prompt.set_busy(False)
                raise

        self._call(resolve())


def factory(options: dict[str, Any], paths: InstancePaths) -> TuiFrontend:
    """Options: mode, activation, conversation_id, model. With a mode the agent starts
    right away; without one the start screen asks."""
    if "mode" not in options:
        return TuiFrontend()
    return TuiFrontend(StartOptions(
        mode=Mode(options["mode"]),
        activation=Activation(options.get("activation", Activation.USER_CONTROLLED)),
        conversation_id=options.get("conversation_id"),
        model=options.get("model"),
    ))


class TuiFrontend(Frontend):
    def __init__(self, start_options: StartOptions | None = None) -> None:
        self.start_options = start_options

    @property
    def info(self) -> FrontendInfo:
        return FrontendInfo(name="tui", interactive_permissions=True)

    async def run(self, runtime: RuntimeAPI) -> None:
        """Returns when the user quits or the runtime's event stream ends."""
        await KaydenApp(runtime, self.start_options).run_async()
