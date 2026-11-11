"""TUI tests against a stub RuntimeAPI that replays scripted event sequences."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace

import pytest
from rich.text import Text as RichText
from textual.pilot import Pilot
from textual.widgets import Button, Input, Label, OptionList, Select, Static, Switch, TextArea

from kayden.api import (
    PROFILE,
    AgentConfig,
    AgentState,
    Budget,
    ConversationDeleted,
    ConversationInfo,
    ConversationUpdated,
    Decision,
    EventEnvelope,
    Failure,
    FailureKind,
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
    PermissionPolicy,
    PermissionRequest,
    PermissionRequested,
    PermissionResolved,
    Reasoning,
    ReasoningDelta,
    Role,
    RuntimeEvent,
    RuntimeSnapshot,
    RuntimeStateError,
    Settings,
    SettingsUpdate,
    ShuttingDown,
    StartOptions,
    StateChanged,
    StopReason,
    StopStalled,
    Text,
    TextDelta,
    ToolBackgrounded,
    ToolCall,
    ToolCallDelta,
    ToolFinished,
    ToolProgress,
    ToolResult,
    ToolStarted,
    ToolStatus,
    Usage,
    UsageSnapshot,
    UsageUpdated,
)
from kayden.frontends.tui import (
    AssistantEntry,
    ConfirmScreen,
    ConversationsScreen,
    KaydenApp,
    MemoryScreen,
    ModelScreen,
    NoteScreen,
    OperationsScreen,
    PermissionPrompt,
    SettingsScreen,
    StartScreen,
    StepGroup,
    SystemEntry,
    TextPromptScreen,
    ToolEntry,
    TuiFrontend,
    UserEntry,
)
from tests.fakes.settings import settings as fake_settings

CONVERSATION = ConversationInfo("conv-1", "Test", 0.0, 0.0, folder="/work")
OTHERS = [
    ConversationInfo("conv-2", None, 0.0, 0.0, preview="What is the weather"),
    ConversationInfo("conv-3", None, 0.0, 0.0),
    ConversationInfo("conv-4", "Old plans", 0.0, 0.0, archived=True),
]
MODELS = [ModelInfo("m1", "Model One", 100_000), ModelInfo("m2", "Model Two", 200_000)]
MEMORIES = [MemoryNote("1", PROFILE, "Who the user is", "Likes tea", "conv-1", 0.0),
            MemoryNote("2", "Trip", "The Oslo trip", "Leaves Friday", None, 0.0)]


class StubRuntime:
    """Scriptable RuntimeAPI: tests emit events and inspect the recorded commands."""

    def __init__(
        self,
        *,
        started: bool = True,
        history: Sequence[Message] = (),
        operations: Sequence[OperationInfo] = (),
        pending_permissions: Sequence[PermissionRequest] = (),
        queued: Sequence[MessageQueued] = (),
        editable_memory: bool = True,
    ) -> None:
        self.seq = 0
        self.state = AgentState.IDLE if started else AgentState.NOT_STARTED
        self.conversations = [CONVERSATION, *OTHERS]
        self.conversation = self.conversations[0] if started else None
        self.config = AgentConfig(Budget(), PermissionPolicy.ASK) if started else None
        self.model = "m1"
        self.history_messages = list(history)
        self.operations = list(operations)
        self.pending_permissions = list(pending_permissions)
        self.queued = list(queued)
        self.entries = list(MEMORIES) if editable_memory else None
        self.calls: list[tuple] = []
        self.command_delay = 0.0
        self.fail_with: dict[str, Exception] = {}
        self.spent = 1.25
        self.current_settings = fake_settings()
        self._subscribers: list[asyncio.Queue[EventEnvelope | None]] = []

    # scripting

    def emit(self, *events: RuntimeEvent) -> None:
        for event in events:
            self.seq += 1
            if isinstance(event, StateChanged):
                self.state = event.state
            for queue in self._subscribers:
                queue.put_nowait(EventEnvelope(self.seq, event))

    def skip(self, n: int) -> None:
        """Simulate a lagging subscriber: these sequence numbers never arrive."""
        self.seq += n

    def close(self) -> None:
        for queue in self._subscribers:
            queue.put_nowait(None)

    async def _record(self, name: str, *args: object) -> None:
        self.calls.append((name, *args))
        if self.command_delay:
            await asyncio.sleep(self.command_delay)
        if name in self.fail_with:
            raise self.fail_with[name]

    def _update(self, conversation_id: str, **changes: object) -> None:
        index = [c.id for c in self.conversations].index(conversation_id)
        info = replace(self.conversations[index], **changes)
        self.conversations[index] = info
        if self.conversation is not None and self.conversation.id == info.id:
            self.conversation = info
        self.emit(ConversationUpdated(info))

    def _memories(self) -> list[MemoryNote]:
        if self.entries is None:
            raise NotImplementedError("this memory module cannot edit memories")
        return self.entries

    # RuntimeAPI

    def events(self) -> AsyncIterator[EventEnvelope]:
        queue: asyncio.Queue[EventEnvelope | None] = asyncio.Queue()
        self._subscribers.append(queue)

        async def stream() -> AsyncIterator[EventEnvelope]:
            while (envelope := await queue.get()) is not None:
                yield envelope

        return stream()

    def snapshot(self) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            seq=self.seq,
            state=self.state,
            conversation=self.conversation,
            config=self.config,
            model=self.model,
            usage=UsageSnapshot(Usage(cost_usd=self.spent), 0, 0.0),
            operations=list(self.operations),
            pending_permissions=list(self.pending_permissions),
            queued_messages=list(self.queued),
        )

    async def list_models(self) -> Sequence[ModelInfo]:
        if "list_models" in self.fail_with:
            raise self.fail_with["list_models"]
        return MODELS

    async def list_tools(self):
        return []

    async def list_conversations(self, *, include_archived: bool = False
                                 ) -> Sequence[ConversationInfo]:
        return [c for c in self.conversations if include_archived or not c.archived]

    async def history(self, conversation_id: str, *, before: str | None = None,
                      limit: int | None = None) -> Sequence[Message]:
        if before is None:
            self.calls.append(("history", conversation_id))
        else:
            self.calls.append(("history_before", before))
        messages = list(self.history_messages)
        if before is not None:
            messages = messages[: [m.id for m in messages].index(before)]
        return messages[-limit:] if limit else messages

    async def search_archive(self, query, *, conversation_id=None, limit=10):
        return []

    async def inspect_memory(self, conversation_id):
        return None

    async def notes(self) -> Sequence[MemoryNote]:
        return list(self._memories())

    async def start(self, options: StartOptions) -> ConversationInfo:
        await self._record("start", options)
        if options.conversation_id is None:
            info = ConversationInfo("conv-new", None, 0.0, 0.0, seed_from=options.seed_from,
                                    folder=options.folder or "/work")
            self.conversations.insert(0, info)
        else:
            info = next(c for c in self.conversations if c.id == options.conversation_id)
        self.conversation = info
        self.config = AgentConfig(Budget(), PermissionPolicy.ASK)
        self.emit(StateChanged(AgentState.IDLE))
        return info

    async def rename_conversation(self, conversation_id: str, title: str) -> None:
        await self._record("rename_conversation", conversation_id, title)
        self._update(conversation_id, title=title or None)

    async def archive_conversation(self, conversation_id: str, archived: bool) -> None:
        await self._record("archive_conversation", conversation_id, archived)
        self._update(conversation_id, archived=archived)

    async def delete_conversation(self, conversation_id: str) -> None:
        await self._record("delete_conversation", conversation_id)
        if self.conversation is not None and conversation_id == self.conversation.id:
            raise RuntimeStateError("cannot delete the open conversation")
        self.conversations = [c for c in self.conversations if c.id != conversation_id]
        self.emit(ConversationDeleted(conversation_id))

    async def save_note(self, title: str, description: str, text: str, *,
                        note_id: str | None = None) -> MemoryNote:
        await self._record("save_note", title, description, text, note_id)
        notes = self._memories()
        if note_id is None:
            note = MemoryNote(str(len(notes) + 1), title, description, text, None, 0.0)
            notes.append(note)
        else:
            index = [n.id for n in notes].index(note_id)
            note = notes[index] = replace(notes[index], title=title, description=description,
                                          text=text)
        self.emit(MemoriesChanged())
        return note

    async def delete_note(self, note_id: str) -> None:
        await self._record("delete_note", note_id)
        self.entries = [n for n in self._memories() if n.id != note_id]
        self.emit(MemoriesChanged())

    async def set_duty(self, duty) -> None:
        await self._record("set_duty", duty)
        self._update(self.conversation.id, duty=duty)

    async def set_plan(self, plan) -> None:
        await self._record("set_plan", list(plan))
        self._update(self.conversation.id, plan=tuple(plan))

    async def select_model(self, model_id: str) -> None:
        await self._record("select_model", model_id)
        self.model = model_id
        self.emit(ModelSelected(model_id))

    async def cancel_operation(self, call_id: str) -> None:
        await self._record("cancel_operation", call_id)

    async def send(self, text: str) -> None:
        await self._record("send", text)

    async def activate(self) -> None:
        await self._record("activate")

    async def stop(self) -> None:
        await self._record("stop")

    async def resolve_permission(self, request_id: str, decision: Decision) -> None:
        await self._record("resolve_permission", request_id, decision)

    async def settings(self) -> Settings:
        return self.current_settings

    async def update_settings(self, update: SettingsUpdate) -> Settings:
        await self._record("update_settings", update)
        return self.current_settings

    async def restart(self) -> None:
        await self._record("restart")
        for queue in self._subscribers:
            queue.put_nowait(None)


def msg(id: str, role: Role, *blocks) -> Message:
    return Message(id, role, tuple(blocks), 0.0)


async def until(pilot: Pilot, predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await pilot.pause(0.01)


async def caught_up(pilot: Pilot, app: KaydenApp, stub: StubRuntime) -> None:
    await until(pilot, lambda: app.idle_and_caught_up(stub.seq))
    await pilot.pause()


def plain(widget: Static) -> str:
    content = widget.content
    return content.plain if isinstance(content, RichText) else str(content)


def entries[T](app: KaydenApp, cls: type[T]) -> list[T]:
    return list(app.query_one("#transcript").query(cls))


def status(app: KaydenApp) -> str:
    return plain(app.query_one("#status", Static))


async def type_text(pilot: Pilot, text: str) -> None:
    for char in text:
        await pilot.press("space" if char == " " else char)


async def submit(pilot: Pilot, app: KaydenApp, text: str) -> None:
    app.query_one("#prompt", Input).value = text
    app.query_one("#prompt", Input).focus()
    await pilot.press("enter")


def notices(app: KaydenApp) -> str:
    return "\n".join(n.message for n in app._notifications)


def make(stub: StubRuntime, **kwargs) -> KaydenApp:
    return KaydenApp(stub, **kwargs)  # type: ignore[arg-type]


def test_frontend_info():
    assert TuiFrontend().info.interactive_permissions is True


async def test_streams_text_and_commits_markdown():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test() as pilot:
        stub.emit(StateChanged(AgentState.THINKING), ReasoningDelta("hmm"))
        stub.emit(TextDelta("Hel"), TextDelta("lo **there**"))
        await caught_up(pilot, app, stub)
        assert "Thinking…" in status(app)
        [entry] = entries(app, AssistantEntry)
        assert (entry.body, entry.reasoning, entry.final) == ("Hello **there**", "hmm", False)

        stub.emit(MessageCommitted(msg("a1", Role.ASSISTANT, Text("Hello **there**"))))
        stub.emit(StateChanged(AgentState.IDLE))
        await caught_up(pilot, app, stub)
        [entry] = entries(app, AssistantEntry)
        assert entry.final and entry.body == "Hello **there**" and not entry.interrupted
        assert "Ready" in status(app)


async def test_model_retry_discards_the_failed_attempts_stream():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test() as pilot:
        stub.emit(StateChanged(AgentState.THINKING), TextDelta("Hal"))
        stub.emit(ToolCallDelta("c1", "shell", "{"))
        stub.emit(ModelRetry(1, 3, 2.0, "overloaded"), TextDelta("Hello"))
        await caught_up(pilot, app, stub)
        assert [e.body for e in entries(app, AssistantEntry)] == ["Hello"]
        assert not entries(app, ToolEntry)
        assert not entries(app, StepGroup)
        assert "Retrying (1 of 3)" in status(app)


async def test_tool_call_deltas_progress_and_results():
    stub = StubRuntime()
    app = make(stub)
    call = ToolCall("c1", "shell", {"command": "make"})
    async with app.run_test() as pilot:
        stub.emit(StateChanged(AgentState.THINKING), TextDelta("Running it."))
        stub.emit(ToolCallDelta("c1", "shell", '{"comm'), ToolCallDelta("c1", None, 'and":'))
        await caught_up(pilot, app, stub)
        [tool] = entries(app, ToolEntry)
        assert tool.tool_name == "shell" and tool.arguments == '{"command":'

        stub.emit(MessageCommitted(msg("a1", Role.ASSISTANT, Text("Running it."), call)))
        stub.emit(StateChanged(AgentState.RUNNING_TOOLS, pending_ops=1), ToolStarted(call))
        stub.emit(ToolProgress("c1", "compiling 3/10"))
        await caught_up(pilot, app, stub)
        [tool] = entries(app, ToolEntry)
        assert len(entries(app, AssistantEntry)) == 1
        assert "running: compiling 3/10" in plain(tool)
        assert "Working… · 1 running" in status(app)

        stub.emit(ToolBackgrounded("c1"))
        stub.emit(MessageCommitted(msg("t0", Role.TOOL,
                                       ToolResult("c1", ToolStatus.BACKGROUNDED, "op c1"))))
        stub.emit(ToolProgress("c1", "compiling 9/10"))
        await caught_up(pilot, app, stub)
        assert "background: compiling 9/10" in plain(tool)

        result = ToolResult("c1", ToolStatus.OK, "built\nok")
        stub.emit(ToolFinished(result, 1.5))
        stub.emit(MessageCommitted(msg("t1", Role.TOOL, result)))
        await caught_up(pilot, app, stub)
        [tool] = entries(app, ToolEntry)
        assert "✓ ok (1.5s)" in plain(tool) and "built" in plain(tool)


@pytest.mark.parametrize("decision", list(Decision))
async def test_permission_prompt_offers_every_decision(decision: Decision):
    stub = StubRuntime()
    app = make(stub)
    call = ToolCall("c1", "write_file", {"path": "x"})
    async with app.run_test(size=(120, 40)) as pilot:
        stub.emit(MessageCommitted(msg("a1", Role.ASSISTANT, call)))
        stub.emit(StateChanged(AgentState.AWAITING_PERMISSION))
        stub.emit(PermissionRequested(PermissionRequest("p1", call, read_only=False)))
        await caught_up(pilot, app, stub)
        assert "Waiting for you" in status(app)
        prompt = app.query_one(PermissionPrompt)
        assert {b.id for b in prompt.query(Button)} == {d.value for d in Decision}

        await pilot.click(f"#{decision.value}")
        await until(pilot, lambda: ("resolve_permission", "p1", decision) in stub.calls)
        assert all(b.disabled for b in prompt.query(Button))

        stub.emit(PermissionResolved("p1", decision, "c1"),
                  StateChanged(AgentState.RUNNING_TOOLS))
        await caught_up(pilot, app, stub)
        assert not app.query(PermissionPrompt)
        assert "permission" not in plain(entries(app, ToolEntry)[0])  # no permission line


async def test_permission_commands_and_runtime_side_resolution():
    stub = StubRuntime()
    app = make(stub)
    call = ToolCall("c1", "shell", {"command": "rm x"})
    async with app.run_test() as pilot:
        stub.emit(PermissionRequested(PermissionRequest("p1", call, read_only=False)))
        stub.emit(PermissionRequested(PermissionRequest("p2", call, read_only=False)))
        await caught_up(pilot, app, stub)
        await type_text(pilot, "/allow conversation")
        await pilot.press("enter")
        await until(pilot, lambda: ("resolve_permission", "p1", Decision.ALLOW_CONVERSATION)
                    in stub.calls)

        # Stop denies pending prompts runtime-side; the prompt must disappear.
        stub.emit(PermissionResolved("p1", Decision.ALLOW_CONVERSATION, "c1"))
        stub.emit(PermissionResolved("p2", Decision.DENY, "c1"))
        await caught_up(pilot, app, stub)
        assert not app.query(PermissionPrompt)


async def test_failed_permission_resolution_reenables_prompt():
    stub = StubRuntime()
    stub.fail_with["resolve_permission"] = RuntimeStateError("already resolved")
    app = make(stub)
    async with app.run_test(size=(120, 40)) as pilot:
        stub.emit(PermissionRequested(PermissionRequest("p1", ToolCall("c", "x", {}), True)))
        await caught_up(pilot, app, stub)
        await pilot.click("#deny")
        prompt = app.query_one(PermissionPrompt)
        await until(pilot, lambda: not any(b.disabled for b in prompt.query(Button)))


async def test_send_while_running_is_queued_then_committed():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test() as pilot:
        stub.emit(StateChanged(AgentState.RUNNING_TOOLS, pending_ops=2))
        await caught_up(pilot, app, stub)
        await type_text(pilot, "also this")
        await pilot.press("enter")
        await until(pilot, lambda: ("send", "also this") in stub.calls)
        assert app.query_one("#prompt", Input).value == ""

        stub.emit(MessageQueued("q1", "also this"), MessageQueued("q2", "also this"))
        await caught_up(pilot, app, stub)
        first, second = entries(app, UserEntry)
        assert first.queued and "(queued)" in plain(first) and second.queued

        stub.emit(MessageCommitted(msg("q2", Role.USER, Text("also this"))))
        await caught_up(pilot, app, stub)
        assert [e.queued for e in entries(app, UserEntry)] == [True, False]
        stub.emit(MessageCommitted(msg("q1", Role.USER, Text("also this"))))
        await caught_up(pilot, app, stub)
        assert [e.queued for e in entries(app, UserEntry)] == [False, False]


async def test_stop_keeps_ui_responsive_and_reports_progress():
    stub = StubRuntime()
    stub.command_delay = 30.0  # a runtime whose stop() hangs
    app = make(stub)
    async with app.run_test() as pilot:
        stub.emit(StateChanged(AgentState.THINKING), TextDelta("partial"))
        await caught_up(pilot, app, stub)
        async def stop_then_type() -> None:
            await pilot.press("escape")
            await until(pilot, lambda: ("stop",) in stub.calls)
            await type_text(pilot, "still typing")

        await asyncio.wait_for(stop_then_type(), 3.0)  # stop() itself hangs for 30 s
        assert app.query_one("#prompt", Input).value == "still typing"

        stub.emit(StateChanged(AgentState.STOPPING))
        await caught_up(pilot, app, stub)
        assert "Stopping…" in status(app)

        stub.emit(StopStalled(["c-9"]))
        await caught_up(pilot, app, stub)
        assert "c-9" in plain(entries(app, SystemEntry)[-1])
        assert "Some tasks did not stop in time" in status(app)

        stub.emit(StateChanged(AgentState.STOPPED))
        await caught_up(pilot, app, stub)
        assert "Stopped" in status(app)
        [entry] = entries(app, AssistantEntry)
        assert entry.interrupted


async def test_model_selection_dialog_and_command():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+o")
        await until(pilot, lambda: isinstance(app.screen, ModelScreen))
        await pilot.press("down", "enter")
        await until(pilot, lambda: ("select_model", "m2") in stub.calls)
        await until(pilot, lambda: "m2" in status(app))

        await type_text(pilot, "/model m1")
        await pilot.press("enter")
        await until(pilot, lambda: ("select_model", "m1") in stub.calls)


async def test_activate_needs_only_a_started_agent():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test() as pilot:
        await caught_up(pilot, app, stub)
        await pilot.press("ctrl+r")
        await until(pilot, lambda: ("activate",) in stub.calls)
        await type_text(pilot, "/activate")
        await pilot.press("enter")
        await until(pilot, lambda: stub.calls.count(("activate",)) == 2)

    stub = StubRuntime(started=False)
    app = make(stub)
    async with app.run_test(size=(120, 50)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, StartScreen))
        assert app.check_action("activate", ()) is False


async def test_late_attach_restores_history_operations_and_prompts():
    call = ToolCall("c1", "shell", {"command": "sleep 99"})
    ask = ToolCall("c2", "write_file", {"path": "x"})
    stub = StubRuntime(
        history=[
            msg("u1", Role.USER, Text("hi")),
            msg("a1", Role.ASSISTANT, Text("on it"), call, ask),
        ],
        operations=[OperationInfo(call, 0.0, "50%")],
        pending_permissions=[PermissionRequest("p1", ask, read_only=False)],
        queued=[MessageQueued("q1", "later")],
    )
    stub.seq = 41
    app = make(stub)
    async with app.run_test() as pilot:
        await caught_up(pilot, app, stub)
        assert [(e.message_text, e.queued) for e in entries(app, UserEntry)] == [
            ("hi", False), ("later", True)]
        tools = entries(app, ToolEntry)
        assert "running: 50%" in plain(tools[0])
        assert app.query_one(PermissionPrompt).request.id == "p1"

        # A message committed while history was loading is not rendered twice.
        stub.emit(MessageCommitted(msg("a1", Role.ASSISTANT, Text("on it"))))
        await caught_up(pilot, app, stub)
        assert len(entries(app, AssistantEntry)) == 1


async def test_sequence_gap_triggers_resync():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test() as pilot:
        await caught_up(pilot, app, stub)
        stub.history_messages = [msg("u1", Role.USER, Text("from history"))]
        stub.skip(3)
        stub.emit(StateChanged(AgentState.THINKING))
        await caught_up(pilot, app, stub)
        assert [e.message_text for e in entries(app, UserEntry)] == ["from history"]
        assert stub.calls.count(("history", "conv-1")) == 2


async def test_start_screen_starts_a_new_conversation():
    stub = StubRuntime(started=False)
    app = make(stub)
    async with app.run_test(size=(120, 50)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, StartScreen))
        screen = app.screen
        assert not screen.query("#mode") and not screen.query("#activation")
        await until(pilot, lambda: len(screen.query_one("#conversation", Select)._options) == 4)
        assert not screen.query("#autonomous")
        screen.query_one("#seed-from", Select).value = "conv-2"
        await pilot.pause()
        await pilot.click("#start")
        await until(pilot, lambda: any(c[0] == "start" for c in stub.calls))
        [options] = [c[1] for c in stub.calls if c[0] == "start"]
        assert options == StartOptions(seed_from="conv-2")
        await until(pilot, lambda: ("history", "conv-new") in stub.calls)


async def test_start_screen_starts_a_new_conversation_in_another_folder():
    stub = StubRuntime(started=False)
    app = make(stub)
    async with app.run_test(size=(120, 50)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, StartScreen))
        screen = app.screen
        folder = screen.query_one("#folder", Input)
        await until(pilot, lambda: folder.placeholder == "/work (the default)")
        folder.value = "/projects/site"
        await pilot.click("#start")
        await until(pilot, lambda: any(c[0] == "start" for c in stub.calls))
        [options] = [c[1] for c in stub.calls if c[0] == "start"]
        assert options == StartOptions(folder="/projects/site")
        await until(pilot, lambda: "/projects/site" in status(app))


async def test_folder_command_moves_an_empty_chat_and_keeps_one_with_messages():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(120, 50), notifications=True) as pilot:
        await until(pilot, lambda: ("history", "conv-1") in stub.calls)
        await submit(pilot, app, "/folder")
        await until(pilot, lambda: "Kayden works in" in notices(app))
        await submit(pilot, app, "/folder /projects/site")
        await until(pilot, lambda: ("delete_conversation", "conv-1") in stub.calls)
        assert ("start", StartOptions(folder="/projects/site")) in stub.calls
    stub = StubRuntime(history=[Message("u1", Role.USER, (Text("hi"),), 0.0)])
    app = make(stub)
    async with app.run_test(size=(120, 50)) as pilot:
        await until(pilot, lambda: entries(app, UserEntry))
        await submit(pilot, app, "/folder /projects/site")
        await until(pilot, lambda: ("start", StartOptions(folder="/projects/site"))
                    in stub.calls)
        await pilot.pause(0.05)
        assert not any(c[0] == "delete_conversation" for c in stub.calls)


async def test_start_screen_lists_titles_and_resumes_a_conversation():
    stub = StubRuntime(started=False)
    app = make(stub)
    async with app.run_test(size=(120, 50)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, StartScreen))
        screen = app.screen
        select = screen.query_one("#conversation", Select)
        await until(pilot, lambda: len(select._options) == 4)
        labels = [str(prompt) for prompt, _ in select._options]
        # Archived conversations are left out; untitled ones show their preview.
        assert labels == ["New conversation", "Test", "What is the weather",
                          "New chat"]
        select.value = "conv-3"
        await pilot.pause()
        assert screen.query_one("#seed-from", Select).disabled
        assert screen.query_one("#folder", Input).disabled
        await pilot.click("#start")
        await until(pilot, lambda: any(c[0] == "start" for c in stub.calls))
        [options] = [c[1] for c in stub.calls if c[0] == "start"]
        assert options == StartOptions(conversation_id="conv-3")


async def test_start_screen_fits_a_small_terminal():
    stub = StubRuntime(started=False)
    app = make(stub)
    async with app.run_test(size=(80, 24)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, StartScreen))
        start = app.screen.query_one("#start", Button)
        # Scrolling Start into view takes a few refreshes.
        await until(pilot, lambda: app.screen.focused is start
                    and app.screen.region.contains_region(start.region))
        await pilot.press("enter")
        await until(pilot, lambda: any(c[0] == "start" for c in stub.calls))


async def test_start_options_skip_the_start_screen():
    stub = StubRuntime(started=False)
    app = make(stub, start_options=StartOptions(conversation_id="conv-1"))
    async with app.run_test() as pilot:
        await until(pilot, lambda: ("history", "conv-1") in stub.calls)
        assert not isinstance(app.screen, StartScreen)


async def test_usage_failure_and_command_errors_are_shown():
    stub = StubRuntime()
    stub.fail_with["send"] = RuntimeStateError("agent failed")
    app = make(stub)
    async with app.run_test(notifications=True) as pilot:
        snapshot = UsageSnapshot(Usage(1200, 340, cost_usd=0.0123), 2, 5.0, {"tool_calls": 8})
        stub.emit(UsageUpdated(snapshot), Failure(FailureKind.MODEL, "retries exhausted"))
        stub.emit(StateChanged(AgentState.FAILED))
        await caught_up(pilot, app, stub)
        assert "1200↑ 340↓ tok $0.0123" in status(app)
        assert "tool_calls 8" in status(app)
        assert "retries exhausted" in plain(entries(app, SystemEntry)[-1])

        await type_text(pilot, "hello")
        await pilot.press("enter")
        await until(pilot, lambda: any("agent failed" in n.message for n in app._notifications))


async def test_shutting_down_and_closed_stream_exit_the_app():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test() as pilot:
        stub.emit(ShuttingDown("budget exhausted"))
        await until(pilot, lambda: app._exit)

    stub = StubRuntime()
    app = make(stub)
    async with app.run_test() as pilot:
        await caught_up(pilot, app, stub)
        stub.close()
        await until(pilot, lambda: app._exit)


async def test_not_started_state_is_shown():
    stub = StubRuntime(started=False)
    app = make(stub)
    async with app.run_test(size=(120, 50)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, StartScreen))
        assert "Not started" in status(app)


async def test_model_selected_event_updates_the_status():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test() as pilot:
        await caught_up(pilot, app, stub)
        assert "m1" in status(app)
        stub.emit(ModelSelected("m2"))  # e.g. selected by another frontend
        await caught_up(pilot, app, stub)
        assert "m2" in status(app) and "m1" not in status(app)


async def test_refusal_is_shown_distinctly():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test() as pilot:
        stub.emit(MessageCommitted(msg("a1", Role.ASSISTANT, Text("No.")), StopReason.REFUSAL))
        stub.emit(MessageCommitted(msg("a2", Role.ASSISTANT, Text("Yes.")), StopReason.END_TURN))
        await caught_up(pilot, app, stub)
        assert [e.refused for e in entries(app, AssistantEntry)] == [True, False]


async def test_a_permission_decision_adds_no_line_to_the_call():
    stub = StubRuntime()
    app = make(stub)
    call = ToolCall("c1", "read_file", {"path": "x"})
    async with app.run_test() as pilot:
        stub.emit(MessageCommitted(msg("a1", Role.ASSISTANT, call)))
        stub.emit(PermissionResolved("auto-1", Decision.DENY, "c1"))
        await caught_up(pilot, app, stub)
        assert "permission" not in plain(entries(app, ToolEntry)[0]).lower()


async def test_always_allow_is_not_offered():
    stub = StubRuntime()
    app = make(stub)
    call = ToolCall("c1", "shell", {"command": "rm x"})
    async with app.run_test(notifications=True) as pilot:
        stub.emit(PermissionRequested(PermissionRequest("p1", call, read_only=False)))
        await caught_up(pilot, app, stub)
        await type_text(pilot, "/allow always")
        await pilot.press("enter")
        await until(pilot, lambda: any("/allow once" in n.message for n in app._notifications))
        assert not any(c[0] == "resolve_permission" for c in stub.calls)


async def test_every_status_renders():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test() as pilot:
        calls = [ToolCall(f"c-{s}", "shell", {}) for s in ToolStatus]
        stub.emit(MessageCommitted(msg("a1", Role.ASSISTANT, *calls)))
        stub.emit(MessageCommitted(msg("t1", Role.TOOL, *(
            ToolResult(c.id, s, str(s)) for c, s in zip(calls, ToolStatus)))))
        await caught_up(pilot, app, stub)
        rendered = [plain(e) for e in entries(app, ToolEntry)]
        assert len(rendered) == len(ToolStatus)
        assert "abandoned" in rendered[list(ToolStatus).index(ToolStatus.ABANDONED)]


async def test_operation_result_finishes_a_background_call():
    call = ToolCall("c1", "shell", {"command": "make"})
    stub = StubRuntime(history=[
        msg("u1", Role.USER, Text("build")),
        msg("a1", Role.ASSISTANT, call),
        msg("t1", Role.TOOL, ToolResult("c1", ToolStatus.BACKGROUNDED, "running as c1")),
        msg("u2", Role.USER, OperationResult("c1", ToolStatus.OK, "built")),
    ])
    app = make(stub)
    async with app.run_test() as pilot:
        await caught_up(pilot, app, stub)
        [tool] = entries(app, ToolEntry)
        assert "✓ ok" in plain(tool) and "built" in plain(tool)
        assert [e.message_text for e in entries(app, UserEntry)] == ["build"]


async def test_late_attach_shows_backgrounded_calls():
    call = ToolCall("c1", "shell", {"command": "sleep 99"})
    stub = StubRuntime(history=[msg("a1", Role.ASSISTANT, call)],  # placeholder not in history
                       operations=[OperationInfo(call, 0.0, None, backgrounded=True)])
    app = make(stub)
    async with app.run_test() as pilot:
        await caught_up(pilot, app, stub)
        assert "background" in plain(entries(app, ToolEntry)[0])


async def test_cancel_operation_dialog_and_command():
    call = ToolCall("c1", "shell", {"command": "sleep 99"})
    stub = StubRuntime(operations=[OperationInfo(call, 0.0, None)])
    app = make(stub)
    async with app.run_test() as pilot:
        await caught_up(pilot, app, stub)
        await pilot.press("ctrl+k")
        await until(pilot, lambda: isinstance(app.screen, OperationsScreen))
        await pilot.press("enter")
        await until(pilot, lambda: ("cancel_operation", "c1") in stub.calls)

        await type_text(pilot, "/cancel c1")
        await pilot.press("enter")
        await until(pilot, lambda: stub.calls.count(("cancel_operation", "c1")) == 2)


async def test_history_is_paginated():
    from kayden.frontends.tui import HISTORY_PAGE

    stub = StubRuntime(history=[msg(f"u{i}", Role.USER, Text(f"m{i}"))
                                for i in range(2 * HISTORY_PAGE + 10)])
    app = make(stub)
    async with app.run_test() as pilot:
        await caught_up(pilot, app, stub)
        shown = [e.message_text for e in entries(app, UserEntry)]
        assert shown == [f"m{i}" for i in range(HISTORY_PAGE + 10, 2 * HISTORY_PAGE + 10)]

        for expected in (2 * HISTORY_PAGE, 2 * HISTORY_PAGE + 10):
            button = app.query_one("#load-earlier", Button)
            await until(pilot, lambda b=button: not b.disabled)  # a page may still be loading
            button.press()  # scrolled out of view
            await until(pilot, lambda n=expected: len(entries(app, UserEntry)) == n)
        assert [e.message_text for e in entries(app, UserEntry)] == [
            f"m{i}" for i in range(2 * HISTORY_PAGE + 10)]
        await until(pilot, lambda: not app.query("#load-earlier"))


async def test_factory_builds_start_options(tmp_path):
    from kayden.api import InstancePaths
    from kayden.frontends.tui import factory

    paths = InstancePaths(tmp_path)
    assert factory({}, paths).start_options is None
    assert factory({"start": False, "model": "m1"}, paths).start_options is None
    assert factory({"start": True}, paths).start_options == StartOptions()
    frontend = factory({"start": True, "conversation_id": "conv-1", "model": "m2"}, paths)
    assert frontend.start_options == StartOptions("conv-1", "m2")


async def test_conversation_events_for_other_conversations_and_memories():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test() as pilot:
        await caught_up(pilot, app, stub)
        stub.emit(ConversationUpdated(replace(OTHERS[0], title="Weather")),
                  ConversationDeleted("conv-4"), MemoriesChanged())
        stub.emit(ConversationUpdated(replace(CONVERSATION, title="Renamed")))
        await caught_up(pilot, app, stub)
        assert app.conversation is not None
        assert (app.conversation.id, app.conversation.title) == ("conv-1", "Renamed")


# ── conversation list ────────────────────────────────────────────────────


async def open_conversations(pilot: Pilot, app: KaydenApp, stub: StubRuntime,
                             how: str = "f3") -> ConversationsScreen:
    await caught_up(pilot, app, stub)
    if how == "f3":
        await pilot.press("f3")
    else:
        await type_text(pilot, "/conversations")
        await pilot.press("enter")
    await until(pilot, lambda: isinstance(app.screen, ConversationsScreen))
    screen = app.screen
    assert isinstance(screen, ConversationsScreen)
    await until(pilot, lambda: screen.conversations is not None)
    return screen


def listed(screen: ConversationsScreen) -> list[str]:
    options = screen.query_one("#conversations", OptionList)
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


def highlight(screen: ConversationsScreen | MemoryScreen, option_id: str) -> None:
    options = screen.query_one(OptionList)
    options.highlighted = options.get_option_index(option_id)


async def test_conversation_list_shows_titles_and_opens_one():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(120, 50)) as pilot:
        screen = await open_conversations(pilot, app, stub, how="command")
        assert listed(screen) == ["● Test", "  What is the weather",
                                  "  New chat"]
        screen.query_one("#show-archived", Switch).value = True
        await until(pilot, lambda: len(listed(screen)) == 4)
        assert listed(screen)[-1] == "  Old plans  · archived"

        highlight(screen, "conv-2")
        screen.query_one("#open", Button).press()
        await until(pilot, lambda: ("start", StartOptions(conversation_id="conv-2"))
                    in stub.calls)
        await until(pilot, lambda: ("history", "conv-2") in stub.calls)
        assert not isinstance(app.screen, ConversationsScreen)


async def test_conversation_cannot_be_switched_while_the_agent_runs():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(120, 50), notifications=True) as pilot:
        stub.emit(StateChanged(AgentState.THINKING))
        screen = await open_conversations(pilot, app, stub)
        highlight(screen, "conv-2")
        screen.query_one("#open", Button).press()
        await until(pilot, lambda: any("stop the current task" in n.message for n in app._notifications))
        assert not any(c[0] == "start" for c in stub.calls)


async def test_new_conversation_from_the_list_uses_the_start_screen():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(120, 50)) as pilot:
        screen = await open_conversations(pilot, app, stub)
        screen.query_one("#new", Button).press()
        await until(pilot, lambda: isinstance(app.screen, StartScreen))
        await pilot.press("escape")  # cancellable when an agent is already running
        await until(pilot, lambda: not isinstance(app.screen, StartScreen))
        assert not any(c[0] == "start" for c in stub.calls)


async def test_rename_and_archive_a_conversation():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(120, 50)) as pilot:
        screen = await open_conversations(pilot, app, stub)
        highlight(screen, "conv-2")
        screen.query_one("#rename", Button).press()
        await until(pilot, lambda: isinstance(app.screen, TextPromptScreen))
        app.screen.query_one(Input).value = "Weather"
        await pilot.press("enter")
        await until(pilot, lambda: ("rename_conversation", "conv-2", "Weather") in stub.calls)
        await until(pilot, lambda: "  Weather" in listed(screen))

        highlight(screen, "conv-2")
        screen.query_one("#archive", Button).press()
        await until(pilot, lambda: ("archive_conversation", "conv-2", True) in stub.calls)
        await until(pilot, lambda: "  Weather" not in listed(screen))

        screen.query_one("#show-archived", Switch).value = True
        await until(pilot, lambda: "  Weather  · archived" in listed(screen))
        highlight(screen, "conv-2")
        await pilot.pause()
        assert str(screen.query_one("#archive", Button).label) == "Unarchive"
        screen.query_one("#archive", Button).press()
        await until(pilot, lambda: ("archive_conversation", "conv-2", False) in stub.calls)


async def test_delete_a_conversation_after_confirmation():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(120, 50), notifications=True) as pilot:
        screen = await open_conversations(pilot, app, stub)
        highlight(screen, "conv-2")
        screen.query_one("#delete", Button).press()
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        message = plain(app.screen.query_one("#confirm-message"))
        assert message == ('Delete "What is the weather"? This permanently deletes the '
                           "conversation. What Kayden remembered from it stays in Memory.")
        await pilot.press("escape")
        await until(pilot, lambda: app.screen is screen)
        assert not any(c[0] == "delete_conversation" for c in stub.calls)

        screen.query_one("#delete", Button).press()
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        app.screen.query_one("#confirm-yes", Button).press()
        await until(pilot, lambda: ("delete_conversation", "conv-2") in stub.calls)
        await until(pilot, lambda: "  What is the weather" not in listed(screen))


async def test_deleting_the_open_conversation_opens_a_new_chat():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(120, 50)) as pilot:
        screen = await open_conversations(pilot, app, stub)
        highlight(screen, "conv-1")  # the open conversation
        screen.query_one("#delete", Button).press()
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        assert "A new chat will open." in plain(app.screen.query_one("#confirm-message"))
        app.screen.query_one("#confirm-yes", Button).press()
        await until(pilot, lambda: ("delete_conversation", "conv-1") in stub.calls)
        starts = [i for i, c in enumerate(stub.calls) if c[0] == "start"]
        assert starts[-1] < stub.calls.index(("delete_conversation", "conv-1"))
        assert app.conversation.id == "conv-new"
        await until(pilot, lambda: not any("conv-1" in line for line in listed(screen)))


async def test_delete_is_disabled_when_memory_cannot_delete():
    stub = StubRuntime()
    stub.fail_with["delete_conversation"] = NotImplementedError(
        "this memory module cannot delete conversations")
    app = make(stub)
    async with app.run_test(size=(120, 50), notifications=True) as pilot:
        screen = await open_conversations(pilot, app, stub)
        highlight(screen, "conv-2")
        screen.query_one("#delete", Button).press()
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        app.screen.query_one("#confirm-yes", Button).press()
        await until(pilot, lambda: screen.query_one("#delete", Button).disabled)
        assert any("cannot delete" in n.message for n in app._notifications)


# ── long-term memory ─────────────────────────────────────────────────────


async def open_memory(pilot: Pilot, app: KaydenApp, stub: StubRuntime) -> MemoryScreen:
    await caught_up(pilot, app, stub)
    await pilot.press("f4")
    await until(pilot, lambda: isinstance(app.screen, MemoryScreen))
    screen = app.screen
    assert isinstance(screen, MemoryScreen)
    return screen


def memory_rows(screen: MemoryScreen) -> list[str]:
    options = screen.query_one("#memories", OptionList)
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


async def test_memory_screen_adds_edits_and_deletes_notes():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test(size=(120, 50), notifications=True) as pilot:
        screen = await open_memory(pilot, app, stub)
        assert memory_rows(screen) == [f"{PROFILE}  · Who the user is",
                                       "Trip  · The Oslo trip"]

        screen.query_one("#add", Button).press()
        await until(pilot, lambda: isinstance(app.screen, NoteScreen))
        app.screen.query_one("#note-title", Input).value = "Units"
        app.screen.query_one("#note-description", Input).value = "Measurements"
        app.screen.query_one("#note-text", TextArea).text = "Prefers metric units"
        app.screen.query_one("#save", Button).press()
        await until(pilot, lambda: ("save_note", "Units", "Measurements",
                                    "Prefers metric units", None) in stub.calls)
        await until(pilot, lambda: len(memory_rows(screen)) == 3)

        highlight(screen, "1")
        screen.query_one("#edit", Button).press()
        await until(pilot, lambda: isinstance(app.screen, NoteScreen))
        assert app.screen.query_one("#note-text", TextArea).text == "Likes tea"
        assert app.screen.query_one("#note-title", Input).disabled  # the profile's title stays
        app.screen.query_one("#note-text", TextArea).text = "Likes green tea"
        app.screen.query_one("#save", Button).press()
        await until(pilot, lambda: ("save_note", PROFILE, "Who the user is", "Likes green tea",
                                    "1") in stub.calls)

        highlight(screen, "1")
        screen.query_one("#forget", Button).press()  # the profile cannot be deleted
        await until(pilot, lambda: any("can be emptied" in n.message
                                       for n in app._notifications))
        highlight(screen, "2")
        screen.query_one("#forget", Button).press()
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        message = plain(app.screen.query_one("#confirm-message"))
        assert "Trip" in message and "stay until you delete their conversation" in message
        app.screen.query_one("#confirm-yes", Button).press()
        await until(pilot, lambda: ("delete_note", "2") in stub.calls)
        await until(pilot, lambda: len(memory_rows(screen)) == 2)

        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, MemoryScreen))
        await type_text(pilot, "/memory")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, MemoryScreen))


async def test_memory_screen_unavailable_without_editable_memory():
    stub = StubRuntime(editable_memory=False)
    app = make(stub)
    async with app.run_test(notifications=True) as pilot:
        await caught_up(pilot, app, stub)
        await pilot.press("f4")
        await until(pilot, lambda: any("cannot edit memories" in n.message
                                       for n in app._notifications))
        assert not isinstance(app.screen, MemoryScreen)


async def test_a_command_shows_its_description_with_the_command_behind_it():
    stub = StubRuntime()
    app = make(stub)
    call = ToolCall("c1", "shell", {"command": "rm -rf build",
                                    "step_summary": "Remove the old build"})
    async with app.run_test(size=(120, 40)) as pilot:
        stub.emit(ToolStarted(call),
                  PermissionRequested(PermissionRequest("p1", call, read_only=False)))
        await caught_up(pilot, app, stub)
        [entry] = entries(app, ToolEntry)
        shown = plain(entry)
        assert shown.startswith("⚙ Remove the old build\n  rm -rf build")
        prompt = str(app.query_one(PermissionPrompt).query_one(Label).render())
        assert "Kayden wants to: Remove the old build" in prompt and "rm -rf build" in prompt


async def test_a_reply_with_nothing_to_show_takes_no_space():
    history = [msg("u1", Role.USER, Text("I study CS")),
               msg("a1", Role.ASSISTANT, Reasoning(""),
                   ToolCall("c1", "remember", {"text": "Studies CS"})),
               msg("t1", Role.TOOL, ToolResult("c1", ToolStatus.OK, "Remembered"))]
    stub = StubRuntime(history=history)
    app = make(stub)
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: entries(app, ToolEntry))
        assert [e.display for e in entries(app, AssistantEntry)] in ([], [False])
        [tool] = entries(app, ToolEntry)
        assert plain(tool).startswith("⚙ remember Studies CS")  # no description given


def look(id: str, description: str) -> ToolCall:
    return ToolCall(id, "look", {"q": "x", "step_summary": description})


async def test_steps_fold_into_one_group_with_the_thinking_before_them():
    stub = StubRuntime()
    app = make(stub)
    a, b = look("c1", "Search the web"), look("c2", "Open the page")
    async with app.run_test() as pilot:
        stub.emit(StateChanged(AgentState.THINKING), ReasoningDelta("Let me look."))
        stub.emit(ToolCallDelta("c1", "look", json.dumps(a.arguments)))
        await caught_up(pilot, app, stub)
        [group] = entries(app, StepGroup)
        assert group.collapsed and group.title == "Search the web · waiting"
        assert [e.reasoning for e in group.query(AssistantEntry)] == ["Let me look."]
        stub.emit(MessageCommitted(msg("a1", Role.ASSISTANT, Reasoning("Let me look."), a)))
        stub.emit(ToolStarted(a), ToolFinished(ToolResult("c1", ToolStatus.OK, "found"), 0.1))
        stub.emit(MessageCommitted(msg("t1", Role.TOOL, ToolResult("c1", ToolStatus.OK, "found"))))
        stub.emit(MessageCommitted(msg("a2", Role.ASSISTANT, Reasoning("Next."), b)))
        stub.emit(ToolStarted(b))
        await caught_up(pilot, app, stub)
        [group] = entries(app, StepGroup)
        assert group.title == "Open the page · running"
        failed = ToolResult("c2", ToolStatus.ERROR, "blocked")
        stub.emit(ToolFinished(failed, 0.1), MessageCommitted(msg("t2", Role.TOOL, failed)))
        stub.emit(MessageCommitted(msg("a3", Role.ASSISTANT, Text("Here it is."))))
        await caught_up(pilot, app, stub)
        [group] = entries(app, StepGroup)
        assert group.title == "2 steps · 1 failed"
        assert len(group.query(ToolEntry)) == 2 and len(group.query(AssistantEntry)) == 2
        [reply] = [e for e in entries(app, AssistantEntry) if e.body]
        assert reply.parent is app.query_one("#transcript")


async def test_a_lone_step_shows_its_sentence_and_text_starts_a_new_group():
    stub = StubRuntime()
    app = make(stub)
    a, b = look("c1", "Search the web"), look("c2", "Search again")
    ok = ToolResult("c1", ToolStatus.OK, "found")
    async with app.run_test() as pilot:
        stub.emit(MessageCommitted(msg("a1", Role.ASSISTANT, a)), ToolFinished(ok, 0.1))
        stub.emit(MessageCommitted(msg("t1", Role.TOOL, ok)))
        stub.emit(MessageCommitted(msg("a2", Role.ASSISTANT, Text("One more."), b)))
        await caught_up(pilot, app, stub)
        first, second = entries(app, StepGroup)
        assert first.title == "Searched the web"
        assert second.title == "Search again · waiting"


async def test_a_tool_the_provider_ran_is_a_finished_step():
    from kayden.api import ServerToolUse

    stub = StubRuntime()
    app = make(stub)
    use = ServerToolUse("srv_1", "web_search", {"query": "worlds"},
                        "Searched the web for “worlds”", "Worlds 2026\nhttps://w.example")
    async with app.run_test() as pilot:
        stub.emit(MessageCommitted(msg("a1", Role.ASSISTANT, use, Text("Found it."))))
        await caught_up(pilot, app, stub)
        [group] = entries(app, StepGroup)
        assert group.title == "Searched the web for “worlds”"
        [step] = entries(app, ToolEntry)
        assert "w.example" in plain(step)


async def test_a_second_kayden_says_it_is_already_open_and_quits():
    from dataclasses import replace

    stub = StubRuntime(started=False)
    stub.current_settings = replace(stub.current_settings, already_open=True,
                                    startup_error="Kayden is already open in another window")
    app = make(stub)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.return_code is not None)
        assert app.return_code == 1
        assert not any(isinstance(s, SettingsScreen) for s in app.screen_stack)


async def test_a_memory_update_after_an_answer_is_mentioned():
    stub = StubRuntime()
    app = make(stub)
    async with app.run_test() as pilot:
        stub.emit(MemoriesChanged(saved=("Trip", PROFILE)))
        stub.emit(MemoriesChanged())  # the user's own edit: nothing to mention
        await caught_up(pilot, app, stub)
        [line] = entries(app, SystemEntry)
        assert plain(line) == f"Memory updated: Trip, {PROFILE} (F4 to see it)"
