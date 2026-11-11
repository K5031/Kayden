"""Sub-sessions: the frontend hosts children, headless Kayden processes the agent starts
for self-contained tasks; each reports back and can be watched while it works."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
import tomli_w

from kayden.api import OperationResult, PermissionPolicy, ToolResult, ToolStatus
from kayden.frontends.sessions import SubSessions
from kayden.runtime import InstanceConfig, ModuleSpec, RuntimeConfig, run_instance
from tests.conftest import Harness, eventually
from tests.fakes.core import ScriptedCore, Step, call

ROOT = Path(__file__).resolve().parents[1]


def finish(report: str) -> dict[str, Any]:
    return {"calls": [{"name": "finish_goal", "arguments": {"report": report}}]}


async def parent(make: Any, tmp_path: Path, steps: list[Step], child_script: list[Any],
                 child_runtime: dict[str, Any] | None = None) -> tuple[Harness, SubSessions]:
    data = tmp_path / "parent"
    data.mkdir()
    (data / "config.toml").write_text(tomli_w.dumps({
        "core": {"factory": "tests.fakes.core:factory", "options": {"script": child_script}},
        "tools": {"factory": "tests.fakes.tools:factory", "options": {"instant": ["lookup"]}},
        **(child_runtime or {})}))
    sessions = SubSessions(data, check_in_s=(0.05, 0.2))
    h: Harness = await make(core=ScriptedCore(steps), data_dir=data, more_tools=[sessions],
                            permissions=PermissionPolicy.ASK)
    h.auto_approve()
    await h.start(folder=str(tmp_path))
    return h, sessions


def outcome(transcript: list[Any], call_id: str) -> OperationResult | ToolResult | None:
    """The call's final result, whether it finished in the foreground or the background."""
    return next((b for m in transcript for b in m.blocks
                 if isinstance(b, OperationResult | ToolResult) and b.call_id == call_id
                 and b.status != ToolStatus.BACKGROUNDED), None)


async def test_a_child_works_on_its_task_and_reports_back(make, tmp_path, child_env):
    h, sessions = await parent(make, tmp_path, [
        Step(calls=[call("start_session", {"task": "Write a haiku", "budget_usd": 0.5},
                         id="s")]),
        Step(text="A sub-session is on it."),
        Step(text="Here is the haiku.")],
        [{"text": "Drafting."}, finish("Haiku written."), {"text": "Done."}])
    await h.turn("Get me a haiku")
    result = await eventually(lambda: outcome(h.store.conversations[h.conversation.id]
                                              .messages, "s"), timeout=30)
    assert result.status == ToolStatus.OK and "Haiku written." in result.content
    [child] = sessions.sessions
    assert child.task == "Write a haiku" and child.state == "done"
    assert child.conversation_id == h.conversation.id
    assert any("Drafting." in text for _, text in child.lines())
    settings = tomllib.loads((child.data_dir / "settings.toml").read_text())
    assert settings["budget"] == {"max_cost_usd": 0.5}
    assert settings["work_folder"] == str(tmp_path)
    await eventually(lambda: child.returncode is not None, timeout=10)  # it quit


async def test_a_question_comes_back_and_an_answer_lets_it_finish(make, tmp_path, child_env):
    h, sessions = await parent(make, tmp_path, [
        Step(calls=[call("start_session", {"task": "Write a poem"}, id="s")]),
        Step(text="Started."),
        Step(calls=[call("message_session", {"session": "1", "text": "A sonnet."}, id="m")]),
        Step(text="Answered."),
        Step(text="Got the sonnet.")],
        [{"calls": [{"name": "ask_for_help", "arguments": {"question": "Which form?"}}]},
         {"text": "Waiting."}, finish("Wrote a sonnet."), {"text": "Done."}])
    await h.turn("Get me a poem")
    asked = await eventually(lambda: outcome(h.store.conversations[h.conversation.id]
                                             .messages, "s"), timeout=30)
    assert "Which form?" in asked.content and "message_session" in asked.content
    answered = await eventually(lambda: outcome(h.store.conversations[h.conversation.id]
                                                .messages, "m"), timeout=30)
    assert "Wrote a sonnet." in answered.content
    assert sessions.sessions[0].state == "done"


async def test_cancelling_the_call_stops_the_child(make, tmp_path, child_env):
    h, sessions = await parent(make, tmp_path, [
        Step(calls=[call("start_session", {"task": "Think forever"}, id="s")]),
        Step(text="Started.")],
        [{"hang": True}])
    await h.turn("Go")
    await eventually(lambda: sessions.sessions and sessions.sessions[0].state == "working",
                     timeout=30)
    child = sessions.sessions[0]
    await h.runtime.cancel_operation("s")
    await eventually(lambda: child.returncode is not None, timeout=15)
    assert child.state == "stopped"


async def test_a_childs_permission_prompt_waits_for_the_user(make, tmp_path, child_env):
    from kayden.api import Decision

    h, sessions = await parent(make, tmp_path, [
        Step(calls=[call("start_session", {"task": "Look it up"}, id="s")]),
        Step(text="Started."), Step(text="Reported.")],
        [{"calls": [{"name": "lookup"}]}, finish("Looked it up."), {"text": "Done."}],
        child_runtime={"runtime": {"permissions": "ask"}})
    await h.turn("Go")
    child = await eventually(lambda: sessions.sessions and sessions.sessions[0].approvals
                             and sessions.sessions[0], timeout=30)
    assert child.summary.startswith("Needs your approval")
    remote = await sessions.open(child)
    [request] = remote.snapshot().pending_permissions
    assert request.call.name == "lookup"
    await remote.resolve_permission(request.id, Decision.ALLOW_ONCE)
    result = await eventually(lambda: outcome(h.store.conversations[h.conversation.id]
                                              .messages, "s"), timeout=30)
    assert "Looked it up." in result.content
    sessions.release(child, remote)


async def test_a_child_is_listed_after_a_restart_and_opens_again(make, tmp_path, child_env):
    h, sessions = await parent(make, tmp_path, [
        Step(calls=[call("start_session", {"task": "Write a haiku"}, id="s")]),
        Step(text="Started."), Step(text="Got it.")],
        [finish("Haiku written."), {"text": "Done."}])
    await h.turn("Go")
    await eventually(lambda: sessions.sessions and sessions.sessions[0].state == "done",
                     timeout=30)
    await h.close()
    again = SubSessions(h.data_dir)
    [child] = again.sessions
    assert (child.task, child.state, child.report) == ("Write a haiku", "done",
                                                       "Haiku written.")
    remote = await again.open(child)  # starts it again on its conversation
    history = await remote.history(child.child_conversation_id)
    assert any("Done." in str(m.blocks) for m in history)
    assert remote.snapshot().conversation.id == child.child_conversation_id
    again.release(child, remote)
    await again.aclose()
    assert child.returncode is not None


async def test_unknown_sessions_are_errors(make, tmp_path, child_env):
    h, _ = await parent(make, tmp_path, [
        Step(calls=[call("message_session", {"session": "9", "text": "hi"}, id="m")]),
        Step(text="ok")], [])
    await h.turn("Go")
    assert (await h.results())["m"].status == ToolStatus.ERROR


async def test_a_frontend_offers_its_tools(tmp_path, clean_env):
    from tests.fakes.tools import FakeTools

    extra = FakeTools()
    extra.instant("from_the_frontend")
    seen: list[str] = []

    async def script(runtime, frontend) -> None:
        seen.extend(t.name for t in await runtime.list_tools())

    import tests.fakes.frontend as fakes

    original = fakes.factory

    def factory(options, paths):
        return original({**options, "script": script, "tools": [extra]}, paths)

    fakes.tools_factory = factory  # type: ignore[attr-defined]
    try:
        await run_instance(InstanceConfig(
            runtime=RuntimeConfig(data_dir=tmp_path / "d"),
            core=ModuleSpec("tests.fakes.core:factory"),
            memory=ModuleSpec("tests.fakes.memory:factory"),
            tools=ModuleSpec("tests.fakes.tools:factory", {"instant": ["lookup"]}),
            frontend=ModuleSpec("tests.fakes.frontend:tools_factory")))
    finally:
        del fakes.tools_factory  # type: ignore[attr-defined]
    assert seen.index("from_the_frontend") > seen.index("lookup")
    assert extra.closed


async def test_a_cancelled_open_leaves_the_child_running(make, tmp_path, child_env):
    import asyncio

    h, sessions = await parent(make, tmp_path, [
        Step(calls=[call("start_session", {"task": "Think forever"}, id="s")]),
        Step(text="Started.")],
        [{"hang": True}])
    await h.turn("Go")
    child = await eventually(lambda: sessions.sessions and sessions.sessions[0].state
                             == "working" and sessions.sessions[0], timeout=30)
    opening = asyncio.create_task(sessions.open(child))
    await asyncio.sleep(0)
    opening.cancel()
    await asyncio.gather(opening, return_exceptions=True)
    remote = await sessions.open(child)  # the reply to the cancelled request is ignored
    assert remote.snapshot().state.value == "thinking"
    sessions.release(child, remote)
    assert child.state == "working"


async def test_an_idea_of_the_agents_is_proposed_and_starts_on_approval(
        make, tmp_path, child_env):
    h, sessions = await parent(make, tmp_path, [
        Step(calls=[call("propose", {"call": {"name": "start_session",
                                              "arguments": {"task": "Tidy the notes"}},
                                     "reason": "They are messy"}, id="p")]),
        Step(text="Proposed."), Step(text="It is done.")],
        [finish("Tidied."), {"text": "Done."}])
    await h.turn("Hi")
    assert sessions.sessions == []  # nothing starts before the user approves
    await h.runtime.resolve_proposal("p", True)
    result = await eventually(lambda: outcome(h.store.conversations[h.conversation.id]
                                              .messages, "p"), timeout=30)
    assert "Tidied." in result.content


async def test_a_turn_nobody_waited_for_becomes_a_notice_for_the_parent(
        make, tmp_path, child_env):
    h, sessions = await parent(make, tmp_path, [
        Step(calls=[call("start_session", {"task": "Write a haiku"}, id="s")]),
        Step(text="Started."), Step(text="Got it.")],
        [finish("Haiku written."), {"text": "Done."}, {"text": "Made it shorter."}])
    await h.turn("Go")
    child = await eventually(lambda: sessions.sessions and sessions.sessions[0].state == "done"
                             and sessions.sessions[0], timeout=30)
    assert sessions.take_notes(h.conversation.id) == []  # the call got that turn
    remote = await sessions.open(child)
    await remote.send("Shorter, please")  # the user talks to it directly
    [note] = await eventually(lambda: sessions.notes.get(h.conversation.id), timeout=20)
    assert note.startswith("[Runtime notice] Sub-session 1 (Write a haiku) ended a turn")
    assert sessions.take_notes(h.conversation.id) == [note]
    assert sessions.take_notes(h.conversation.id) == []
    sessions.release(child, remote)


async def test_a_stopped_child_resumes_its_goal(make, tmp_path, child_env):
    h, sessions = await parent(make, tmp_path, [
        Step(calls=[call("start_session", {"task": "Keep at it"}, id="s")]),
        Step(text="Started.")],
        [{"hang": True}])
    await h.turn("Go")
    child = await eventually(lambda: sessions.sessions and sessions.sessions[0].state
                             == "working" and sessions.sessions[0], timeout=30)
    await h.runtime.cancel_operation("s")
    await eventually(lambda: child.returncode is not None, timeout=15)
    assert child.state == "stopped" and child.resumable
    await sessions.resume(child)
    await eventually(lambda: child.agent_state is not None
                     and child.agent_state.value == "thinking", timeout=20)
    assert child.state == "working"
    sessions.stop(child)


async def test_children_are_renamed_archived_and_deleted(tmp_path):
    from kayden.frontends.sessions import SubSession

    sessions = SubSessions(tmp_path)
    folder = tmp_path / "sessions" / "conv-1"
    folder.mkdir(parents=True)
    child = SubSession("1", "Write a haiku", "conv", folder, state="done")
    sessions.sessions.append(child)
    sessions.rename(child, "Haiku")
    sessions.archive(child, True)
    await sessions.aclose()  # the record is written
    again = SubSessions(tmp_path)
    [kept] = again.sessions
    assert (kept.name, kept.archived) == ("Haiku", True)
    assert again.of("conv") == [] and again.of("conv", include_archived=True) == [kept]
    await again.delete(kept)
    assert again.sessions == [] and not folder.exists()


async def test_cancelling_while_a_child_starts_stops_it(tmp_path):
    """The child's first requests are still unanswered: the call is cancelled there."""
    import asyncio
    import sys

    from kayden.api import ToolCall

    class Ctx:
        conversation_id = "conv"

        def progress(self, text: str) -> None:
            pass

    silent = [sys.executable, "-c", "import sys; sys.stdin.read()"]  # quits on EOF
    sessions = SubSessions(tmp_path, command=silent)
    call_ = ToolCall("c", "start_session", {"task": "Anything"})
    running = asyncio.create_task(sessions.execute(call_, Ctx()))
    await eventually(lambda: sessions._channels, timeout=10)  # started, not answering
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)
    [child] = sessions.sessions
    await eventually(lambda: child.returncode is not None, timeout=20)
    assert child.state == "stopped"


async def test_the_users_limit_is_the_default_and_the_most_a_child_gets(
        make, tmp_path, child_env):
    h, sessions = await parent(make, tmp_path, [
        Step(calls=[call("start_session", {"task": "Small"}, id="a"),
                    call("start_session", {"task": "Greedy", "budget_usd": 50}, id="b"),
                    call("start_session", {"task": "Frugal", "budget_usd": 0.2}, id="c")]),
        Step(text="Started.")],
        [{"hang": True}])
    await sessions.set_limit(3)
    assert "up to $3" in (await sessions.list_tools())[0].description
    await h.turn("Go")
    await eventually(lambda: len(sessions.sessions) == 3, timeout=30)
    assert {s.task: s.budget_usd for s in sessions.sessions} == {
        "Small": 3, "Greedy": 3, "Frugal": 0.2}
    for child in sessions.sessions:
        assert tomllib.loads((child.data_dir / "settings.toml").read_text())["budget"] == {
            "max_cost_usd": child.budget_usd}
    assert SubSessions(h.data_dir).limit_usd == 3  # kept
    for child in sessions.sessions:
        sessions.stop(child)


async def test_a_childs_limit_changes_while_it_runs_and_after(make, tmp_path, child_env):
    h, sessions = await parent(make, tmp_path, [
        Step(calls=[call("start_session", {"task": "Keep at it"}, id="s")]),
        Step(text="Started.")],
        [{"hang": True}])
    await h.turn("Go")
    child = await eventually(lambda: sessions.sessions and sessions.sessions[0].state
                             == "working" and sessions.sessions[0], timeout=30)
    await eventually(lambda: child.child_conversation_id, timeout=30)
    await sessions.set_child_limit(child, 7.5)
    settings = await sessions._channels[child.id].request("settings")
    assert settings["budget"]["max_cost_usd"] == 7.5 and child.budget_usd == 7.5
    sessions.stop(child)
    await eventually(lambda: child.returncode is not None, timeout=20)
    await sessions.set_child_limit(child, 2)
    assert tomllib.loads((child.data_dir / "settings.toml").read_text())["budget"] == {
        "max_cost_usd": 2}
    with pytest.raises(ValueError):
        await sessions.set_child_limit(child, 0)
