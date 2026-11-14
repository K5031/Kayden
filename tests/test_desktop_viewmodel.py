"""The desktop app's view model, against the runtime with scripted fakes (no display)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from kadence.api import (
    PROFILE,
    AgentState,
    Budget,
    ConversationInfo,
    Decision,
    EventEnvelope,
    Failure,
    FailureKind,
    InstanceLockedError,
    MaintenanceFinished,
    MaintenanceStarted,
    MemoryNote,
    Message,
    MessageCommitted,
    ModuleChoice,
    ModuleInfo,
    PermissionPolicy,
    PermissionRequested,
    ReasoningDelta,
    Role,
    RuntimeSnapshot,
    RuntimeStateError,
    Settings,
    SettingsUpdate,
    ShuttingDown,
    StateChanged,
    StopReason,
    Text,
    TextDelta,
    ToolCall,
    ToolResult,
    ToolStatus,
    TransientCoreError,
    Usage,
    UsageSnapshot,
)
from kadence.frontends.desktop.viewmodel import (
    BUDGET_MESSAGE,
    BUSY_MESSAGE,
    LABEL_CHARS,
    NO_DELETE_MESSAGE,
    AssistantItem,
    Closed,
    Controller,
    ConversationListChanged,
    ConversationModel,
    MemoryListChanged,
    NoticeItem,
    Phase,
    ToolItem,
    UserItem,
    conversation_label,
    conversation_preview,
    form_from,
    memory_source,
    needs_welcome,
    newest_first,
    relative_date,
    settings_update,
    user_message,
)
from tests.conftest import Harness, eventually
from tests.fakes.core import Step, call
from tests.fakes.settings import settings as fake_settings


async def attach(h: Harness) -> tuple[ConversationModel, Controller, asyncio.Task[None]]:
    model = ConversationModel()
    controller = Controller(h.runtime, model)
    task = asyncio.create_task(controller.run())
    await settle(h, controller)
    return model, controller, task


async def settle(h: Harness, controller: Controller) -> None:
    await eventually(lambda: controller.caught_up(h.runtime.snapshot().seq))


async def until_state(h: Harness, controller: Controller, state: AgentState) -> None:
    await eventually(lambda: controller.model.state is state
                     and controller.caught_up(h.runtime.snapshot().seq))


def of[T](model: ConversationModel, kind: type[T]) -> list[T]:
    return [item for item in model.items if isinstance(item, kind)]


async def test_a_turn_with_reasoning_and_a_tool(make):
    h: Harness = await make()
    h.tools.instant("look", "found it")
    h.core.add(Step(reasoning="hmm", text="Let me look.", calls=[call("look", {"q": "x"})]),
               Step(text="**Done.**"))
    await h.start()
    model, controller, _ = await attach(h)
    controller.send("find x")
    await eventually(lambda: len(of(model, AssistantItem)) == 2 and not model.busy)
    await settle(h, controller)
    user, first, tool, last = model.items
    assert isinstance(user, UserItem) and user.text == "find x" and not user.queued
    assert isinstance(first, AssistantItem) and first.final
    assert (first.reasoning, first.text) == ("hmm", "Let me look.")
    assert isinstance(tool, ToolItem) and tool.name == "look" and tool.summary == "x"
    assert tool.status is ToolStatus.OK and tool.state_label == "done"
    assert tool.content.startswith("found it") and tool.duration_s is not None
    assert isinstance(last, AssistantItem) and last.text == "**Done.**"
    assert model.status_text == "Ready"
    assert model.spending_text.endswith("spent")


async def test_permission_prompt_is_answered_and_removed(make):
    h: Harness = await make()
    h.tools.instant("write", read_only=False)
    h.core.add(Step(calls=[call("write", {"path": "a"})]), Step(text="written"))
    await h.start()
    model, controller, _ = await attach(h)
    controller.send("write a")
    await eventually(lambda: model.prompts)
    assert model.status_text == "Waiting for you"
    (request,) = model.prompts.values()
    assert request.call.name == "write" and not request.read_only
    (waiting,) = of(model, ToolItem)  # nothing runs until the user answers
    assert waiting.phase is Phase.WAITING and waiting.state_label == "waiting"
    assert not waiting.cancellable
    controller.resolve(request.id, Decision.ALLOW_ONCE)
    await eventually(lambda: not model.prompts and not model.busy)
    (tool,) = of(model, ToolItem)
    assert tool.status is ToolStatus.OK and not hasattr(tool, "notes")  # no permission line


async def test_denied_call_is_shown_as_declined(make):
    h: Harness = await make()
    h.tools.instant("write", read_only=False)
    h.core.add(Step(calls=[call("write")]), Step(text="ok"))
    await h.start()
    model, controller, _ = await attach(h)
    controller.send("write")
    await eventually(lambda: model.prompts)
    controller.resolve(next(iter(model.prompts)), Decision.DENY)
    await eventually(lambda: not model.busy and of(model, ToolItem)[0].status is not None)
    assert of(model, ToolItem)[0].state_label == "declined"


async def test_message_sent_while_busy_is_queued_until_committed(make):
    h: Harness = await make()
    h.core.add(Step(text="a slow answer", chunks=5, delay=0.05), Step(text="second"))
    await h.start()
    model, controller, _ = await attach(h)
    controller.send("first")
    await eventually(lambda: model.busy)
    controller.send("also this")
    await eventually(lambda: any(u.queued for u in of(model, UserItem)))
    assert model.busy  # Send is Stop meanwhile
    await eventually(lambda: not model.busy and len(of(model, AssistantItem)) == 2)
    assert [(u.text, u.queued) for u in of(model, UserItem)] == [("first", False),
                                                                 ("also this", False)]


async def test_retry_discards_the_partial_stream(make):
    h: Harness = await make()
    h.core.add(Step(text="partial", error=TransientCoreError("busy")), Step(text="final"))
    await h.start()
    model, controller, _ = await attach(h)
    notices: list[str] = []
    model.on_change = lambda change: notices.append(model.notice)
    controller.send("hi")
    await eventually(lambda: not model.busy and of(model, AssistantItem))
    await settle(h, controller)
    assert [a.text for a in of(model, AssistantItem)] == ["final"]
    assert "Retrying (2 of 4)…" in notices


async def test_background_task_shows_in_the_status_and_finishes(make):
    h: Harness = await make()
    h.tools.slow("build", 0.6)
    h.core.add(Step(calls=[call("build")]), Step(text="started"))
    await h.start()
    model, controller, _ = await attach(h)
    controller.send("build it")
    await eventually(lambda: of(model, ToolItem) and of(model, ToolItem)[0].phase
                     is Phase.BACKGROUND)
    tool = of(model, ToolItem)[0]
    assert tool.cancellable
    await eventually(lambda: "1 task running in the background" in model.status_text)
    await eventually(lambda: tool.status is ToolStatus.OK, timeout=5)
    assert not tool.cancellable and tool.progress.startswith("build")
    assert "background" not in model.status_text


async def test_cancel_a_running_tool(make):
    h: Harness = await make()
    h.tools.hanging("wait")
    h.core.add(Step(calls=[call("wait")]), Step(text="ok"))
    await h.start()
    model, controller, _ = await attach(h)
    controller.send("wait")
    await eventually(lambda: of(model, ToolItem) and of(model, ToolItem)[0].cancellable)
    controller.cancel(of(model, ToolItem)[0].call_id)
    await eventually(lambda: of(model, ToolItem)[0].status is ToolStatus.CANCELLED)


async def test_stop_with_a_call_that_ignores_cancellation(make):
    h: Harness = await make()
    h.tools.hanging("stuck", honor_cancel=False)
    h.core.add(Step(calls=[call("stuck")]))
    await h.start()
    model, controller, _ = await attach(h)
    controller.send("go")
    await eventually(lambda: of(model, ToolItem) and of(model, ToolItem)[0].cancellable)
    controller.stop()
    assert model.notice == "Stop requested"
    await until_state(h, controller, AgentState.STOPPED)
    assert of(model, ToolItem)[0].status is ToolStatus.ABANDONED
    assert any(n.severity == "warning" and "may still be running" in n.text
               for n in of(model, NoticeItem))


async def test_attach_mid_cycle_restores_tools_prompts_and_queue(make):
    h: Harness = await make()
    h.tools.slow("index", 30, steps=300)
    h.tools.instant("write", read_only=False)
    h.core.add(Step(calls=[call("index")]), Step(calls=[call("write")]), Step(text="ok"))
    await h.start()
    await h.runtime.send("index and write")
    await h.rec.wait_for(lambda r: r.of(PermissionRequested))
    await h.runtime.send("queued meanwhile")
    await eventually(lambda: any(op.last_progress for op in h.runtime.snapshot().operations))
    model, controller, _ = await attach(h)
    index, write = of(model, ToolItem)
    assert index.phase is Phase.BACKGROUND and index.progress.startswith("index")
    assert write.name == "write" and write.status is None
    assert len(model.prompts) == 1 and model.state is AgentState.AWAITING_PERMISSION
    assert [(u.text, u.queued) for u in of(model, UserItem)] == [
        ("index and write", False), ("queued meanwhile", True)]
    controller.resolve(next(iter(model.prompts)), Decision.ALLOW_ONCE)
    await eventually(lambda: not model.prompts and write.status is ToolStatus.OK)
    await eventually(lambda: not of(model, UserItem)[1].queued)


async def test_load_earlier_pages_back(make):
    h: Harness = await make()
    await h.start()
    for i in range(3):
        h.core.add(Step(text=f"reply {i}"))
        await h.turn(f"message {i}")
    model, controller, _ = await attach(h)
    assert len(model.items) == 6 and not model.has_earlier
    model.load(h.runtime.snapshot(), (await h.transcript())[-2:], page=2)
    assert [i.text for i in model.items] == ["message 2", "reply 2"] and model.has_earlier
    controller.load_earlier()
    await eventually(lambda: len(model.items) == 6)
    assert [i.text for i in model.items][:2] == ["message 0", "reply 0"]


async def test_opening_a_conversation_resets_the_view(make):
    h: Harness = await make()
    first = await h.start()
    await h.turn("in the first")
    model, controller, _ = await attach(h)
    assert controller.can_switch() is None
    await controller.open_conversation(None)
    await eventually(lambda: model.conversation is not None
                     and model.conversation.id != first.id)
    assert model.items == []
    listed = await controller.conversations()
    assert [c.id for c in listed][-1] == first.id
    await controller.open_conversation(first.id)
    await eventually(lambda: model.conversation.id == first.id and len(model.items) == 2)


async def test_switching_is_refused_while_busy(make):
    h: Harness = await make()
    h.tools.hanging("wait")
    h.core.add(Step(calls=[call("wait")]))
    await h.start()
    model, controller, _ = await attach(h)
    controller.send("wait")
    await eventually(lambda: of(model, ToolItem))
    assert controller.can_switch() == BUSY_MESSAGE
    await controller.open_conversation(None)
    assert of(model, NoticeItem)[-1].text == BUSY_MESSAGE


async def test_shutting_down_closes_the_view(make):
    h: Harness = await make()
    await h.start()
    model, _, task = await attach(h)
    closed: list[str] = []
    model.on_change = lambda change: isinstance(change, Closed) and closed.append(change.reason)
    await h.runtime.aclose()
    await asyncio.wait_for(task, 5)
    assert model.closed and len(closed) == 1


# ── conversations and long-term memory ───────────────────────────────────


def changes(model: ConversationModel) -> list[object]:
    seen: list[object] = []
    model.on_change = seen.append
    return seen


async def test_continue_from(make):
    h: Harness = await make()
    first = await h.start()
    await h.turn("plan a trip")
    model, controller, _ = await attach(h)
    await controller.open_conversation(None, seed_from=first.id)
    await eventually(lambda: model.conversation.seed_from == first.id)
    listed = await controller.conversations()
    assert listed[-1].id == first.id and listed[-1].preview == "plan a trip"


async def test_rename_and_archive_keep_the_list_current(make):
    h: Harness = await make()
    other = await h.chat()
    await h.start()
    model, controller, _ = await attach(h)
    seen = changes(model)
    await controller.rename(other.id, "  Trip plans ")
    await eventually(lambda: any(isinstance(c, ConversationListChanged) for c in seen))
    assert model.conversation.id != other.id  # the open one is untouched
    (renamed,) = [c for c in await controller.conversations() if c.id == other.id]
    assert renamed.title == "Trip plans"
    await controller.rename(other.id, "")
    assert next(c for c in await controller.conversations() if c.id == other.id).title is None
    await controller.archive(other.id, True)
    assert other.id not in [c.id for c in await controller.conversations()]
    (archived,) = [c for c in await controller.conversations(include_archived=True)
                   if c.id == other.id]
    assert archived.archived
    await controller.archive(other.id, False)
    assert other.id in [c.id for c in await controller.conversations()]


async def test_delete_erases_another_conversation(make):
    h: Harness = await make()
    other = await h.chat()
    current = await h.chat()
    model, controller, _ = await attach(h)
    seen = changes(model)
    await controller.delete(other.id)
    await eventually(lambda: any(isinstance(c, ConversationListChanged) for c in seen))
    assert [c.id for c in await controller.conversations()] == [current.id]
    assert controller.can_delete


async def test_deleting_the_open_conversation_opens_a_new_chat(make):
    h: Harness = await make()
    other = await h.chat()
    current = await h.chat()
    model, controller, _ = await attach(h)
    await controller.delete(current.id)
    await eventually(lambda: model.conversation is not None
                     and model.conversation.id not in (current.id, other.id))
    ids = [c.id for c in await controller.conversations()]
    assert ids == [other.id]  # the new chat is stored once used


async def test_unsupported_memory_hides_its_controls(make):
    h: Harness = await make(memory_kw={"editable": False})
    other = await h.start()
    await h.start()
    model, controller, _ = await attach(h)
    assert await controller.notes() is None
    assert controller.can_delete
    await controller.delete(other.id)
    assert not controller.can_delete
    assert of(model, NoticeItem)[-1].text == NO_DELETE_MESSAGE


async def test_memory_notes_add_edit_delete(make):
    h: Harness = await make()
    await h.start()
    model, controller, _ = await attach(h)
    assert [n.title for n in await controller.notes()] == [PROFILE]
    seen = changes(model)
    await controller.save_note("Trip", "The Oslo trip", "  Leaves Friday ")
    await eventually(lambda: any(isinstance(c, MemoryListChanged) for c in seen))
    _profile, note = await controller.notes()
    assert note.text == "Leaves Friday" and memory_source(note).startswith("Last written by you")
    await controller.save_note("Trip", "The Oslo trip", "Leaves Saturday", note.id)
    assert (await controller.notes())[1].text == "Leaves Saturday"
    await controller.delete_note(note.id)
    assert [n.title for n in await controller.notes()] == [PROFILE]
    failed = []
    await controller.save_note(" ", "", "x", failed=failed.append)
    assert failed and "title" in str(failed[0])


def test_memory_source_label():
    learned = MemoryNote("2", "Trip", "", "x", "c1", 0.0)
    assert memory_source(learned).startswith("Last written in a conversation")


# ── pure model ────────────────────────────────────────────────────────────

CONVERSATION = ConversationInfo("c", None, 0.0, 0.0)


def _snapshot(seq: int = 0) -> RuntimeSnapshot:
    return RuntimeSnapshot(seq=seq, state=AgentState.IDLE, conversation=CONVERSATION,
                           config=None, model="m", usage=UsageSnapshot(Usage(), 0, 0.0),
                           operations=(), pending_permissions=(), queued_messages=())


def _plan(call_id: str, *steps: tuple[str, str]) -> ToolCall:
    return ToolCall(call_id, "update_plan", {"items": [
        {"text": text, "status": status} for text, status in steps]})


def test_the_checklist_follows_the_newest_message_and_leaves_when_the_agent_stops():
    from kadence.frontends.desktop.viewmodel import ChecklistItem

    def said(i: str, role: Role, *blocks: object) -> MessageCommitted:
        return MessageCommitted(Message(i, role, tuple(blocks), 0.0))

    model = ConversationModel()
    model.load(_snapshot(), ())
    model.apply(StateChanged(AgentState.THINKING))
    model.apply(said("u1", Role.USER, Text("fix the bug")))
    model.apply(said("a1", Role.ASSISTANT, _plan("p1", ("Read", "doing"), ("Fix", "todo")),
                     ToolCall("c1", "look", {})))
    model.apply(said("t1", Role.TOOL, ToolResult("p1", ToolStatus.OK, "Plan saved."),
                     ToolResult("c1", ToolStatus.OK, "found")))
    [plan] = of(model, ChecklistItem)
    assert model.items[-1] is plan  # under the step that came after it
    model.apply(said("a2", Role.ASSISTANT, _plan("p2", ("Read", "done"), ("Fix", "doing")),
                     Text("Fixing.")))
    assert of(model, ChecklistItem) == [plan] and model.items[-1] is plan
    assert [str(s.status) for s in plan.steps] == ["done", "doing"]
    model.apply(StateChanged(AgentState.IDLE))
    assert not of(model, ChecklistItem)


def test_a_reopened_conversation_shows_no_checklist_unless_the_agent_is_working():
    from dataclasses import replace

    from kadence.frontends.desktop.viewmodel import ChecklistItem

    history = [
        Message("u1", Role.USER, (Text("fix the bug"),), 0.0),
        Message("a1", Role.ASSISTANT, (_plan("p1", ("Read", "doing"), ("Fix", "todo")),), 0.0),
        Message("t1", Role.TOOL, (ToolResult("p1", ToolStatus.OK, "Plan saved."),), 0.0)]
    model = ConversationModel()
    model.load(_snapshot(), history)
    assert not of(model, ChecklistItem)
    model.prepend(history)
    assert not of(model, ChecklistItem)
    model.load(replace(_snapshot(), state=AgentState.THINKING), history)
    assert model.items[-1] is of(model, ChecklistItem)[0]


def test_thinking_before_and_after_a_plan_update_is_one_line():
    """A plan update is not shown as a step, so the thinking on both sides of it would be
    two Thinking lines one under the other."""
    from kadence.api import Reasoning

    model = ConversationModel()
    model.load(_snapshot(), ())
    model.apply(StateChanged(AgentState.THINKING))
    model.apply(MessageCommitted(Message("u", Role.USER, (Text("try again"),), 0.0)))
    model.apply(MessageCommitted(Message("a1", Role.ASSISTANT, (
        Reasoning("First I plan."), _plan("p", ("Try", "doing"))), 0.0), StopReason.TOOL_USE))
    model.apply(MessageCommitted(Message(
        "t", Role.TOOL, (ToolResult("p", ToolStatus.OK, "Plan saved."),), 0.0)))
    [first] = of(model, AssistantItem)
    model.apply(ReasoningDelta("Now I try."))  # streaming: still one line on screen
    shown = [i for i in of(model, AssistantItem) if not i.hidden]
    assert len(shown) == 1 and shown[0].reasoning == "Now I try."
    model.apply(MessageCommitted(Message("a2", Role.ASSISTANT, (
        Reasoning("Now I try."), Text("Done.")), 0.0), StopReason.END_TURN))
    [only] = of(model, AssistantItem)
    assert only is first and not only.hidden
    assert only.reasoning == "First I plan.\n\nNow I try." and only.text == "Done."
    # Across a step that is shown, thinking stays where it was said.
    model.apply(MessageCommitted(Message("u2", Role.USER, (Text("again"),), 0.0)))
    model.apply(MessageCommitted(Message("a3", Role.ASSISTANT, (
        Reasoning("Look first."), ToolCall("c", "look", {})), 0.0), StopReason.TOOL_USE))
    model.apply(MessageCommitted(Message(
        "t2", Role.TOOL, (ToolResult("c", ToolStatus.OK, "found"),), 0.0)))
    model.apply(MessageCommitted(Message("a4", Role.ASSISTANT, (
        Reasoning("Then answer."), Text("Found it.")), 0.0), StopReason.END_TURN))
    assert [i.reasoning for i in of(model, AssistantItem)][-2:] == ["Look first.",
                                                                  "Then answer."]


def test_waiting_until_something_of_the_next_step_shows():
    """The model can think for a long time before its first token arrives."""
    model = ConversationModel()
    model.load(_snapshot(), ())
    assert not model.waiting
    model.apply(StateChanged(AgentState.THINKING))
    assert model.waiting
    model.apply(ReasoningDelta("Looking"))  # the reply's own Thinking… line takes over
    assert not model.waiting
    model.apply(StateChanged(AgentState.IDLE))
    assert not model.waiting


def test_not_waiting_while_memory_tidies_up_after_the_answer():
    """The cycle stays THINKING for Memory's upkeep, but nothing more is coming."""
    model = ConversationModel()
    model.load(_snapshot(), ())
    model.apply(StateChanged(AgentState.THINKING))
    model.apply(MessageCommitted(Message("u", Role.USER, (Text("hi"),), 0.0)))
    assert model.waiting
    model.apply(MessageCommitted(Message("a", Role.ASSISTANT, (Text("Hello."),), 0.0),
                                 StopReason.END_TURN))
    model.apply(MaintenanceStarted(CONVERSATION.id))
    assert not model.waiting
    model.apply(MaintenanceFinished(CONVERSATION.id))
    model.apply(StateChanged(AgentState.IDLE))
    model.apply(StateChanged(AgentState.THINKING))  # the next cycle
    assert model.waiting
    call = ToolCall("c", "look", {})
    model.apply(MessageCommitted(Message("b", Role.ASSISTANT, (call,), 0.0),
                                 StopReason.TOOL_USE))
    model.apply(StateChanged(AgentState.RUNNING_TOOLS))
    assert not model.waiting  # the step shows
    model.apply(MessageCommitted(Message(
        "r", Role.TOOL, (ToolResult("c", ToolStatus.OK, "found"),), 0.0)))
    model.apply(StateChanged(AgentState.THINKING))
    assert model.waiting  # the model reads the result


def test_failure_banner_until_the_next_cycle():
    model = ConversationModel()
    model.load(_snapshot(), ())
    model.apply(Failure(FailureKind.BUDGET, "budget exhausted: max_cost_usd"))
    assert model.failure is not None and model.failure.kind is FailureKind.BUDGET
    assert of(model, NoticeItem)[0].severity == "error"
    model.apply(StateChanged(AgentState.THINKING))
    assert model.failure is None


def test_a_notice_meant_for_the_model_alone_is_not_shown():
    from kadence.api import Author

    model = ConversationModel()
    model.load(_snapshot(), ())
    model.apply(MessageCommitted(Message(
        "n", Role.USER, (Text("The user is typing again."),), 0.0, Author.KADENCE,
        hidden=True)))
    assert not of(model, NoticeItem) and not of(model, UserItem)


def test_a_compaction_shows_a_line_that_says_how_it_ended():
    from kadence.api import CompactionFinished, CompactionStarted

    model = ConversationModel()
    model.load(_snapshot(), ())
    model.apply(CompactionStarted(CONVERSATION.id))
    [line] = of(model, NoticeItem)
    assert line.text == "Compacting conversation…"
    model.apply(CompactionFinished(CONVERSATION.id, True))
    assert of(model, NoticeItem) == [line] and line.text == "Conversation compacted"
    model.apply(CompactionStarted(CONVERSATION.id))
    model.apply(CompactionFinished(CONVERSATION.id, False))  # nothing was compacted
    assert of(model, NoticeItem) == [line]


def test_stream_cut_off_by_stop_is_marked_interrupted():
    model = ConversationModel()
    model.load(_snapshot(), ())
    model.apply(StateChanged(AgentState.THINKING))
    model.apply(TextDelta("half"))
    model.apply(StateChanged(AgentState.STOPPED))
    (item,) = model.items
    assert isinstance(item, AssistantItem) and item.interrupted and not item.final


def test_spending_with_and_without_a_limit():
    model = ConversationModel()
    model.load(_snapshot(), ())
    model.usage = UsageSnapshot(Usage(cost_usd=1.2), 0, 0.0, {"max_cost_usd": 8.8})
    assert model.spending_text == "$1.20 of $10 spent"
    model.usage = UsageSnapshot(Usage(cost_usd=1.2), 0, 0.0)
    assert model.spending_text == "$1.20 spent"


class GapRuntime:
    """Events with a gap in seq; counts recoveries."""

    def __init__(self) -> None:
        self.snapshots = 0
        self.history_messages = [Message("m1", Role.USER, (Text("hello"),), 0.0)]

    async def _events(self) -> AsyncIterator[EventEnvelope]:
        yield EventEnvelope(1, StateChanged(AgentState.THINKING))
        yield EventEnvelope(5, StateChanged(AgentState.IDLE))  # 2-4 were dropped
        yield EventEnvelope(6, ShuttingDown("done"))

    def events(self) -> AsyncIterator[EventEnvelope]:
        return self._events()

    def snapshot(self) -> RuntimeSnapshot:
        self.snapshots += 1
        return _snapshot(seq=0 if self.snapshots == 1 else 4)

    async def history(self, conversation_id: str, **kw: object) -> list[Message]:
        return self.history_messages


async def test_seq_gap_triggers_recovery():
    runtime = GapRuntime()
    model = ConversationModel()
    await asyncio.wait_for(Controller(runtime, model).run(), 5)  # type: ignore[arg-type]
    assert runtime.snapshots == 2
    assert [i.text for i in of(model, UserItem)] == ["hello"]  # rendered once
    assert model.closed


# ── settings form ─────────────────────────────────────────────────────────

SETTINGS = fake_settings(work_folder="/home/u/Kadence", permissions=PermissionPolicy.ASK)


def test_an_untouched_form_changes_nothing():
    assert settings_update(SETTINGS, form_from(SETTINGS)) == SettingsUpdate()


def test_only_changed_fields_are_sent():
    from dataclasses import replace

    form = replace(form_from(SETTINGS), model="m2", maintenance_model="m3",
                   spending_limit="$2.50", permissions=PermissionPolicy.DENY,
                   instructions="Be brief.", work_folder="/tmp/w", api_key=" k ")
    assert settings_update(SETTINGS, form) == SettingsUpdate(
        api_key="k", model="m2", maintenance_model="m3", budget=Budget(max_cost_usd=2.5),
        permissions=PermissionPolicy.DENY, system_prompt="Be brief.", work_folder="/tmp/w")
    cleared = replace(form_from(replace(SETTINGS, maintenance_model="m3")),
                      maintenance_model=None, spending_limit="")
    assert settings_update(replace(SETTINGS, maintenance_model="m3"), cleared) == SettingsUpdate(
        maintenance_model="", budget=Budget())


def test_switching_provider_leaves_the_models_to_it():
    from dataclasses import replace

    form = replace(form_from(SETTINGS), modules={**form_from(SETTINGS).modules, "core": "local"},
                   model="m2", api_key="sk")
    assert settings_update(SETTINGS, form) == SettingsUpdate(modules={"core": "local"},
                                                             api_key="sk")


def test_only_changed_modules_are_sent():
    from dataclasses import replace

    modules = {**form_from(SETTINGS).modules, "tools": "other-tools", "frontend": "desktop"}
    assert settings_update(SETTINGS, replace(form_from(SETTINGS), modules=modules)) == (
        SettingsUpdate(modules={"tools": "other-tools", "frontend": "desktop"}))


@pytest.mark.parametrize("field, value, message", [
    ("spending_limit", "ten", "amount in dollars"),
    ("spending_limit", "-1", "amount in dollars"),
    ("instructions", "  ", "can't be empty"),
    ("work_folder", " ", "work folder"),
])
def test_invalid_values_are_explained(field, value, message):
    from dataclasses import replace

    with pytest.raises(ValueError, match=message):
        settings_update(SETTINGS, replace(form_from(SETTINGS), **{field: value}))


def test_welcome_conversation_labels_and_refusals():
    from dataclasses import replace

    def cores(chosen: str | None, *installed: ModuleInfo) -> Settings:
        return replace(SETTINGS, startup_error="no key", modules={
            **SETTINGS.modules, "core": ModuleChoice(chosen, chosen, installed)})

    assert not needs_welcome(SETTINGS)
    no_key = cores("anthropic", ModuleInfo("anthropic", "Claude", "ANTHROPIC_API_KEY"))
    assert needs_welcome(no_key)
    assert not needs_welcome(replace(no_key, startup_error=None))  # it started
    assert not needs_welcome(cores(None))  # custom Core
    assert not needs_welcome(cores("local", ModuleInfo("local", "Local")))
    old = ConversationInfo("a", "Trip plans", 0.0, 1.0)
    new = ConversationInfo("b", None, 0.0, 2.0)
    assert newest_first([old, new]) == [new, old]
    assert conversation_label(old) == "Trip plans"
    assert conversation_label(new) == "New chat"
    previewed = ConversationInfo("c", None, 0.0, 3.0, preview="  What should I\npack for Oslo?")
    assert conversation_label(previewed) == "What should I pack for Oslo?"
    assert conversation_preview(previewed) == ""  # already the label
    assert conversation_preview(replace(previewed, title="Oslo trip")) == (
        "What should I pack for Oslo?")
    assert conversation_preview(old) == ""
    assert conversation_label(replace(previewed, preview="x" * 200)).endswith("…")
    assert len(conversation_label(replace(previewed, preview="x" * 200))) == LABEL_CHARS
    assert user_message(RuntimeStateError("cannot start while thinking")) == BUSY_MESSAGE
    assert user_message(RuntimeStateError("budget exhausted: max_cost_usd")) == BUDGET_MESSAGE
    assert user_message(InstanceLockedError("Kadence is already open in another window (x)")
                        ) == "Kadence is already open in another window (x)."
    assert user_message(ValueError("unknown model x")) == "unknown model x"


def test_relative_dates():
    import time

    now = time.mktime((2026, 11, 10, 15, 30, 0, 0, 0, -1))  # a Tuesday

    def at(day: int, hour: int = 9, month: int = 11, year: int = 2026) -> float:
        return time.mktime((year, month, day, hour, 5, 0, 0, 0, -1))

    assert relative_date(at(10), now) == "09:05"
    assert relative_date(at(9, 23), now) == "Yesterday"
    assert relative_date(at(5), now) == "Thu"
    assert relative_date(at(3), now) == "3 Nov"
    assert relative_date(at(3, year=2025), now) == "3 Nov 2025"


def test_a_tool_call_with_a_description_shows_it_as_its_title():
    tool = ToolItem("c", "shell", '{"command": "ls -la", "step_summary": "List the files"}')
    assert tool.summary == "List the files" and tool.detail == "ls -la"
    plain = ToolItem("c", "remember", '{"text": "Studies CS at UCL"}')
    assert plain.summary == "Studies CS at UCL" and plain.sentence is None
    streaming = ToolItem("c", "shell", '{"command": "ls')
    assert streaming.summary == '{"command": "ls'


async def test_a_tool_the_provider_ran_is_a_finished_step(make):
    from kadence.api import ServerToolUse

    use = ServerToolUse("srv_1", "web_search", {"query": "worlds"},
                        "Searched the web for “worlds”", "Worlds 2026\nhttps://w.example")
    h: Harness = await make()
    h.core.add(Step(text="Found it.", server=[use]))
    await h.start()
    model, controller, _ = await attach(h)
    controller.send("search")
    await eventually(lambda: of(model, ToolItem) and not model.busy)
    (step,) = of(model, ToolItem)
    assert step.summary == "Searched the web for “worlds”" and step.status is ToolStatus.OK
    assert "w.example" in step.content


async def test_a_question_for_help_is_shown_and_named_in_the_status(make):
    from kadence.api import Budget, Duty

    h: Harness = await make(budget=Budget(max_cost_usd=5))
    h.core.add(Step(calls=[call("ask_for_help", {"question": "Which account?"})]),
               Step(text="Waiting for you."))
    await h.start()
    model, controller, _ = await attach(h)
    await h.runtime.set_duty(Duty("Run socials"))
    await eventually(lambda: any("Which account?" in n.text for n in of(model, NoticeItem)))
    await until_state(h, controller, AgentState.IDLE)
    assert "Duty waiting for your answer" in model.status_text
    controller.send("The main one.")
    await eventually(lambda: model.help_needed is None)


async def test_a_runtime_notice_is_shown_as_a_notice_not_as_the_users_words(make):
    from kadence.api import Budget, Duty
    from kadence.frontends.desktop.viewmodel import conversation_label

    h: Harness = await make(budget=Budget(max_cost_usd=5))
    h.core.add(Step(text="On it."))
    info = await h.start()
    model, _controller, _ = await attach(h)
    await h.runtime.set_duty(Duty("Watch the inbox"))
    await eventually(lambda: any("New duty: Watch the inbox" in n.text
                                 for n in of(model, NoticeItem)))
    assert not any("New duty" in u.text for u in of(model, UserItem))
    [notice] = [n for n in of(model, NoticeItem) if "New duty" in n.text]
    assert notice.text == "New duty: Watch the inbox"
    [stored] = await h.runtime.list_conversations()
    assert stored.id == info.id and conversation_label(stored) == "New chat"


def test_passing_80_percent_of_the_limit_gives_one_warning():
    from kadence.api import Usage, UsageSnapshot, UsageUpdated

    model = ConversationModel()
    model.usage = UsageSnapshot(Usage(0, 0, cost_usd=7.0), 0, 0.0, {"max_cost_usd": 3.0})

    def spend(cost: float) -> None:
        model.apply(UsageUpdated(UsageSnapshot(Usage(0, 0, cost_usd=cost), 0, 0.0,
                                               {"max_cost_usd": 10.0 - cost})))

    spend(7.9)
    assert not of(model, NoticeItem)
    spend(8.1)
    spend(9.0)
    [warning] = of(model, NoticeItem)
    assert warning.severity == "warning" and "$8.10 of your $10 limit" in warning.text


async def test_opening_a_conversation_finishes_once_it_is_shown(make):
    h: Harness = await make()
    other = await h.chat()
    await h.chat()
    model, controller, _ = await attach(h)
    history = h.runtime.history

    async def slow_history(*args: Any, **kwargs: Any) -> Any:  # as SQLite, in a thread
        await asyncio.sleep(0.05)
        return await history(*args, **kwargs)

    h.runtime.history = slow_history  # type: ignore[method-assign]
    seen: list[str] = []
    task = controller.open_conversation(other.id)
    task.add_done_callback(lambda _: seen.append(model.conversation.id))  # as the window
    await task
    await asyncio.sleep(0)
    assert seen == [other.id]  # the sidebar refreshed then highlights the new one
