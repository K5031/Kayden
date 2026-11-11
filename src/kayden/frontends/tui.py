"""Terminal Frontend built on Textual.

All rendering happens in one consumer task that applies runtime events in sequence
order; runtime commands run as separate workers, so a slow or hanging runtime call
(Stop included) never freezes the interface.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import (
    Button,
    Checkbox,
    Collapsible,
    Footer,
    Input,
    Label,
    OptionList,
    Select,
    Static,
    Switch,
    TextArea,
)
from textual.widgets.option_list import Option

from kayden.api import (
    PROFILE,
    AgentConfig,
    AgentState,
    CompactionFinished,
    CompactionStarted,
    ConversationDeleted,
    ConversationId,
    ConversationInfo,
    ConversationUpdated,
    Decision,
    Duty,
    DutyKind,
    DutyScheduled,
    EventEnvelope,
    Failure,
    Frontend,
    FrontendInfo,
    HelpNeeded,
    InstancePaths,
    MaintenanceFinished,
    MaintenanceStarted,
    McpServer,
    MemoriesChanged,
    MemoryNote,
    Message,
    MessageCommitted,
    MessageQueued,
    ModelInfo,
    ModelRetry,
    ModelSelected,
    OperationInfo,
    OperationResult,
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
    ServerToolUse,
    Settings,
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
    Tools,
    ToolStarted,
    ToolStatus,
    UsageSnapshot,
    UsageUpdated,
)
from kayden.api import Text as TextBlock
from kayden.frontends.common import (
    AT_RESTART,
    COMPACTED,
    COMPACTING,
    DUTY_HINT,
    DUTY_KIND_LABELS,
    MCP_VARIABLES_HINT,
    MCP_WARNING,
    MODULE_LABELS,
    PERMISSION_LABELS,
    PLAN_HINT,
    RESULT_LABELS,
    Card,
    call_parts,
    card_of,
    delete_details,
    duty_status,
    folder_path,
    group_title,
    help_text,
    is_blank,
    loosening,
    mcp_server,
    mcp_target,
    mcp_variables,
    model_label,
    module_options,
    needs_restart,
    notice_of,
    parse_plan_text,
    past_tense,
    plan_text,
    proposal_parts,
    provider_step,
    request_note,
    restart_notice,
)
from kayden.frontends.remote import RemoteRuntime
from kayden.frontends.sessions import SubSession, SubSessions, working_text

STATE_LABELS = {
    AgentState.NOT_STARTED: "Not started",
    AgentState.IDLE: "Ready",
    AgentState.THINKING: "Thinking…",
    AgentState.RUNNING_TOOLS: "Working…",
    AgentState.AWAITING_PERMISSION: "Waiting for you",
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
    Decision.ALLOW_CONVERSATION: "Allow for this conversation",
    Decision.DENY: "Deny",
}
DECISION_ALIASES = {
    "once": Decision.ALLOW_ONCE,
    "conversation": Decision.ALLOW_CONVERSATION,
    "conv": Decision.ALLOW_CONVERSATION,
}
HELP = (
    "Commands: /stop (Esc), /activate (Ctrl+R), /model [id] (Ctrl+O), /settings (F2), "
    "/conversations (F3), /memory (F4), /mcp (tool servers), /folder [path], "
    "/duty [instruction|off] (F5), /goal [instruction], /sessions, /back, "
    "/approve, /dismiss (a proposal), "
    "/cancel [call id] (Ctrl+K), /allow [once|conversation], /deny, /quit (Ctrl+Q). "
    "Messages can be sent while the agent runs; they are queued."
)
UNTITLED = "New chat"
# States in which runtime.start() may switch conversations (given no outstanding call).
SWITCHABLE = (AgentState.NOT_STARTED, AgentState.IDLE, AgentState.STOPPED, AgentState.FAILED)
HISTORY_PAGE = 100  # messages loaded per history page
UNDERWAY = {"requested": "waiting", "running": "running", "background": "in the background"}
MAX_ARGS_CHARS = 300
MAX_RESULT_LINES = 6
MAX_RESULT_CHARS = 600


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _call_label(name: str, arguments: Any) -> str:
    """One line for a call: its description, else its name and arguments."""
    headline, detail = _call_parts(arguments)
    return headline or f"{name} {detail}"


def _call_parts(arguments: Any) -> tuple[str | None, str]:
    """call_parts for arguments that may still be streaming JSON text."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return None, arguments
    return call_parts(arguments)


def conversation_label(info: ConversationInfo) -> str:
    """The title, else the start of the first user message."""
    return info.title or (_clip(" ".join(info.preview.split()), 60) if info.preview
                          else UNTITLED)


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
            parts.append(Text("The model declined to answer.", style="bold red"))
        self.update(Group(*parts))
        self.display = bool(parts)  # thinking with no readable text, before a tool call


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

    @property
    def headline(self) -> str:
        """The model's sentence for the step (past tense once it went fine), else the call."""
        headline, detail = _call_parts(self.arguments)
        if headline is None:
            return _clip(f"{self.tool_name} {detail}", MAX_ARGS_CHARS)
        ok = self.result is not None and self.result.status is ToolStatus.OK
        return _clip(past_tense(headline) if ok else headline, MAX_ARGS_CHARS)

    @property
    def problem(self) -> str:
        """How it ended, when worth pointing out: '' when it went fine or has not ended."""
        if self.phase == "not run":
            return "not run"
        if self.result is None or self.result.status is ToolStatus.OK:
            return ""
        return RESULT_LABELS.get(self.result.status, self.result.status.value)

    def refresh_content(self) -> None:
        if isinstance(group := self.parent and self.parent.parent, StepGroup):
            group.refresh_title()
        headline, detail = _call_parts(self.arguments)
        if headline and self.result is not None and self.result.status is ToolStatus.OK:
            headline = past_tense(headline)
        if headline:  # the model's own words say what the step is
            out = Text.assemble(("⚙ ", "cyan"), (_clip(headline, MAX_ARGS_CHARS), "bold"))
            out.append(f"\n  {_clip(detail, MAX_ARGS_CHARS)}", style="dim")
        else:
            out = Text.assemble(("⚙ ", "cyan"), (self.tool_name, "bold cyan"), " ")
            out.append(_clip(detail, MAX_ARGS_CHARS), style="dim")
        if self.phase in ("running", "background"):
            label = self.phase
            out.append(f"\n  ⋯ {label}", style="yellow")
            if self.progress:
                out.append(f": {_clip(self.progress, 200)}", style="dim")
        elif self.phase == "not run":
            out.append("\n  (not run)", style="dim")
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


class StepGroup(Collapsible):
    """A run of steps, and the thinking before and between them, folded into one line: the
    step under way while it runs, then how many there were and what went wrong."""

    def __init__(self) -> None:
        super().__init__(title="", collapsed=True)

    async def add(self, widget: Widget) -> None:
        await self.query_one(Collapsible.Contents).mount(widget)
        self.refresh_title()

    def refresh_title(self) -> None:
        steps = list(self.query(ToolEntry))
        if not steps:
            return
        title, state, _ = group_title([(s.headline, s.problem, UNDERWAY.get(s.phase))
                                       for s in steps])
        self.title = Content(f"{title} · {state}" if state else title)  # type: ignore[assignment]


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
        headline, detail = _call_parts(call.arguments)
        yield Label(
            Text.assemble(
                *((("Kayden wants to: ", "bold"), (headline, "bold cyan")) if headline else
                  (("Kayden wants to use ", "bold"), (call.name, "bold cyan"))),
                f"\n{request_note(self.request)}\n",
                (_clip(detail, MAX_ARGS_CHARS), "dim"),
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


class ProposalPrompt(Vertical):
    """A call the agent proposed: it runs only if the user approves it."""

    def __init__(self, proposal: Proposal) -> None:
        super().__init__()
        self.proposal = proposal

    def compose(self) -> ComposeResult:
        headline, detail = proposal_parts(self.proposal)
        yield Label(Text.assemble((headline, "bold"), f"\n{self.proposal.reason}",
                                  (f"\n{_clip(detail, MAX_ARGS_CHARS)}" if detail else "",
                                   "dim")))
        with Horizontal(classes="buttons"):
            yield Button("Approve", id="approve", variant="primary")
            yield Button("Dismiss", id="dismiss")

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        event.stop()
        for button in self.query(Button):
            button.disabled = True
        self.app.resolve_proposal(self.proposal.id, event.button.id == "approve")  # type: ignore[attr-defined]


class WrapUpPrompt(Vertical):
    """The card that ends the agent's turn: its recap, its question and the options, its pick
    first. Pressing an option sends it as the user's message, or runs its call while that is
    pending; ✕ puts the card away."""

    def __init__(self, card: Card) -> None:
        super().__init__()
        self.card = card

    def compose(self) -> ComposeResult:
        card = self.card
        yield Label(Text.assemble((card.recap, "dim"), "\n" if card.recap and card.question
                                  else "", (card.question, "bold")))
        with Horizontal(classes="buttons"):
            for i, option in enumerate(card.options):
                yield Button(option, variant="primary" if i == 0 else "default")
            yield Button("✕", id="close", tooltip="Close")

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "close":
            self.app.close_card()  # type: ignore[attr-defined]
        else:
            self.app.choose(str(event.button.label))  # type: ignore[attr-defined]


# ── modal screens ────────────────────────────────────────────────────────


class OperationsScreen(ModalScreen[str | None]):
    """Picks a running call to cancel."""

    BINDINGS: ClassVar = [Binding("escape", "dismiss(None)", "Close")]

    def __init__(self, operations: Sequence[OperationInfo]) -> None:
        super().__init__()
        self.operations = operations

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Cancel a running task" if self.operations else "Nothing is running")
            yield OptionList(
                *(Option(_clip(_call_label(op.call.name, op.call.arguments), 70)
                         + f"  [{op.call.id}]"
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
            yield Label("Choose a model (applies from the next reply)")
            options = [
                Option(f"{'● ' if m.id == self.current else '  '}{m.name}  [{m.id}]", id=m.id)
                for m in self.models
            ]
            yield OptionList(*options, id="models")

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)


SAME_MODEL = "__same__"


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS: ClassVar = [Binding("escape", "dismiss(False)", "No")]

    def __init__(self, message: str, *, yes: str = "Yes", no: str = "No",
                 danger: bool = False) -> None:
        super().__init__()
        self.message, self.yes, self.no, self.danger = message, yes, no, danger

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.message, id="confirm-message")
            with Horizontal(classes="buttons"):
                yield Button(self.no, id="confirm-no")
                yield Button(self.yes, variant="error" if self.danger else "warning",
                             id="confirm-yes")

    def on_mount(self) -> None:
        self.query_one("#confirm-no", Button).focus()

    @on(Button.Pressed, "#confirm-yes")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#confirm-no")
    def _no(self) -> None:
        self.dismiss(False)


class SettingsScreen(ModalScreen[tuple[SettingsUpdate, bool] | None]):
    """Edits the user settings; returns only what changed, and whether to restart now.
    Loosening permissions or raising the spending limit is confirmed first (api.py
    update_settings), and so is a different Memory."""

    BINDINGS: ClassVar = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, settings: Settings, models: Sequence[ModelInfo], spent: float) -> None:
        super().__init__()
        self.settings = settings
        self.models = models
        self.spent = spent

    def compose(self) -> ComposeResult:
        s = self.settings
        models = [(model_label(m), m.id) for m in self.models]
        with VerticalScroll(classes="dialog"):
            yield Label("Settings", classes="title")
            if s.startup_error is not None:
                yield Label(f"Kayden could not start: {s.startup_error}", id="startup-error",
                            classes="error", markup=False)
            yield Label("AI provider")
            yield self._module_select("core")
            yield Label("API key for this provider")
            yield Input(password=True, placeholder="Leave empty to keep the current key",
                        id="api-key")
            yield Label("Model")
            yield Select(models, value=s.model if s.model in dict(map(reversed, models))
                         else Select.NULL, allow_blank=True, prompt="Provider's default",
                         id="model", disabled=not models)
            yield Label("After the restart, the new provider starts with its first model.",
                        id="model-note")
            yield Label("Model for memory upkeep")
            yield Select([("Same as the main model", SAME_MODEL), *models],
                         value=s.maintenance_model or SAME_MODEL, allow_blank=False,
                         id="maintenance-model", disabled=not models)
            yield Label("Spending limit in US dollars (leave empty for no limit)")
            limit = s.budget.max_cost_usd
            yield Input("" if limit is None else f"{limit:g}", id="spending-limit")
            yield Static(f"${self.spent:.2f} spent so far", id="spent")
            yield Label("What the agent may do")
            yield Select([(label, policy) for policy, label in PERMISSION_LABELS.items()],
                         value=s.permissions, allow_blank=False, id="permissions")
            yield Label("Instructions")
            yield TextArea(s.system_prompt, id="instructions")
            yield Label("Folder for new chats")
            yield Input(s.work_folder or "", disabled=s.work_folder is None, id="work-folder")
            for kind in ("tools", "frontend"):
                yield Label(MODULE_LABELS[kind])
                yield self._module_select(kind)
            yield Label(AT_RESTART, classes="hint")
            yield Label(restart_notice(s) or "", id="restart-notice", classes="hint")
            with Horizontal(classes="buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Restart now", id="restart")
                yield Button("Cancel", id="cancel")

    def _module_select(self, kind: str) -> Select[str]:
        choice = self.settings.modules[kind]
        return Select(module_options(choice),
                      value=choice.chosen if choice.chosen is not None else Select.NULL,
                      allow_blank=choice.chosen is None, prompt="Custom (from config.toml)",
                      id=f"module-{kind}")

    def on_mount(self) -> None:
        self.query_one("#model-note").display = False
        s = self.settings
        self.query_one("#restart-notice").display = restart_notice(s) is not None
        self.query_one("#restart").display = s.restart_needed or s.startup_error is not None

    @on(Select.Changed, "#module-core")
    def _core_changed(self, event: Select.Changed) -> None:
        switching = event.value != self.settings.modules["core"].running
        for selector in ("#model", "#maintenance-model"):
            self.query_one(selector, Select).disabled = switching or not self.models
        self.query_one("#model-note").display = switching

    @on(Select.Changed, "#module-frontend")
    def _frontend_changed(self, event: Select.Changed) -> None:
        self.query_one("#restart").display = True

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        self._finish(restart=False)

    @on(Button.Pressed, "#restart")
    def _restart(self) -> None:
        self._finish(restart=True)

    def _finish(self, *, restart: bool) -> None:
        update = self._update()
        if update is None:
            return
        reasons = loosening(self.settings, update)
        if not reasons:
            self.dismiss((update, restart))
            return
        message = "This change " + " and ".join(reasons) + ". "

        def confirmed(yes: bool | None) -> None:
            if yes:
                self.dismiss((update, restart))

        self.app.push_screen(ConfirmScreen("Save these changes? " + message.strip(),
                                           yes="Save", no="Cancel"), confirmed)

    def _update(self) -> SettingsUpdate | None:
        """The changed fields, or None (with a message) if a value is invalid."""
        s = self.settings
        changes: dict[str, Any] = {}
        modules = {}
        for kind, choice in s.modules.items():
            if kind not in MODULE_LABELS:
                continue
            value = self.query_one(f"#module-{kind}", Select).value
            if value is not Select.NULL and value != choice.chosen:
                modules[kind] = value
        if modules:
            changes["modules"] = modules
        if key := self.query_one("#api-key", Input).value.strip():
            changes["api_key"] = key
        if not self.query_one("#model", Select).disabled:
            model = self.query_one("#model", Select).value
            if model is not Select.NULL and model != s.model:
                changes["model"] = model
            light = self.query_one("#maintenance-model", Select).value
            light = None if light == SAME_MODEL else light
            if light != s.maintenance_model:
                changes["maintenance_model"] = light or ""
        text = self.query_one("#spending-limit", Input).value.strip()
        try:
            limit = float(text.lstrip("$")) if text else None
            if limit is not None and limit < 0:
                raise ValueError
        except ValueError:
            self.notify("The spending limit must be an amount in dollars, like 10 or 2.50.",
                        severity="error")
            return None
        if limit != s.budget.max_cost_usd:
            changes["budget"] = replace(s.budget, max_cost_usd=limit)
        permissions = self.query_one("#permissions", Select).value
        if permissions != s.permissions:
            changes["permissions"] = permissions
        prompt = self.query_one("#instructions", TextArea).text
        if prompt != s.system_prompt:
            if not prompt.strip():
                self.notify("The instructions can't be empty.", severity="error")
                return None
            changes["system_prompt"] = prompt
        folder = self.query_one("#work-folder", Input)
        if not folder.disabled and folder.value.strip() != (s.work_folder or ""):
            changes["work_folder"] = folder.value.strip()
        return SettingsUpdate(**changes)


class RestartScreen(ModalScreen[bool]):
    """Offers Restart now after module settings were saved."""

    BINDINGS: ClassVar = [Binding("escape", "dismiss(False)", "Later")]

    def __init__(self, notice: str) -> None:
        super().__init__()
        self.notice = notice

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.notice, id="restart-message")
            with Horizontal(classes="buttons"):
                yield Button("Restart now", variant="primary", id="restart-now")
                yield Button("Later", id="restart-later")

    def on_mount(self) -> None:
        self.query_one("#restart-now", Button).focus()

    @on(Button.Pressed, "#restart-now")
    def _now(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#restart-later")
    def _later(self) -> None:
        self.dismiss(False)


class TextPromptScreen(ModalScreen[str | None]):
    """Asks for one line of text; Enter saves, Escape cancels."""

    BINDINGS: ClassVar = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, title: str, value: str = "", hint: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.value = value
        self.hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.title_text, classes="title")
            yield Input(self.value, id="text-value")
            if self.hint:
                yield Label(self.hint, classes="hint")

    @on(Input.Submitted, "#text-value")
    def _submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value.strip())


class ListScreen(ModalScreen[str | None]):
    """A list the user changes in place; reload() re-reads it from the runtime."""

    BINDINGS: ClassVar = [Binding("escape", "dismiss(None)", "Close")]

    def __init__(self, runtime: RuntimeAPI) -> None:
        super().__init__()
        self.runtime = runtime

    def on_mount(self) -> None:
        self.reload()

    def reload(self) -> None:
        self.run_worker(self._load(), group="load", exclusive=True)

    async def _load(self) -> None:
        raise NotImplementedError

    def _show(self, options: list[Option]) -> None:
        """Replace the options, keeping the highlighted item when it is still listed."""
        option_list = self.query_one(OptionList)
        current = option_list.highlighted_option
        option_list.clear_options()
        option_list.add_options(options)
        ids = [o.id for o in options]
        if options:
            option_list.highlighted = (ids.index(current.id)
                                       if current is not None and current.id in ids else 0)

    def _selected_id(self) -> str | None:
        option = self.query_one(OptionList).highlighted_option
        return None if option is None else option.id

    def _run(self, awaitable: Awaitable[Any], *, unsupported: str | None = None) -> None:
        """Run a runtime command, report errors, then reload. `unsupported` names a button
        to disable when the Memory module does not support the command."""

        async def run() -> None:
            try:
                await awaitable
            except NotImplementedError as e:
                if unsupported is not None:
                    self.query_one(unsupported, Button).disabled = True
                self.notify(str(e) or "This memory module does not support that.",
                            severity="error", markup=False)
            except Exception as e:  # noqa: BLE001 - any runtime error is shown, never fatal
                self.notify(str(e) or type(e).__name__, severity="error", markup=False)
            self.reload()

        self.run_worker(run(), group="commands")


class ConversationsScreen(ListScreen):
    """Lists conversations; renames, archives and deletes them in place. Dismisses with
    the id of the conversation to open, NEW_CONVERSATION, or None."""

    def __init__(self, runtime: RuntimeAPI, open_id: ConversationId | None) -> None:
        super().__init__(runtime)
        self.open_id = open_id
        self.conversations: dict[str, ConversationInfo] | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Conversations", classes="title")
            with Horizontal(classes="switch-row"):
                yield Switch(False, id="show-archived")
                yield Label("Show archived")
            yield OptionList(id="conversations")
            with Horizontal(classes="buttons actions"):
                yield Button("Open", variant="primary", id="open")
                yield Button("New", id="new")
                yield Button("Rename", id="rename")
                yield Button("Archive", id="archive")
                yield Button("Delete", variant="error", id="delete")

    async def _load(self) -> None:
        archived = self.query_one("#show-archived", Switch).value
        try:
            listed = await self.runtime.list_conversations(include_archived=archived)
        except Exception as e:  # noqa: BLE001
            self.notify(str(e) or type(e).__name__, severity="error", markup=False)
            return
        self._show([
            Option(("● " if c.id == self.open_id else "  ") + conversation_label(c)
                   + ("  · archived" if c.archived else ""), id=c.id)
            for c in listed if not is_blank(c)
        ])
        self.conversations = {c.id: c for c in listed}
        self._sync_archive_label()

    def _selected(self) -> ConversationInfo | None:
        conversation_id = self._selected_id()
        if conversation_id is None or self.conversations is None:
            return None
        return self.conversations.get(conversation_id)

    def _sync_archive_label(self) -> None:
        selected = self._selected()
        archived = selected is not None and selected.archived
        self.query_one("#archive", Button).label = "Unarchive" if archived else "Archive"

    @on(Switch.Changed, "#show-archived")
    def _show_archived(self) -> None:
        self.reload()

    @on(OptionList.OptionHighlighted)
    def _highlighted(self) -> None:
        self._sync_archive_label()

    @on(OptionList.OptionSelected)
    @on(Button.Pressed, "#open")
    def _open(self) -> None:
        if (selected := self._selected()) is not None:
            self.dismiss(selected.id)

    @on(Button.Pressed, "#new")
    def _new(self) -> None:
        self.dismiss(NEW_CONVERSATION)

    @on(Button.Pressed, "#rename")
    def _rename(self) -> None:
        if (selected := self._selected()) is None:
            return

        def renamed(title: str | None) -> None:
            if title is not None:
                self._run(self.runtime.rename_conversation(selected.id, title))

        self.app.push_screen(TextPromptScreen("Rename conversation", selected.title or "",
                                              "Leave empty for an automatic title."), renamed)

    @on(Button.Pressed, "#archive")
    def _archive(self) -> None:
        if (selected := self._selected()) is not None:
            self._run(self.runtime.archive_conversation(selected.id, not selected.archived))

    @on(Button.Pressed, "#delete")
    def _delete(self) -> None:
        if (selected := self._selected()) is None:
            return

        def confirmed(yes: bool | None) -> None:
            if yes:
                self._run(self._erase(selected.id), unsupported="#delete")

        sessions = getattr(self.app, "sessions", None)
        children = len(sessions.of(selected.id, include_archived=True)) if sessions else 0
        self.app.push_screen(ConfirmScreen(
            f'Delete "{conversation_label(selected)}"? '
            + delete_details(selected.id == self.open_id, children),
            yes="Delete", no="Cancel", danger=True), confirmed)

    async def _erase(self, conversation_id: ConversationId) -> None:
        """The open conversation is first replaced by a new chat."""
        if conversation_id == self.open_id:
            assert isinstance(self.app, KaydenApp)
            await self.app.start_conversation(StartOptions(), discard_blank=False)
            self.open_id = self.app.conversation.id
        await self.runtime.delete_conversation(conversation_id)
        if (sessions := getattr(self.app, "sessions", None)) is not None:
            await sessions.delete_of(conversation_id)


class MemoryScreen(ListScreen):
    """Lists long-term memory notes, the profile first; adds, edits and deletes them."""

    def __init__(self, runtime: RuntimeAPI) -> None:
        super().__init__(runtime)
        self.entries: dict[str, MemoryNote] = {}

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Long-term memory", classes="title")
            yield Label(f'Every conversation sees "{PROFILE}" and the list of notes; Kayden '
                        "opens a note when it is relevant.", classes="hint")
            yield OptionList(id="memories")
            with Horizontal(classes="buttons actions"):
                yield Button("Add", variant="primary", id="add")
                yield Button("Edit", id="edit")
                yield Button("Delete", variant="error", id="forget")

    async def _load(self) -> None:
        try:
            notes = await self.runtime.notes()
        except Exception as e:  # noqa: BLE001
            self.notify(str(e) or type(e).__name__, severity="error", markup=False)
            return
        self.entries = {n.id: n for n in notes}
        self._show([Option(f"{n.title}  · {n.description}" if n.description else n.title,
                           id=n.id) for n in notes])

    def _selected(self) -> MemoryNote | None:
        note_id = self._selected_id()
        return None if note_id is None else self.entries.get(note_id)

    @on(Button.Pressed, "#add")
    def _add(self) -> None:
        def added(note: tuple[str, str, str] | None) -> None:
            if note is not None:
                self._run(self.runtime.save_note(*note))

        self.app.push_screen(NoteScreen(None), added)

    @on(OptionList.OptionSelected)
    @on(Button.Pressed, "#edit")
    def _edit(self) -> None:
        if (selected := self._selected()) is None:
            return

        def edited(note: tuple[str, str, str] | None) -> None:
            if note is not None:
                self._run(self.runtime.save_note(*note, note_id=selected.id))

        self.app.push_screen(NoteScreen(selected), edited)

    @on(Button.Pressed, "#forget")
    def _forget(self) -> None:
        if (selected := self._selected()) is None:
            return
        if selected.title == PROFILE:
            self.notify(f'"{PROFILE}" can be emptied but not deleted.', markup=False)
            return

        def confirmed(yes: bool | None) -> None:
            if yes:
                self._run(self.runtime.delete_note(selected.id))

        self.app.push_screen(ConfirmScreen(
            f'Delete the note "{_clip(selected.title, 200)}"? Kayden won\'t use it again. The '
            "messages it came from stay until you delete their conversation.",
            yes="Delete", no="Cancel", danger=True), confirmed)


class NoteScreen(ModalScreen[tuple[str, str, str] | None]):
    """Add or edit one note: (title, description, text); the profile keeps its title."""

    BINDINGS: ClassVar = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, note: MemoryNote | None) -> None:
        super().__init__()
        self.note = note

    def compose(self) -> ComposeResult:
        note = self.note
        with Vertical(classes="dialog"):
            yield Label("Edit note" if note else "Add a note", classes="title")
            yield Label("Title")
            yield Input(note.title if note else "", placeholder="A project, person or topic",
                        id="note-title", disabled=note is not None and note.title == PROFILE)
            yield Label("About")
            yield Input(note.description if note else "",
                        placeholder="One line: what the note is about", id="note-description")
            yield Label("Note")
            yield TextArea(note.text if note else "", id="note-text")
            with Horizontal(classes="buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        self.dismiss((self.query_one("#note-title", Input).value,
                      self.query_one("#note-description", Input).value,
                      self.query_one("#note-text", TextArea).text))

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


class McpScreen(ListScreen):
    """The MCP servers whose tools Kayden offers; changes start with the next start."""

    def __init__(self, runtime: RuntimeAPI) -> None:
        super().__init__(runtime)
        self.servers: list[McpServer] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Tool servers (MCP)", classes="title")
            yield Label("Each server gives Kayden more tools, such as GitHub, a database or "
                        "your calendar.", classes="hint")
            yield OptionList(id="servers")
            yield Label("", id="mcp-notice", classes="hint")
            with Horizontal(classes="buttons actions"):
                yield Button("Add", variant="primary", id="add")
                yield Button("Edit", id="edit")
                yield Button("Remove", variant="error", id="remove")
                yield Button("Restart now", id="restart")

    async def _load(self) -> None:
        settings = await self.runtime.settings()
        self.servers = list(settings.mcp_servers)
        self._show([Option(f"{s.name}  · {mcp_target(s)}", id=s.name) for s in self.servers])
        notice = restart_notice(settings) if settings.restart_needed else None
        self.query_one("#mcp-notice", Label).update(notice or "")
        self.query_one("#restart", Button).display = settings.restart_needed

    def _selected(self) -> McpServer | None:
        name = self._selected_id()
        return next((s for s in self.servers if s.name == name), None)

    def _save(self, servers: list[McpServer]) -> None:
        self._run(self.runtime.update_settings(SettingsUpdate(mcp_servers=servers)))

    @on(Button.Pressed, "#add")
    def _add(self) -> None:
        def added(server: McpServer | None) -> None:
            if server is not None:
                self._save([*self.servers, server])

        self.app.push_screen(ServerScreen(None), added)

    @on(OptionList.OptionSelected)
    @on(Button.Pressed, "#edit")
    def _edit(self) -> None:
        if (selected := self._selected()) is None:
            return

        def edited(server: McpServer | None) -> None:
            if server is not None:
                self._save([server if s.name == selected.name else s for s in self.servers])

        self.app.push_screen(ServerScreen(selected), edited)

    @on(Button.Pressed, "#remove")
    def _remove(self) -> None:
        if (selected := self._selected()) is None:
            return

        def confirmed(yes: bool | None) -> None:
            if yes:
                self._save([s for s in self.servers if s.name != selected.name])

        self.app.push_screen(ConfirmScreen(
            f'Remove the tool server "{selected.name}"? Its tools are gone after the next '
            "restart, and the keys saved for it are deleted.",
            yes="Remove", no="Cancel", danger=True), confirmed)

    @on(Button.Pressed, "#restart")
    def _restart(self) -> None:
        self._run(self.runtime.restart())


class ServerScreen(ModalScreen[McpServer | None]):
    """Add or edit one MCP server. The runtime checks it when the list is saved."""

    BINDINGS: ClassVar = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, server: McpServer | None) -> None:
        super().__init__()
        self.server = server

    def compose(self) -> ComposeResult:
        server = self.server
        with Vertical(classes="dialog"):
            yield Label("Edit tool server" if server else "Add tool server", classes="title")
            yield Label("Name")
            yield Input(server.name if server else "", placeholder="github", id="server-name")
            yield Label("Command or URL")
            yield Input(mcp_target(server) if server else "",
                        placeholder="npx -y @modelcontextprotocol/server-github, or a URL",
                        id="server-target")
            yield Label("Variables")
            yield TextArea(mcp_variables(server) if server else "", id="server-variables")
            yield Label(MCP_VARIABLES_HINT, classes="hint")
            yield Label(MCP_WARNING, id="warning", classes="hint")
            with Horizontal(classes="buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        try:
            server = mcp_server(self.query_one("#server-name", Input).value,
                                self.query_one("#server-target", Input).value,
                                self.query_one("#server-variables", TextArea).text)
        except ValueError as e:
            self.notify(str(e), severity="error", markup=False)
            return
        self.dismiss(server)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


NEW_CONVERSATION = "__new__"
FRESH = "__fresh__"
DEFAULT_MODEL = "__default__"


type DutyChoice = tuple[Duty | None, list[PlanItem]]


class DutyScreen(ModalScreen[DutyChoice | None]):
    """The open conversation's duty and plan: (duty or None to end it, plan)."""

    BINDINGS: ClassVar = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, info: ConversationInfo) -> None:
        super().__init__()
        self.info = info

    def compose(self) -> ComposeResult:
        duty = self.info.duty or Duty("")
        with VerticalScroll(classes="dialog"):
            yield Label("Duty", classes="title")
            yield Static(DUTY_HINT, classes="hint")
            yield Label("What should Kayden keep doing?")
            yield Input(duty.instruction, placeholder="e.g. Keep my inbox answered",
                        id="duty-instruction")
            yield Select([(label, kind) for kind, label in DUTY_KIND_LABELS.items()],
                         value=duty.kind, allow_blank=False, id="duty-kind")
            yield Label("Check in at least every … minutes")
            yield Input(f"{duty.max_interval_s / 60:g}", id="duty-max")
            yield Label("and at most every … minutes")
            yield Input(f"{duty.min_interval_s / 60:g}", id="duty-min")
            yield Label("Plan")
            yield Static(PLAN_HINT, classes="hint")
            yield TextArea(plan_text(self.info.plan), id="duty-plan")
            yield Label("", id="duty-error", classes="error")
            with Horizontal(classes="buttons"):
                yield Button("Save" if self.info.duty else "Turn on", variant="primary",
                             id="save")
                if self.info.duty is not None:
                    yield Button("Turn off", id="turn-off")
                yield Button("Cancel", id="cancel")

    def _plan(self) -> list[PlanItem] | None:
        try:
            return parse_plan_text(self.query_one("#duty-plan", TextArea).text)
        except ValueError as e:
            self.query_one("#duty-error", Label).update(str(e))
            return None

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        if (plan := self._plan()) is None:
            return
        instruction = self.query_one("#duty-instruction", Input).value.strip()
        if not instruction:
            self.dismiss((None, plan))
            return
        try:
            low, high = (60 * float(self.query_one(f"#duty-{n}", Input).value)
                         for n in ("min", "max"))
        except ValueError:
            self.query_one("#duty-error", Label).update("Write the minutes as numbers.")
            return
        kind = self.query_one("#duty-kind", Select).value
        self.dismiss((Duty(instruction, kind, low, high), plan))

    @on(Button.Pressed, "#turn-off")
    def _turn_off(self) -> None:
        if (plan := self._plan()) is not None:
            self.dismiss((None, plan))

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


class SessionsScreen(ModalScreen[str | None]):
    """The open conversation's sub-sessions. Returns the one to open in place."""

    BINDINGS: ClassVar = [Binding("escape", "dismiss(None)", "Close")]

    def __init__(self, sessions: SubSessions, conversation_id: ConversationId | None) -> None:
        super().__init__()
        self.sessions, self.conversation_id = sessions, conversation_id

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Sub-sessions", classes="title")
            yield OptionList(id="sessions")
            yield Static("", id="session-empty", classes="hint")
            yield Checkbox("Show archived", id="show-archived")
            with Horizontal(classes="buttons"):
                yield Button("Open", id="open-session", variant="primary")
                yield Button("Resume", id="resume-session")
                yield Button("Stop", id="stop-session", variant="error")
            with Horizontal(classes="buttons"):
                yield Button("Limit", id="limit-session")
                yield Button("Rename", id="rename-session")
                yield Button("Archive", id="archive-session")
                yield Button("Delete", id="delete-session", variant="error")
            with Horizontal(classes="buttons"):
                yield Button("", id="default-limit")
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        self.sessions.listeners.append(self.refresh_view)
        self.refresh_view()

    def on_unmount(self) -> None:
        with contextlib.suppress(ValueError):
            self.sessions.listeners.remove(self.refresh_view)

    def _selected(self) -> SubSession | None:
        option = self.query_one(OptionList).highlighted_option
        return next((s for s in self.sessions.sessions if option and s.id == option.id), None)

    def refresh_view(self) -> None:
        if not self.is_attached:  # told of a change while closing
            return
        archived = self.query_one("#show-archived", Checkbox).value
        listed = self.sessions.of(self.conversation_id, include_archived=archived)
        option_list = self.query_one(OptionList)
        current = option_list.highlighted
        option_list.clear_options()
        option_list.add_options([
            Option(f"{s.name[:70]}{'  (archived)' if s.archived else ''}\n  {s.summary}",
                   id=s.id) for s in listed])
        if listed:
            option_list.highlighted = min(current or 0, len(listed) - 1)
        self.query_one("#session-empty", Static).update(
            "" if listed else "No sub-sessions in this conversation. The agent starts one "
            "when it hands off a task.")
        self._buttons()

    @on(Checkbox.Changed, "#show-archived")
    def _archived_toggled(self) -> None:
        self.refresh_view()

    @on(OptionList.OptionHighlighted)
    def _buttons(self) -> None:
        if not self.is_attached:
            return
        session = self._selected()
        shown = {
            "#open-session": session is not None,
            "#resume-session": session is not None and session.resumable,
            "#stop-session": session is not None and session.running,
            "#limit-session": session is not None,
            "#rename-session": session is not None,
            "#archive-session": session is not None,
            "#delete-session": session is not None}
        for selector, visible in shown.items():
            self.query_one(selector, Button).display = visible
        if session is not None:
            self.query_one("#archive-session", Button).label = (
                "Unarchive" if session.archived else "Archive")
        self.query_one("#default-limit", Button).label = (
            f"Limit per sub-session: ${self.sessions.limit_usd:g}")

    def _ask_limit(self, title: str, current: float,
                   then: Callable[[float], Awaitable[None]]) -> None:
        """Asks for dollars; raising is confirmed first."""
        def chosen(text: str | None) -> None:
            if text is None:
                return
            try:
                usd = float(text.strip().lstrip("$"))
            except ValueError:
                self.notify("Write the limit as a number of dollars.", severity="error")
                return
            if usd > current:
                self.app.push_screen(
                    ConfirmScreen(f"Raise it to ${usd:g}?", yes="Raise"),
                    lambda yes: yes and self._run(then(usd)))
            else:
                self._run(then(usd))

        self.app.push_screen(TextPromptScreen(title, f"{current:g}",
                                              hint="US dollars."), chosen)

    @on(Button.Pressed, "#limit-session")
    def _limit(self) -> None:
        if (session := self._selected()) is not None:
            self._ask_limit(f"Most “{session.name[:40]}” may spend "
                            f"(${session.spent:.2f} spent so far)", session.budget_usd,
                            lambda usd: self.sessions.set_child_limit(session, usd))

    @on(Button.Pressed, "#default-limit")
    def _default_limit(self) -> None:
        self._ask_limit("Most a new sub-session may spend, unless the agent gives it less",
                        self.sessions.limit_usd, self.sessions.set_limit)

    def _run(self, awaitable: Awaitable[Any]) -> None:
        async def run() -> None:
            try:
                await awaitable
            except Exception as e:  # noqa: BLE001 - shown, never fatal
                self.notify(str(e) or type(e).__name__, severity="error", markup=False)

        self.run_worker(run())

    @on(OptionList.OptionSelected)
    @on(Button.Pressed, "#open-session")
    def _open(self) -> None:
        if (session := self._selected()) is not None:
            self.dismiss(session.id)

    @on(Button.Pressed, "#resume-session")
    def _resume(self) -> None:
        if (session := self._selected()) is not None:
            self._run(self.sessions.resume(session))

    @on(Button.Pressed, "#stop-session")
    def _stop(self) -> None:
        if (session := self._selected()) is not None:
            self.sessions.stop(session)

    @on(Button.Pressed, "#rename-session")
    def _rename(self) -> None:
        if (session := self._selected()) is not None:
            self.app.push_screen(
                TextPromptScreen("Rename sub-session", session.title or "",
                                 hint="Leave it empty to show its task."),
                lambda title: title is not None and self.sessions.rename(session, title))

    @on(Button.Pressed, "#archive-session")
    def _archive(self) -> None:
        if (session := self._selected()) is not None:
            self.sessions.archive(session, not session.archived)

    @on(Button.Pressed, "#delete-session")
    def _delete(self) -> None:
        if (session := self._selected()) is not None:
            self.app.push_screen(
                ConfirmScreen(f"Delete the sub-session “{session.name[:60]}”? It stops, and its "
                              "conversation is erased. This cannot be undone.", yes="Delete",
                              danger=True),
                lambda yes: yes and self._run(self.sessions.delete(session)))

    @on(Button.Pressed, "#close")
    def _close(self) -> None:
        self.dismiss(None)


class StartScreen(ModalScreen[StartOptions | None]):
    """Chooses the conversation and model, and how a new conversation starts."""

    BINDINGS: ClassVar = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, runtime: RuntimeAPI, *, cancellable: bool = False) -> None:
        super().__init__()
        self.runtime = runtime
        self.cancellable = cancellable
        self.conversations: dict[str, ConversationInfo] = {}

    def compose(self) -> ComposeResult:
        # Scrolls so Start stays reachable when the terminal is shorter than the dialog.
        with VerticalScroll(classes="dialog"):
            yield Label("Start Kayden", classes="title")
            yield Label("Conversation")
            yield Select(
                [("New conversation", NEW_CONVERSATION)],
                value=NEW_CONVERSATION, allow_blank=False, id="conversation",
            )
            yield Label("Model")
            yield Select(
                [("Default", DEFAULT_MODEL)],
                value=DEFAULT_MODEL, allow_blank=False, id="model",
            )
            yield Label("Continue from")
            yield Select([("Nothing, start fresh", FRESH)], value=FRESH, allow_blank=False,
                         id="seed-from")
            yield Label("Folder")
            yield Input(placeholder="The default folder", id="folder")
            yield Button("Start", variant="primary", id="start")

    def on_mount(self) -> None:
        self.query_one("#start", Button).focus()  # Enter starts with the defaults
        self.run_worker(self._load_models(), exit_on_error=False)
        self.run_worker(self._load_conversations(), exit_on_error=False)
        self.run_worker(self._load_folder(), exit_on_error=False)

    def action_cancel(self) -> None:
        if self.cancellable:
            self.dismiss(None)

    async def _load_models(self) -> None:
        models = await self.runtime.list_models()
        self.query_one("#model", Select).set_options(
            [("Default", DEFAULT_MODEL), *((f"{m.name} [{m.id}]", m.id) for m in models)]
        )
        self.query_one("#model", Select).value = DEFAULT_MODEL

    async def _load_conversations(self) -> None:
        conversations = await self.runtime.list_conversations()
        self.conversations = {c.id: c for c in conversations}
        labels = [(conversation_label(c), c.id) for c in conversations if not is_blank(c)]
        self.query_one("#conversation", Select).set_options(
            [("New conversation", NEW_CONVERSATION), *labels])
        self.query_one("#seed-from", Select).set_options(
            [("Nothing, start fresh", FRESH), *labels])

    async def _load_folder(self) -> None:
        folder = self.query_one("#folder", Input)
        default = (await self.runtime.settings()).work_folder
        if default is None:  # the Tools have a fixed folder
            folder.placeholder, folder.disabled = "Set by the tools", True
        else:
            folder.placeholder = f"{folder_path(default)} (the default)"

    @on(Select.Changed, "#conversation")
    def _conversation_changed(self, event: Select.Changed) -> None:
        resumed = self.conversations.get(str(event.value))
        self.query_one("#seed-from").disabled = resumed is not None
        self.query_one("#folder").disabled = resumed is not None

    @on(Button.Pressed, "#start")
    def _start(self) -> None:
        conversation = self.query_one("#conversation", Select).value
        model = self.query_one("#model", Select).value
        model_id = None if model == DEFAULT_MODEL else str(model)
        if conversation != NEW_CONVERSATION:
            self.dismiss(StartOptions(conversation_id=str(conversation), model=model_id))
            return
        seed = self.query_one("#seed-from", Select).value
        folder = self.query_one("#folder", Input).value.strip()
        self.dismiss(StartOptions(
            model=model_id,
            seed_from=None if seed == FRESH else str(seed),
            folder=str(Path(folder).expanduser()) if folder else None,
        ))


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
    StepGroup { background: transparent; border-top: none; padding: 0; }
    StepGroup CollapsibleTitle { color: $text-muted; padding: 0; }
    StepGroup Contents { padding: 0 0 0 2; }
    StepGroup Contents > * { margin-top: 1; }
    UserEntry { color: $accent; }
    SystemEntry.warning { color: $warning; }
    SystemEntry.error { color: $error; }
    #permissions { height: auto; max-height: 50%; }
    PermissionPrompt { height: auto; border: round $warning; padding: 0 1; }
    PermissionPrompt .buttons { height: auto; }
    WrapUpPrompt { height: auto; border: round $primary; padding: 0 1; }
    WrapUpPrompt .buttons { height: auto; }
    WrapUpPrompt Button { margin-right: 1; }
    PermissionPrompt Button { margin-right: 1; }
    ModalScreen { align: center middle; }
    .dialog { width: 70; height: auto; max-height: 90%; border: thick $primary; padding: 1 2;
              background: $surface; }
    .dialog Select, .dialog Button { margin-bottom: 1; }
    .dialog TextArea { height: 6; }
    .dialog .buttons { height: auto; }
    .dialog .actions Button { min-width: 0; width: auto; margin-right: 1; }
    .dialog OptionList { height: auto; max-height: 20; margin-bottom: 1; }
    .dialog .hint { color: $text-muted; margin-bottom: 1; }
    .dialog .error { color: $error; margin-bottom: 1; }
    .switch-row { height: auto; }
    .switch-row Label { padding: 1 0 0 1; }
    """
    BINDINGS: ClassVar = [
        Binding("escape", "stop", "Stop"),
        Binding("ctrl+r", "activate", "Activate", priority=True),
        Binding("ctrl+o", "models", "Model", priority=True),
        Binding("f2", "settings", "Settings", priority=True),
        Binding("f3", "conversations", "Conversations", priority=True),
        Binding("f4", "memory", "Memory", priority=True),
        Binding("f5", "duty", "Duty", priority=True),
        Binding("ctrl+k", "operations", "Cancel task", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, runtime: RuntimeAPI, start_options: StartOptions | None = None,
                 sessions: SubSessions | None = None) -> None:
        super().__init__()
        self.runtime = runtime
        self.start_options = start_options
        self.sessions = sessions
        self.home = runtime  # this app's own runtime; `runtime` is the one shown
        self.viewing: tuple[SubSession, RemoteRuntime] | None = None
        self._generation = 0  # which pump feeds the inbox
        if sessions is not None:
            sessions.listeners.append(self._sessions_changed)
        self.last_seq = -1
        self.state = AgentState.NOT_STARTED
        self.pending_ops = 0
        self.config: AgentConfig | None = None
        self.conversation: ConversationInfo | None = None
        self.model: str | None = None
        self.usage: UsageSnapshot | None = None
        self.duty_wake_at: float | None = None
        self.help_needed: str | None = None
        self.notice = ""
        self._inbox: asyncio.Queue[EventEnvelope | _Resync | None] = asyncio.Queue()
        self._tool_entries: dict[str, ToolEntry] = {}
        self._card: WrapUpPrompt | None = None
        self._card_proposals: set[str] = set()  # wrap_up options pending, shown on the card
        self._stream: list[AssistantEntry | ToolEntry] = []
        self._queued: dict[str, UserEntry] = {}
        self._rendered_ids: set[str] = set()
        self._prompts: dict[str, PermissionPrompt] = {}
        self._proposals: dict[str, ProposalPrompt] = {}
        self._oldest_id: str | None = None  # oldest history message loaded
        self._compaction: SystemEntry | None = None  # the line of a running compaction
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
        self.run_worker(self._pump(self._generation), group="pump", exit_on_error=False)
        self.query_one("#prompt", Input).focus()
        if self.runtime.snapshot().conversation is None:
            self._call(self._begin())
        self._render_status()

    async def _begin(self) -> None:
        """Settings with the error when Kayden could not start (first run: no key yet);
        otherwise the start options or the start screen."""
        settings = await self.runtime.settings()
        if settings.already_open:  # nothing to fix in Settings: the other one is in use
            self.exit(return_code=1, message=f"{settings.startup_error}. Use that one, or "
                                             "quit it and start Kayden again.")
        elif settings.startup_error is not None:
            self.notice = "Kayden could not start: fix it in Settings (F2)"
            self._render_status()
            self.action_settings()
        elif self.start_options is not None:
            await self.start_conversation(self.start_options)
        else:
            self.push_screen(StartScreen(self.runtime), self._on_start_chosen)

    # ── event plumbing ───────────────────────────────────────────────────

    async def _pump(self, generation: int) -> None:
        runtime = self.runtime
        try:
            async for envelope in runtime.events():
                await self._inbox.put(envelope)
        finally:
            if generation == self._generation:  # not when another runtime is shown
                await self._inbox.put(None)

    def _show(self, runtime: RuntimeAPI) -> None:
        """Show another runtime: this app's own or a sub-session's."""
        self._generation += 1
        self.workers.cancel_group(self, "pump")
        self.runtime = runtime
        self.last_seq = -1
        self._inbox.put_nowait(RESYNC)
        self.run_worker(self._pump(self._generation), group="pump", exit_on_error=False)

    async def view_session(self, session: SubSession) -> None:
        """Show a sub-session in place of this conversation; /back returns."""
        assert self.sessions is not None
        remote = await self.sessions.open(session)
        self._leave_session()
        self.viewing = (session, remote)
        self._show(remote)
        self._render_status()

    def _leave_session(self) -> None:
        if self.viewing is not None:
            session, remote = self.viewing
            self.viewing = None
            assert self.sessions is not None
            self.sessions.release(session, remote)
            self._show(self.home)

    def _deliver_notes(self) -> None:
        """Tell this app's conversation what its sub-sessions did while nobody waited."""
        snapshot = self.home.snapshot()
        if self.sessions is not None and snapshot.conversation is not None:
            for note in self.sessions.take_notes(snapshot.conversation.id):
                self._call(self.home.send(note))

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
        self._proposals.clear()
        self._card = None
        self._card_proposals.clear()
        self.last_seq = snap.seq
        self.state = snap.state
        self.pending_ops = len(snap.operations)
        self.config = snap.config
        self.conversation = snap.conversation
        self.model = snap.model
        self.usage = snap.usage
        self.duty_wake_at = snap.duty_wake_at
        self.help_needed = snap.help_needed
        self._oldest_id = history[0].id if history else None
        if len(history) == HISTORY_PAGE:
            await self._mount(LoadEarlier())
        for message in history:
            await self._render_message(message)
        for op in snap.operations:
            entry = self._tool_entries.get(op.call.id) or await self._mount_tool(op.call)
            entry.set_phase("background" if op.backgrounded else "running")
            if op.last_progress:
                entry.set_progress(op.last_progress)
        for queued in snap.queued_messages:
            await self._apply(queued)
        for request in snap.pending_permissions:
            await self._add_prompt(request)
        for proposal in snap.proposals:
            await self._add_proposal(proposal)
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
                    entry = await self._mount_step(ToolEntry(call_id, name))
                    self._tool_entries[call_id] = entry
                    self._stream.append(entry)
                entry.stream_arguments(name, chunk)
            case MessageQueued(message_id=message_id, text=text):
                await self._set_card(None)
                if message_id not in self._queued and notice_of(text) is None:
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
            case PermissionResolved(request_id=request_id):
                if (prompt := self._prompts.pop(request_id, None)) is not None:
                    await prompt.remove()
            case ProposalMade(proposal=proposal):
                await self._add_proposal(proposal)
            case ProposalResolved(proposal_id=proposal_id):
                self._card_proposals.discard(proposal_id)
                if (card := self._proposals.pop(proposal_id, None)) is not None:
                    await card.remove()
            case ModelRetry(attempt=attempt, max_attempts=max_attempts, delay_s=delay, error=err):
                self.notice = f"Retrying ({attempt} of {max_attempts}) in {delay:.0f}s: {err}"
                await self._discard_stream()  # the retried request streams from scratch
                await self._prune_groups()
            case ModelSelected(model_id=model_id):
                self.model = model_id
            case ConversationUpdated(info=info):
                if self.conversation is not None and info.id == self.conversation.id:
                    self.conversation = info
                self._reload(ConversationsScreen)
            case ConversationDeleted():
                self._reload(ConversationsScreen)
            case MemoriesChanged(saved=saved):
                if saved:  # Memory saved on its own after the answer: say so
                    await self._mount(SystemEntry(
                        f"Memory updated: {', '.join(saved)} (F4 to see it)"))
                self._reload(MemoryScreen)
            case DutyScheduled(wake_at=wake_at):
                self.duty_wake_at = wake_at
            case HelpNeeded(question=question):
                self.help_needed = question
                if question is not None:
                    await self._mount(SystemEntry(help_text(question), severity="warning"))
            case MaintenanceStarted():
                self.notice = "Tidying up memory…"
            case MaintenanceFinished():
                self.notice = ""
            case CompactionStarted():
                self._compaction = SystemEntry(COMPACTING)
                await self._mount(self._compaction)
            case CompactionFinished(compacted=compacted):
                if (line := self._compaction) is not None and line.is_mounted:
                    if compacted:
                        line.update(Text(COMPACTED))
                    else:
                        await line.remove()
                self._compaction = None
            case StopStalled(call_ids=call_ids):
                self.notice = "Some tasks did not stop in time"
                await self._mount(
                    SystemEntry(
                        f"These tasks did not confirm that they stopped and may still be "
                        f"running: {', '.join(call_ids)}",
                        severity="warning",
                    )
                )
            case UsageUpdated(snapshot=snapshot):
                self.usage = snapshot
            case Failure(kind=kind, message=message):
                await self._mount(SystemEntry(f"{kind.value} failure: {message}", severity="error"))
            case ShuttingDown(reason=reason):
                await self._mount(SystemEntry(f"Kayden is closing: {reason}", severity="warning"))
                self.exit()

    def _reload(self, kind: type[ListScreen]) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, kind):
                screen.reload()

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
        entry = await self._mount_step(ToolEntry(call.id, call.name, call.arguments))
        self._tool_entries[call.id] = entry
        return entry

    async def _mount_step(self, entry: ToolEntry) -> ToolEntry:
        """Add a step to the group just before it, or start one, taking along the thinking
        that came right before the step."""
        if entry.tool_name == "wrap_up":  # shown as the card instead
            entry.display = False
            return await self._mount(entry)
        transcript = self.query_one("#transcript", VerticalScroll)
        children = list(transcript.children)
        end = (children.index(self._insert_before) if self._insert_before is not None
               else len(children))
        start = end
        while start > 0 and isinstance(prior := children[start - 1], AssistantEntry) \
                and not prior.body and not prior.refused:
            start -= 1
        thinking = children[start:end]
        group = children[start - 1] if start > 0 else None
        if not isinstance(group, StepGroup):
            group = StepGroup()
            if thinking:
                await transcript.mount(group, before=thinking[0])
            else:
                await self._mount(group)
        for old in thinking:  # moved by copy: a widget cannot change parents
            assert isinstance(old, AssistantEntry)
            copy = AssistantEntry(old.body, old.reasoning, final=old.final)
            copy.interrupted = old.interrupted
            self._stream = [copy if w is old else w for w in self._stream]
            await old.remove()
            await group.add(copy)
        await group.add(entry)
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

    async def _prune_groups(self) -> None:
        """Remove step groups whose steps were all discarded."""
        for group in self.query_one("#transcript").query(StepGroup):
            if not group.query(ToolEntry):
                await group.remove()

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
        await self._prune_groups()
        if stop is StopReason.REFUSAL:
            if last is None:
                last = await self._mount(AssistantEntry(final=True))
            last.mark_refused()

    async def _render_message(self, message: Message) -> AssistantEntry | None:
        """Mount a message's entries; returns its last assistant text entry, if any."""
        self._rendered_ids.add(message.id)
        if message.role is Role.USER:
            if self._insert_before is None:  # not an earlier page
                await self._set_card(None)
            text = "".join(b.text for b in message.blocks if isinstance(b, TextBlock))
            if (notice := notice_of(text)) is not None:
                if "\n" in notice or len(notice) > 160:  # a new goal's brief: folded
                    title = _clip(notice.splitlines()[0], 100)
                    await self._mount(Collapsible(Static(notice, markup=False), title=title,
                                                  collapsed=True, classes="notice"))
                else:
                    await self._mount(SystemEntry(notice))
            elif text:
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
                    if (card := card_of(call)) and self._insert_before is None:
                        await self._set_card(card)
                    current = None
                case ToolResult() as result:
                    await self._finish_tool(result, None)
                    current = None
                case ServerToolUse() as use:  # the provider ran it: a finished step
                    call, result = provider_step(use)
                    await self._mount_tool(call)
                    await self._finish_tool(result, None)
                    current = None
        return last

    async def _finish_tool(self, result: ToolResult | OperationResult, duration: float | None
                           ) -> None:
        entry = self._tool_entries.get(result.call_id)
        if entry is None:
            entry = await self._mount_step(ToolEntry(result.call_id, None))
            self._tool_entries[result.call_id] = entry
        entry.finish(result, duration)

    async def _add_proposal(self, proposal: Proposal) -> None:
        if proposal.card:
            self._card_proposals.add(proposal.id)
            return
        if proposal.id in self._proposals:
            return
        prompt = ProposalPrompt(proposal)
        self._proposals[proposal.id] = prompt
        await self.query_one("#permissions", Vertical).mount(prompt)

    async def _set_card(self, card: Card | None) -> None:
        if self._card is not None and self._card.card == card:
            return
        if self._card is not None:
            await self._card.remove()
            self._card = None
        if card is not None:
            self._card = WrapUpPrompt(card)
            await self.query_one("#permissions", Vertical).mount(self._card)

    def choose(self, option: str) -> None:
        card = self._card.card if self._card is not None else None
        if card is not None and option == card.action and card.call_id in self._card_proposals:
            self.resolve_proposal(card.call_id, True)
            self.close_card()
        else:
            self._call(self.runtime.send(option))

    def close_card(self) -> None:
        self.run_worker(self._set_card(None))

    def resolve_proposal(self, proposal_id: str, accept: bool) -> None:
        self._call(self.runtime.resolve_proposal(proposal_id, accept))

    async def _add_prompt(self, request: PermissionRequest) -> None:
        if request.id in self._prompts:
            return
        prompt = PermissionPrompt(request)
        self._prompts[request.id] = prompt
        await self.query_one("#permissions", Vertical).mount(prompt)

    def _sessions_changed(self) -> None:
        with contextlib.suppress(NoMatches):  # before the status line is mounted
            self._render_status()
        self._deliver_notes()

    def _render_status(self) -> None:
        state = STATE_LABELS[self.state]
        parts = [state + (f" · {self.pending_ops} running" if self.pending_ops else "")]
        if self.viewing is not None:
            parts.insert(0, f"Sub-session: {self.viewing[0].name[:40]} (/back returns)")
        if self.model:
            parts.append(self.model)
        if self.conversation is not None and self.conversation.folder is not None:
            parts.append(folder_path(self.conversation.folder))
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
        if self.conversation is not None and (duty := duty_status(
                self.conversation.duty, self.duty_wake_at, self.state, self.help_needed)):
            parts.append(duty)
        if self.sessions is not None and self.conversation is not None and (
                working := working_text(self.sessions.of(self.conversation.id))):
            parts.append(working)
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

    def _can_switch(self) -> bool:
        """Whether runtime.start() may open another conversation now; tells the user if not."""
        if self.state in SWITCHABLE and not self.pending_ops:
            return True
        self.notify("Finish or stop the current task first.", severity="warning")
        return False

    def _on_start_chosen(self, options: StartOptions | None) -> None:
        if options is not None and not (options == StartOptions() and self._blank()):
            self._call(self.start_conversation(options))  # a blank chat is already new

    async def start_conversation(self, options: StartOptions, *,
                                 discard_blank: bool = True) -> None:
        """Open a chat. A blank one left behind is deleted, so lists never fill with chats
        nobody wrote in."""
        blank = self._blank() if discard_blank else None
        self.conversation = await self.runtime.start(options)
        await self._inbox.put(RESYNC)
        self._deliver_notes()
        if blank is not None and blank.id != self.conversation.id:
            with contextlib.suppress(NotImplementedError):
                await self.runtime.delete_conversation(blank.id)

    def _blank(self) -> ConversationInfo | None:
        """The open chat, if nothing has happened in it yet."""
        info = self.conversation
        if info is None or self._rendered_ids:
            return None
        return info if info.duty is None and info.seed_from is None and not info.title else None

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
            case "settings":
                self.action_settings()
            case "conversations":
                self.action_conversations()
            case "memory":
                self.action_memory()
            case "back":
                self._leave_session()
            case "approve" | "dismiss":
                if not self._proposals:
                    self.notify("Nothing is proposed.", severity="warning")
                else:
                    self.resolve_proposal(next(iter(self._proposals)), name == "approve")
            case "mcp":
                self._leave_session()
                self.push_screen(McpScreen(self.runtime))
            case "duty" | "goal" if arg == "off":
                self._call(self.runtime.set_duty(None))
            case "duty" | "goal" if arg:
                current = self.conversation.duty if self.conversation is not None else None
                kind = DutyKind.GOAL if name == "goal" else DutyKind.DUTY
                duty = replace(current, instruction=arg, kind=kind) if current else Duty(arg, kind)
                self._call(self.runtime.set_duty(duty))
            case "duty" | "goal" | "plan":
                self.action_duty()
            case "sessions":
                if self.sessions is None:
                    self.notify("Sub-sessions are not available here.", severity="warning")
                else:
                    self._leave_session()
                    own = self.home.snapshot().conversation
                    self.push_screen(SessionsScreen(self.sessions, own.id if own else None),
                                     self._on_session_chosen)
            case "folder" if arg:
                self._leave_session()
                self.new_in_folder(str(Path(arg).expanduser()))
            case "folder":
                folder = self.conversation.folder if self.conversation is not None else None
                self.notify(f"Kayden works in {folder_path(folder)}. /folder PATH starts a "
                            "chat in another folder." if folder is not None
                            else "These tools work in one fixed folder.", markup=False)
            case "cancel" if arg:
                self._call(self.runtime.cancel_operation(arg))
            case "cancel":
                self.action_operations()
            case "allow" | "deny":
                decision = Decision.DENY if name == "deny" else DECISION_ALIASES.get(arg or "once")
                if decision is None:
                    self.notify("Use /allow once or /allow conversation.", severity="error")
                elif not self._prompts:
                    self.notify("Nothing is waiting for permission.", severity="warning")
                else:
                    self.resolve_permission(next(iter(self._prompts)), decision)
            case "quit" | "exit":
                self.exit()
            case "help":
                self.notify(HELP, markup=False, timeout=10)
            case _:
                self.notify(f"Unknown command /{name}. {HELP}", severity="error", markup=False)

    def new_in_folder(self, folder: str) -> None:
        """Start a chat in `folder`."""
        if self._can_switch():
            self._call(self.start_conversation(StartOptions(folder=folder)))

    def action_stop(self) -> None:
        self.notice = "Stop requested"
        self._render_status()
        self._call(self.runtime.stop())

    def action_activate(self) -> None:
        self._call(self.runtime.activate())

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "activate":
            return self.config is not None and self.conversation is not None
        return True

    def _on_session_chosen(self, session_id: str | None) -> None:
        if self.sessions is None or session_id is None:
            return
        session = next((s for s in self.sessions.sessions if s.id == session_id), None)
        if session is not None:
            self._call(self.view_session(session))

    def action_conversations(self) -> None:
        self._leave_session()
        open_id = None if self.conversation is None else self.conversation.id
        self.push_screen(ConversationsScreen(self.runtime, open_id),
                         self._on_conversation_chosen)

    def _on_conversation_chosen(self, choice: str | None) -> None:
        if choice is None or (self.conversation is not None
                              and choice == self.conversation.id):
            return
        if not self._can_switch():
            return
        if choice == NEW_CONVERSATION:
            self.push_screen(StartScreen(self.runtime, cancellable=True), self._on_start_chosen)
        else:
            self._call(self.start_conversation(StartOptions(conversation_id=choice)))

    def action_memory(self) -> None:
        self._leave_session()
        async def open_screen() -> None:
            try:
                await self.runtime.notes()
            except NotImplementedError:
                self.notify("This memory module cannot edit memories.", severity="warning")
                return
            self.push_screen(MemoryScreen(self.runtime))

        self._call(open_screen())

    def action_duty(self) -> None:
        if self.conversation is None:
            self.notify("Open a conversation first.", severity="warning")
            return
        self.push_screen(DutyScreen(self.conversation), self._on_duty_chosen)

    def _on_duty_chosen(self, choice: DutyChoice | None) -> None:
        if choice is None:
            return
        duty, plan = choice

        async def apply() -> None:
            await self.runtime.set_plan(plan)
            if duty is not None or (self.conversation and self.conversation.duty):
                await self.runtime.set_duty(duty)

        self._call(apply())

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

    def action_settings(self) -> None:
        self._leave_session()
        async def open_screen() -> None:
            settings = await self.runtime.settings()
            try:
                models = await self.runtime.list_models()
            except Exception:  # noqa: BLE001 - e.g. no key yet; the provider can still be set
                models = []
            spent = self.runtime.snapshot().usage.usage.cost_usd or 0.0
            self.push_screen(SettingsScreen(settings, models, spent), self._on_settings_chosen)

        self._call(open_screen())

    def _on_settings_chosen(self, result: tuple[SettingsUpdate, bool] | None) -> None:
        if result is None:
            return
        update, restart = result

        async def apply() -> None:
            settings = await self.runtime.settings()
            if update != SettingsUpdate():
                settings = await self.runtime.update_settings(update)
                self.notify("Settings saved")
            if restart:
                await self.runtime.restart()
            elif needs_restart(update) and (notice := restart_notice(settings)):
                self.push_screen(RestartScreen(notice), self._on_restart_chosen)

        self._call(apply())

    def _on_restart_chosen(self, now: bool | None) -> None:
        if now:
            self._call(self.runtime.restart())

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
    """Options: start, conversation_id, model. With start true the agent starts right away
    with the others; otherwise the start screen asks."""
    data_dir = paths.data_dir if paths is not None else None
    if not options.get("start"):
        return TuiFrontend(data_dir=data_dir)
    return TuiFrontend(StartOptions(conversation_id=options.get("conversation_id"),
                                    model=options.get("model")), data_dir)


factory.display_name = "Terminal (TUI)"  # type: ignore[attr-defined]
factory.kind = "terminal"  # type: ignore[attr-defined]


class TuiFrontend(Frontend):
    def __init__(self, start_options: StartOptions | None = None,
                 data_dir: Path | None = None) -> None:
        self.start_options = start_options
        self.sessions = SubSessions(data_dir) if data_dir is not None else None

    def tools(self) -> Sequence[Tools]:
        return [self.sessions] if self.sessions is not None else []

    @property
    def info(self) -> FrontendInfo:
        return FrontendInfo(name="tui", interactive_permissions=True)

    async def run(self, runtime: RuntimeAPI) -> None:
        """Returns when the user quits or the runtime's event stream ends."""
        await KaydenApp(runtime, self.start_options, self.sessions).run_async()
