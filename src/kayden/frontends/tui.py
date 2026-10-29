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
    MaintenanceFinished,
    MaintenanceStarted,
    Message,
    MessageCommitted,
    MessageQueued,
    Mode,
    ModelInfo,
    ModelRetry,
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
    ToolStatus.CANCELLED: ("⊘", "yellow"),
    ToolStatus.TIMED_OUT: ("⏱", "yellow"),
    ToolStatus.INTERRUPTED: ("⚠", "yellow"),
}
DECISION_LABELS = {
    Decision.ALLOW_ONCE: "Allow once",
    Decision.ALLOW_CONVERSATION: "Allow for conversation",
    Decision.ALLOW_ALWAYS: "Always allow",
    Decision.DENY: "Deny",
}
DECISION_ALIASES = {
    "once": Decision.ALLOW_ONCE,
    "conversation": Decision.ALLOW_CONVERSATION,
    "conv": Decision.ALLOW_CONVERSATION,
    "always": Decision.ALLOW_ALWAYS,
}
HELP = (
    "Commands: /stop (Esc), /activate (Ctrl+R), /model [id] (Ctrl+O), "
    "/allow [once|conversation|always], /deny, /quit (Ctrl+Q). "
    "Messages can be sent while the agent runs; they are queued."
)
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
    def __init__(self, text: str, *, queued: bool = False) -> None:
        super().__init__()
        self.message_text = text
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

    def refresh_content(self) -> None:
        parts: list[RenderableType] = []
        if self.reasoning:
            parts.append(Text(self.reasoning.strip(), style="dim italic"))
        if self.body:
            parts.append(Markdown(self.body) if self.final else Text(self.body))
        if self.interrupted:
            parts.append(Text("(interrupted)", style="dim"))
        self.update(Group(*parts))


class ToolEntry(Static):
    def __init__(self, call_id: str, name: str | None, arguments: Any = "") -> None:
        super().__init__()
        self.call_id = call_id
        self.tool_name = name or "…"
        self.arguments = arguments
        self.phase = "requested"  # requested | running | background | done | not run
        self.progress: str | None = None
        self.op_id: str | None = None
        self.result: ToolResult | None = None
        self.duration: float | None = None
        self.notes: list[str] = []
        self.refresh_content()

    def stream_arguments(self, name: str | None, chunk: str) -> None:
        if name:
            self.tool_name = name
        self.arguments = (self.arguments if isinstance(self.arguments, str) else "") + chunk
        self.refresh_content()

    def set_phase(self, phase: str, *, op_id: str | None = None) -> None:
        self.phase = phase
        self.op_id = op_id or self.op_id
        self.refresh_content()

    def set_progress(self, text: str) -> None:
        self.progress = text
        self.refresh_content()

    def finish(self, result: ToolResult, duration: float | None = None) -> None:
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
            label = "running" if self.phase == "running" else f"background {self.op_id}"
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
        with Vertical(classes="dialog"):
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


RESYNC = _Resync()


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
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, runtime: RuntimeAPI, start_options: StartOptions | None = None) -> None:
        super().__init__()
        self.runtime = runtime
        self.start_options = start_options
        self.last_seq = -1
        self.state = AgentState.IDLE
        self.pending_ops = 0
        self.config: AgentConfig | None = None
        self.conversation: ConversationInfo | None = None
        self.model: str | None = None
        self.usage: UsageSnapshot | None = None
        self.notice = ""
        self._inbox: asyncio.Queue[EventEnvelope | _Resync | None] = asyncio.Queue()
        self._tool_entries: dict[str, ToolEntry] = {}
        self._stream: list[AssistantEntry | ToolEntry] = []
        self._queued: list[UserEntry] = []
        self._rendered_ids: set[str] = set()
        self._prompts: dict[str, PermissionPrompt] = {}

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
        history = await self.runtime.history(snap.conversation.id) if snap.conversation else ()
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
        self.model = snap.config.model
        self.usage = snap.usage
        for message in history:
            await self._render_message(message)
        for op in snap.operations:
            entry = self._tool_entries.get(op.call.id) or await self._mount_tool(op.call)
            entry.set_phase("background", op_id=op.op_id)
            if op.last_progress:
                entry.set_progress(op.last_progress)
        for request in snap.pending_permissions:
            await self._add_prompt(request)
        self._render_status()

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
            case MessageQueued(text=text):
                self._queued.append(await self._mount(UserEntry(text, queued=True)))
            case MessageCommitted(message=message):
                await self._commit(message)
            case ToolStarted(call=call):
                entry = self._tool_entries.get(call.id) or await self._mount_tool(call)
                entry.set_phase("running")
            case ToolProgress(call_id=call_id, text=text):
                if entry := self._tool_entries.get(call_id):
                    entry.set_progress(text)
            case ToolBackgrounded(call_id=call_id, op_id=op_id):
                if entry := self._tool_entries.get(call_id):
                    entry.set_phase("background", op_id=op_id)
            case ToolFinished(result=result, duration_s=duration):
                await self._finish_tool(result, duration)
            case PermissionRequested(request=request):
                await self._add_prompt(request)
            case PermissionResolved(request_id=request_id, decision=decision):
                prompt = self._prompts.pop(request_id, None)
                if prompt is not None:
                    await prompt.remove()
                    if entry := self._tool_entries.get(prompt.request.call.id):
                        entry.note(f"permission: {DECISION_LABELS[decision].lower()}")
            case ModelRetry(attempt=attempt, max_attempts=max_attempts, delay_s=delay, error=err):
                self.notice = f"model retry {attempt}/{max_attempts} in {delay:.0f}s: {err}"
                await self._discard_stream()  # the retried request streams from scratch
            case MaintenanceStarted():
                self.notice = "maintaining memory…"
            case MaintenanceFinished():
                self.notice = ""
            case StopStalled(op_ids=op_ids):
                self.notice = f"stop stalled on {', '.join(op_ids)}"
                await self._mount(
                    SystemEntry(
                        f"Stop is waiting on operations that have not terminated: "
                        f"{', '.join(op_ids)}",
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

    async def _commit(self, message: Message) -> None:
        if message.id in self._rendered_ids:
            return
        if message.role is Role.USER:
            text = "".join(b.text for b in message.blocks if isinstance(b, TextBlock))
            queued = next((e for e in self._queued if e.message_text == text), None)
            if queued is not None:
                self._queued.remove(queued)
                queued.mark_committed()
                self._rendered_ids.add(message.id)
                return
        elif message.role is Role.ASSISTANT:
            await self._discard_stream()  # replaced by the authoritative message
        await self._render_message(message)

    async def _render_message(self, message: Message) -> None:
        self._rendered_ids.add(message.id)
        if message.role is Role.USER:
            text = "".join(b.text for b in message.blocks if isinstance(b, TextBlock))
            await self._mount(UserEntry(text))
            return
        current: AssistantEntry | None = None
        for block in message.blocks:
            match block:
                case TextBlock(text=text):
                    current = current or await self._mount(AssistantEntry(final=True))
                    current.body += text
                    current.refresh_content()
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

    async def _finish_tool(self, result: ToolResult, duration: float | None) -> None:
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
                self._call(self._select_model(arg))
            case "model":
                self.action_models()
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
            return self.config is not None and self.config.mode is Mode.CONTINUOUS
        return True

    def action_models(self) -> None:
        async def pick() -> None:
            models = await self.runtime.list_models()
            self.push_screen(ModelScreen(models, self.model), self._on_model_chosen)

        self._call(pick())

    def _on_model_chosen(self, model_id: str | None) -> None:
        if model_id is not None:
            self._call(self._select_model(model_id))

    async def _select_model(self, model_id: str) -> None:
        await self.runtime.select_model(model_id)
        self.model = model_id
        self._render_status()

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


class TuiFrontend(Frontend):
    def __init__(self, start_options: StartOptions | None = None) -> None:
        self.start_options = start_options

    @property
    def info(self) -> FrontendInfo:
        return FrontendInfo(name="tui", interactive_permissions=True)

    async def run(self, runtime: RuntimeAPI) -> None:
        await KaydenApp(runtime, self.start_options).run_async()
