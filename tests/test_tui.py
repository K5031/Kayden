"""TUI tests against a stub RuntimeAPI that replays scripted event sequences."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Sequence

import pytest
from rich.text import Text as RichText
from textual.pilot import Pilot
from textual.widgets import Button, Input, Static

from kayden.api import (
    Activation,
    AgentConfig,
    AgentState,
    Budget,
    ConversationInfo,
    Decision,
    EventEnvelope,
    Failure,
    FailureKind,
    Message,
    MessageCommitted,
    MessageQueued,
    Mode,
    ModelInfo,
    ModelRetry,
    OperationInfo,
    PermissionPolicy,
    PermissionRequest,
    PermissionRequested,
    PermissionResolved,
    ReasoningDelta,
    Role,
    RuntimeEvent,
    RuntimeSnapshot,
    RuntimeStateError,
    ShuttingDown,
    StartOptions,
    StateChanged,
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
    KaydenApp,
    ModelScreen,
    PermissionPrompt,
    StartScreen,
    SystemEntry,
    ToolEntry,
    TuiFrontend,
    UserEntry,
)

CONVERSATION = ConversationInfo("conv-1", Mode.SESSION, "Test", 0.0, 0.0)
MODELS = [ModelInfo("m1", "Model One", 100_000), ModelInfo("m2", "Model Two", 200_000)]


class StubRuntime:
    """Scriptable RuntimeAPI: tests emit events and inspect the recorded commands."""

    def __init__(
        self,
        *,
        started: bool = True,
        mode: Mode = Mode.SESSION,
        activation: Activation = Activation.USER_CONTROLLED,
        history: Sequence[Message] = (),
        operations: Sequence[OperationInfo] = (),
        pending_permissions: Sequence[PermissionRequest] = (),
    ) -> None:
        self.seq = 0
        self.state = AgentState.IDLE
        self.conversation = CONVERSATION if started else None
        self.config = AgentConfig(mode, activation, "m1", Budget(), PermissionPolicy.ASK)
        self.history_messages = list(history)
        self.operations = list(operations)
        self.pending_permissions = list(pending_permissions)
        self.calls: list[tuple] = []
        self.command_delay = 0.0
        self.fail_with: dict[str, Exception] = {}
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
            usage=UsageSnapshot(Usage(), 0, 0.0),
            operations=list(self.operations),
            pending_permissions=list(self.pending_permissions),
        )

    async def list_models(self) -> Sequence[ModelInfo]:
        return MODELS

    async def list_tools(self):
        return []

    async def list_conversations(self, mode: Mode) -> Sequence[ConversationInfo]:
        return [CONVERSATION] if mode is Mode.SESSION else []

    async def history(self, conversation_id: str) -> Sequence[Message]:
        self.calls.append(("history", conversation_id))
        return list(self.history_messages)

    async def search_archive(self, query, *, mode=None, limit=10):
        return []

    async def inspect_memory(self, conversation_id):
        return None

    async def start(self, options: StartOptions) -> ConversationInfo:
        await self._record("start", options)
        self.conversation = CONVERSATION
        return CONVERSATION

    async def select_model(self, model_id: str) -> None:
        await self._record("select_model", model_id)

    async def send(self, text: str) -> None:
        await self._record("send", text)

    async def activate(self) -> None:
        await self._record("activate")

    async def stop(self) -> None:
        await self._record("stop")

    async def resolve_permission(self, request_id: str, decision: Decision) -> None:
        await self._record("resolve_permission", request_id, decision)


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
        assert "Idle" in status(app)


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
        assert "model retry 1/3" in status(app)


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
        assert "Running tools… · 1 ops" in status(app)

        stub.emit(ToolBackgrounded("c1", "op-7"))
        await caught_up(pilot, app, stub)
        assert "background op-7" in plain(tool)

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
        assert "Awaiting permission" in status(app)
        prompt = app.query_one(PermissionPrompt)
        assert {b.id for b in prompt.query(Button)} == {d.value for d in Decision}

        await pilot.click(f"#{decision.value}")
        await until(pilot, lambda: ("resolve_permission", "p1", decision) in stub.calls)
        assert all(b.disabled for b in prompt.query(Button))

        stub.emit(PermissionResolved("p1", decision), StateChanged(AgentState.RUNNING_TOOLS))
        await caught_up(pilot, app, stub)
        assert not app.query(PermissionPrompt)
        assert "permission:" in plain(entries(app, ToolEntry)[0])


async def test_permission_commands_and_runtime_side_resolution():
    stub = StubRuntime()
    app = make(stub)
    call = ToolCall("c1", "shell", {"command": "rm x"})
    async with app.run_test() as pilot:
        stub.emit(PermissionRequested(PermissionRequest("p1", call, read_only=False)))
        stub.emit(PermissionRequested(PermissionRequest("p2", call, read_only=False)))
        await caught_up(pilot, app, stub)
        await type_text(pilot, "/allow always")
        await pilot.press("enter")
        await until(pilot, lambda: ("resolve_permission", "p1", Decision.ALLOW_ALWAYS) in stub.calls)

        # Stop denies pending prompts runtime-side; the prompt must disappear.
        stub.emit(PermissionResolved("p1", Decision.ALLOW_ALWAYS))
        stub.emit(PermissionResolved("p2", Decision.DENY))
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

        stub.emit(MessageQueued("also this"))
        await caught_up(pilot, app, stub)
        [entry] = entries(app, UserEntry)
        assert entry.queued and "(queued)" in plain(entry)

        stub.emit(MessageCommitted(msg("u2", Role.USER, Text("also this"))))
        await caught_up(pilot, app, stub)
        [entry] = entries(app, UserEntry)
        assert not entry.queued


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

        stub.emit(StopStalled(["op-1"]))
        await caught_up(pilot, app, stub)
        assert "op-1" in plain(entries(app, SystemEntry)[-1])
        assert "stop stalled" in status(app)

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


async def test_activate_for_continuous_user_controlled_agent():
    stub = StubRuntime(mode=Mode.CONTINUOUS)
    app = make(stub)
    async with app.run_test() as pilot:
        await caught_up(pilot, app, stub)
        assert "continuous/user" in status(app)
        await pilot.press("ctrl+r")
        await until(pilot, lambda: ("activate",) in stub.calls)
        await type_text(pilot, "/activate")
        await pilot.press("enter")
        await until(pilot, lambda: stub.calls.count(("activate",)) == 2)


async def test_activate_binding_disabled_in_session_mode():
    stub = StubRuntime(mode=Mode.SESSION)
    app = make(stub)
    async with app.run_test() as pilot:
        await caught_up(pilot, app, stub)
        await pilot.press("ctrl+r")
        await pilot.pause(0.05)
        assert ("activate",) not in stub.calls


async def test_late_attach_restores_history_operations_and_prompts():
    call = ToolCall("c1", "shell", {"command": "sleep 99"})
    ask = ToolCall("c2", "write_file", {"path": "x"})
    stub = StubRuntime(
        history=[
            msg("u1", Role.USER, Text("hi")),
            msg("a1", Role.ASSISTANT, Text("on it"), call, ask),
        ],
        operations=[OperationInfo("op-1", call, 0.0, "50%")],
        pending_permissions=[PermissionRequest("p1", ask, read_only=False)],
    )
    stub.seq = 41
    app = make(stub)
    async with app.run_test() as pilot:
        await caught_up(pilot, app, stub)
        assert [e.message_text for e in entries(app, UserEntry)] == ["hi"]
        tools = entries(app, ToolEntry)
        assert "background op-1: 50%" in plain(tools[0])
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


async def test_start_screen_starts_the_agent():
    stub = StubRuntime(started=False)
    app = make(stub)
    async with app.run_test(size=(120, 50)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, StartScreen))
        app.screen.query_one("#mode").value = Mode.CONTINUOUS
        app.screen.query_one("#activation").value = Activation.AGENT_CONTROLLED
        await pilot.pause()
        await pilot.click("#start")
        await until(pilot, lambda: any(c[0] == "start" for c in stub.calls))
        [options] = [c[1] for c in stub.calls if c[0] == "start"]
        assert options == StartOptions(Mode.CONTINUOUS, Activation.AGENT_CONTROLLED, None, None)
        await until(pilot, lambda: ("history", "conv-1") in stub.calls)


async def test_start_options_skip_the_start_screen():
    stub = StubRuntime(started=False)
    app = make(stub, start_options=StartOptions(Mode.SESSION, conversation_id="conv-1"))
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
